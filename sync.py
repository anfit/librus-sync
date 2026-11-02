#!/usr/bin/env python3
"""
Email upcoming Librus calendar events as .ics invitations, and keep Google
Calendar in step when they change or disappear.

Read-only towards Librus: only calendar (terminarz) pages are fetched, so no
message is ever marked as read. Pipeline:

    fetch -> filter (drop absences etc.) -> diff against state.json -> email

The diff yields three kinds of mail, all keyed by a stable UID so the
calendar updates the same entry instead of duplicating it:
    new     METHOD:REQUEST, SEQUENCE 0
    update  METHOD:REQUEST, SEQUENCE+1   (description/teacher/room/... changed)
    cancel  METHOD:CANCEL,  SEQUENCE+1   (gone from Librus, still in the window)

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
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple
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
        """Stable identity (also the calendar UID). Content edits do not change it."""
        raw = "|".join([self.account, self.when.isoformat(), self.lesson, self.subject, self.category])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        """Changes when an edit should reach the calendar as an update."""
        raw = "|".join([self.interval, self.teacher, self.classroom, self.description, self.link])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def snapshot(self) -> Dict[str, str]:
        return {**asdict(self), "when": self.when.isoformat()}

    @classmethod
    def from_snapshot(cls, data: Dict[str, str]) -> "Event":
        return cls(**{**data, "when": datetime.date.fromisoformat(data["when"])})


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
            "recipients": list(item.get("recipients") or []),
        }
        for item in raw.get("accounts", [])
    ]
    for acc in accounts:
        if not acc["recipients"]:
            raise SystemExit(f"Account {acc['name']} has no recipients")
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
    """Returns events; raises if any account failed, so a partial run never changes state."""
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


# --- state + diff -----------------------------------------------------------

# state.json: {"events": {key: {"seq": int, "hash": str, "cancelled": bool,
#                               "sent": iso timestamp, "event": snapshot}}}
State = Dict[str, Dict[str, Any]]


def load_state(path: pathlib.Path) -> State:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("events", {})


def save_state(path: pathlib.Path, state: State) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"events": state}, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record(state: State, event: Event, seq: int, cancelled: bool = False) -> None:
    state[event.key] = {
        "seq": seq,
        "hash": event.content_hash,
        "cancelled": cancelled,
        "sent": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event.snapshot(),
    }


# (action, event, sequence); action in "new" | "update" | "cancel"
Change = Tuple[str, Event, int]


def diff(events: List[Event], state: State, days: int) -> List[Change]:
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    changes: List[Change] = []
    current = {e.key: e for e in events}

    for key, e in current.items():
        entry = state.get(key)
        if entry is None:
            changes.append(("new", e, 0))
        elif entry["cancelled"]:
            changes.append(("new", e, entry["seq"] + 1))  # reappeared: re-issue with a newer sequence
        elif entry["hash"] != e.content_hash:
            changes.append(("update", e, entry["seq"] + 1))

    for key, entry in state.items():
        if key in current or entry["cancelled"]:
            continue
        old = Event.from_snapshot(entry["event"])
        if today <= old.when <= horizon:  # only judge events the fetch could actually have seen
            changes.append(("cancel", old, entry["seq"] + 1))
    return changes


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


def build_ics(event: Event, sender: str, attendees: List[str], method: str = "REQUEST", sequence: int = 0) -> str:
    """A one-event iCalendar message so Gmail shows it as an invitation (or cancellation)."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = event.when + datetime.timedelta(days=1)
    description = (event.when_detail + "\n" if event.when_detail else "") + event.details
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//librus-sync//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{event.key}@librus-sync",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{event.when:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SUMMARY:{_esc(f'{event.account} - {event.what}')}",
        f"DESCRIPTION:{_esc(description)}",
        f"SEQUENCE:{sequence}",
        f"STATUS:{'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'}",
        "TRANSP:TRANSPARENT",
        f"ORGANIZER;CN=Librus:mailto:{sender}",
        *[f"ATTENDEE;CN={a};ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:{a}" for a in attendees],
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


