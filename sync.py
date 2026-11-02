#!/usr/bin/env python3
"""
Email new upcoming Librus calendar events as .ics invitations.

Read-only towards Librus: only calendar (terminarz) pages are fetched, so no
message is ever marked as read. Pipeline:

    fetch -> filter (drop absences etc.) -> dedupe against state.json -> email

Each event is reduced to what / when / details. Without --send nothing is
emailed and the state is not touched (dry run).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import pathlib
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import librus
import requests_html
import yaml
from librus_pyapi import LibrusAPI

logger = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_CANDIDATES = [HERE / "config.yml", HERE / "config.yaml"]
DEFAULT_STATE = HERE / "state.json"
# Categories/subjects containing any of these (case-insensitive) are skipped.
DEFAULT_EXCLUDE = ["nieobecn", "absen"]


@dataclass
class Event:
    account: str
    subject: str
    category: str
    when: datetime.date
    lesson: str
    interval: str
    teacher: str
    classroom: str
    description: str
    link: str

    @property
    def what(self) -> str:
        return " ".join(p for p in [self.subject, f"({self.category})" if self.category else ""] if p) or "(no title)"

    @property
    def when_detail(self) -> str:
        return ", ".join(p for p in [f"lesson {self.lesson}" if self.lesson else "", self.interval] if p)

    @property
    def details(self) -> str:
        return "\n".join(
            p
            for p in [
                f"Teacher: {self.teacher}" if self.teacher else "",
                f"Room: {self.classroom}" if self.classroom else "",
                self.description,
                self.link,
            ]
            if p
        )

    @property
    def key(self) -> str:
        """Stable identity. Description edits do not make an event 'new'."""
        raw = "|".join([self.account, self.when.isoformat(), self.lesson, self.subject, self.category])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --- config -----------------------------------------------------------------

def find_config(explicit: Optional[pathlib.Path]) -> pathlib.Path:
    if explicit:
        return explicit
    for path in CONFIG_CANDIDATES:
        if path.exists():
            return path
    raise SystemExit(f"No config found (tried: {', '.join(p.name for p in CONFIG_CANDIDATES)})")


def load_config(path: pathlib.Path) -> Dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    accounts = [
        {
            "name": item.get("name") or item["username"],
            "username": _secret(item, "username"),
            "password": _secret(item, "password"),
        }
        for item in raw.get("accounts", [])
    ]
    if not accounts:
        raise SystemExit(f"No accounts in {path}")
    raw["accounts"] = accounts
    return raw


def _secret(item: Dict, key: str) -> str:
    return str(item[key]) if key in item else os.environ[item[f"{key}_env"]]


# --- fetch ------------------------------------------------------------------

def parse_date(value: Any) -> Optional[datetime.date]:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    return None


def to_event(account: str, exam: librus.Exam) -> Optional[Event]:
    when = parse_date(exam.date)
    if not when:
        logger.warning("%s: skipping event with unparsable date %r", account, exam.date)
        return None
    return Event(
        account=account,
        subject=exam.subject or "",
        category=exam.category or "",
        when=when,
        lesson=exam.lesson or "",
        interval=exam.interval or "",
        teacher=exam.teacher or "",
        classroom=exam.classroom or "",
        description=exam.specification or "",
        link=exam.online_lesson_link or "",
    )


def login(username: str, password: str) -> librus.LibrusSession:
    """Log in with librus_pyapi (current auth flow), scrape with librus (calendar parser)."""
    authed = LibrusAPI(username, password).session
    html = requests_html.HTMLSession()
    html.cookies.update(authed.cookies)
    session = librus.LibrusSession()
    session._html_session = html
    return session


def fetch_upcoming(accounts: List[Dict[str, str]], days: int) -> List[Event]:
    """Returns events; raises if any account failed, so a partial run never marks anything as seen."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    events: List[Event] = []
    for acc in accounts:
        session = login(acc["username"], acc["password"])
        for exam in session.list_exams():
            event = to_event(acc["name"], exam)
            if event and today <= event.when <= horizon:
                events.append(event)
    events.sort(key=lambda e: (e.when, e.account, e.what))
    return events


# --- filter -----------------------------------------------------------------

def keep(event: Event, exclude: List[str]) -> bool:
    haystack = f"{event.category} {event.subject}".lower()
    return not any(word.lower() in haystack for word in exclude)


# --- state ------------------------------------------------------------------