def build_message(change: Change, sender: str, to: List[str], test: bool = False) -> EmailMessage:
    action, event, sequence = change
    cancel = action == "cancel"
    prefix = ("[TEST] " if test else "") + {"new": "", "update": "Updated: ", "cancel": "Cancelled: "}[action]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = f"{prefix}[{event.account}] {event.when:%a %d.%m} {event.what}"
    when = f"{event.when:%A, %Y-%m-%d}" + (f" ({event.when_detail})" if event.when_detail else "")
    body = (
        f"{'This event was removed from Librus.' + chr(10) if cancel else ''}"
        f"{'This event was changed in Librus.' + chr(10) if action == 'update' else ''}"
        f"What:    {event.account} - {event.what}\n"
        f"When:    {when}\n"
        f"Details:\n{event.details or '-'}\n"
    )
    if not cancel:
        body += f"\nAdd to Google Calendar: {gcal_link(event)}\n"
    msg.set_content(body)
    ics = build_ics(event, sender, to, "CANCEL" if cancel else "REQUEST", sequence)
    params = {"method": "CANCEL" if cancel else "REQUEST"}
    msg.add_alternative(ics, subtype="calendar", charset="utf-8", params=params)
    msg.add_attachment(ics, subtype="calendar", charset="utf-8", params=params, filename="event.ics")
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=pathlib.Path, help="default: config.yml, then config.yaml")
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    parser.add_argument("--days", type=int, default=21, help="look-ahead window")
    parser.add_argument("--send", action="store_true", help="actually send emails and update state (default: dry run)")
    parser.add_argument(
        "--outbox", type=pathlib.Path, nargs="?", const=HERE / "outbox",
        help="write each email as an .eml file into DIR (default: outbox/) instead of sending; state is untouched",
    )
    parser.add_argument(
        "--test-to", metavar="ADDR",
        help="send ONE notification (the first pending change, see --test-index) to ADDR only; state is untouched",
    )
    parser.add_argument("--test-index", type=int, default=1, help="which pending change --test-to sends (1-based)")
    parser.add_argument("--baseline", action="store_true", help="mark all current events as seen without emailing")
    parser.add_argument("--force", action="store_true", help="allow cancelling when Librus returned no events at all")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config(find_config(args.config))
    exclude = cfg.get("exclude", DEFAULT_EXCLUDE)
    recipients = {acc["name"]: acc["recipients"] for acc in cfg["accounts"]}

    fetched = fetch_upcoming(cfg["accounts"], args.days)
    events = [e for e in fetched if keep(e, exclude)]
    state = load_state(args.state)

    if args.baseline:
        for e in events:
            if e.key not in state:
                record(state, e, 0)
        save_state(args.state, state)
        logger.info("Baseline saved: %d events marked as seen", len(events))
        return

    changes = [c for c in diff(events, state, args.days) if c[1].account in recipients]
    counts = {a: sum(1 for c in changes if c[0] == a) for a in ("new", "update", "cancel")}
    logger.info("%d fetched, %d after filter: %s", len(fetched), len(events), counts)

    if counts["cancel"] and not events and not args.force:
        raise SystemExit("Librus returned no events but cancellations are pending; refusing (use --force).")

    sender = cfg["email"]["sender"]
    if args.test_to:
        if not 1 <= args.test_index <= len(changes):
            raise SystemExit(f"--test-index {args.test_index} out of range ({len(changes)} pending change(s))")
        change = changes[args.test_index - 1]
        with Mailer(cfg["email"]) as mailer:
            mailer.send(build_message(change, sender, [args.test_to], test=True))
        logger.info("TEST sent to %s: %s [%s] %s (state untouched)", args.test_to, change[0], change[1].account, change[1].what)
        return

    if args.outbox:
        args.outbox.mkdir(parents=True, exist_ok=True)
        for i, change in enumerate(changes, 1):
            action, e, _ = change
            path = args.outbox / f"{i:02d}_{action}_{e.when:%Y-%m-%d}_{e.account}.eml"
            path.write_bytes(bytes(build_message(change, sender, recipients[e.account])))
        logger.info("Wrote %d .eml file(s) to %s", len(changes), args.outbox)
        return

    if not args.send:
        for action, e, seq in changes:
            print(f"{action.upper():6} [{e.account}] {e.what}  (seq {seq})")
            print(f"  to:      {', '.join(recipients[e.account])}")
            print(f"  when:    {e.when:%Y-%m-%d} {e.when_detail}\n  details: {e.details or '-'}\n")
        print(f"DRY RUN: {len(changes)} email(s) would be sent")
        return

    if not changes:
        return
    with Mailer(cfg["email"]) as mailer:
        for change in changes:
            action, e, seq = change
            mailer.send(build_message(change, sender, recipients[e.account]))
            record(state, e, seq, cancelled=action == "cancel")
            save_state(args.state, state)  # per email, so a crash never re-sends
            logger.info("Sent %s: [%s] %s %s", action, e.account, e.when, e.what)


if __name__ == "__main__":
    main()