def load_state(path: pathlib.Path) -> Dict[str, str]:
    """key -> ISO timestamp of when it was sent."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("sent", {})


def save_state(path: pathlib.Path, sent: Dict[str, str]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"sent": sent}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- ics + email ------------------------------------------------------------

def _esc(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 folding: max 75 octets per line, never splitting a UTF-8 character."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 74:
            out.append(current)
            current, size = " " + ch, 1 + n
        else:
            current += ch
            size += n
    out.append(current)
    return "\r\n".join(out)


def build_ics(event: Event, sender: str, attendee: str) -> str:
    """A one-event iCalendar invitation (METHOD:REQUEST) so Gmail shows it as an invite."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = event.when + datetime.timedelta(days=1)
    summary = f"{event.account} - {event.what}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//librus-sync//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{event.key}@librus-sync",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{event.when:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(event.when_detail + chr(10) + event.details if event.when_detail else event.details)}",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
        f"ORGANIZER;CN=Librus:mailto:{sender}",
        f"ATTENDEE;CN={attendee};ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:{attendee}",
    ]
    if event.classroom:
        lines.append(f"LOCATION:{_esc(event.classroom)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def gcal_link(event: Event) -> str:
    """Fallback for clients that ignore .ics: a one-click 'add to Google Calendar' URL."""
    end = event.when + datetime.timedelta(days=1)
    params = {
        "action": "TEMPLATE",
        "text": f"{event.account} - {event.what}",
        "dates": f"{event.when:%Y%m%d}/{end:%Y%m%d}",
        "details": (event.when_detail + "\n" if event.when_detail else "") + event.details,
    }
    if event.classroom:
        params["location"] = event.classroom
    return "https://calendar.google.com/calendar/render?" + urlencode(params, quote_via=quote)


def build_message(event: Event, sender: str, to: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"[{event.account}] {event.when:%a %d.%m} {event.what}"
    when = f"{event.when:%A, %Y-%m-%d}" + (f" ({event.when_detail})" if event.when_detail else "")
    msg.set_content(
        f"What:    {event.account} - {event.what}\n"
        f"When:    {when}\n"
        f"Details:\n{event.details or '-'}\n\n"
        f"Add to Google Calendar: {gcal_link(event)}\n"
    )
    ics = build_ics(event, sender, to)
    msg.add_alternative(ics, subtype="calendar", charset="utf-8", params={"method": "REQUEST"})
    msg.add_attachment(ics, subtype="calendar", charset="utf-8", params={"method": "REQUEST"}, filename="event.ics")
    return msg


class Mailer:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client: Optional[smtplib.SMTP] = None

    def __enter__(self) -> "Mailer":
        c, ctx = self.cfg, ssl.create_default_context()
        if c.get("use_ssl"):
            self.client = smtplib.SMTP_SSL(c["host"], int(c["port"]), timeout=20, context=ctx)
        else:
            self.client = smtplib.SMTP(c["host"], int(c["port"]), timeout=20)
            self.client.ehlo()
            if c.get("use_starttls", True):
                self.client.starttls(context=ctx)
        self.client.login(_secret(c, "username"), _secret(c, "password"))
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.client.quit()
        except (OSError, smtplib.SMTPException):
            pass

    def send(self, msg: EmailMessage) -> None:
        self.client.send_message(msg)


# --- main -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, help="default: config.yml, then config.yaml")
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    parser.add_argument("--days", type=int, default=21, help="look-ahead window")
    parser.add_argument("--send", action="store_true", help="actually send emails and update state (default: dry run)")
    parser.add_argument("--baseline", action="store_true", help="mark all current events as seen without emailing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config(find_config(args.config))
    exclude = cfg.get("exclude", DEFAULT_EXCLUDE)
    to = cfg["notify"]["to"]

    fetched = fetch_upcoming(cfg["accounts"], args.days)
    events = [e for e in fetched if keep(e, exclude)]
    sent = load_state(args.state)
    new = [e for e in events if e.key not in sent]
    logger.info("%d fetched, %d after filter, %d new", len(fetched), len(events), len(new))

    if args.baseline:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        save_state(args.state, {**sent, **{e.key: now for e in events}})
        logger.info("Baseline saved: %d events marked as seen", len(events))
        return

    if not args.send:
        for e in new:
            print(f"[{e.account}] {e.what}\n  when:    {e.when:%Y-%m-%d} {e.when_detail}\n  details: {e.details or '-'}\n")
        print(f"DRY RUN: {len(new)} new event(s) would be emailed to {to}")
        return

    if not new:
        return
    sender = cfg["email"]["sender"]
    with Mailer(cfg["email"]) as mailer:
        for e in new:
            mailer.send(build_message(e, sender, to))
            sent[e.key] = datetime.datetime.now().isoformat(timespec="seconds")
            save_state(args.state, sent)  # per event, so a crash never re-sends
            logger.info("Sent: [%s] %s %s", e.account, e.when, e.what)


if __name__ == "__main__":
    main()
