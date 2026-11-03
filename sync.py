#!/usr/bin/env python3
"""
Email upcoming Librus calendar events as .ics invitations, keep Google
Calendar in step when they change or disappear, and tell the admin when the
scraper stops working.

Read-only towards Librus: only calendar (terminarz) pages are fetched, so no
message is ever marked as read. Pipeline:

    fetch (per account) -> filter (drop absences etc.) -> diff against state -> email

The diff yields three kinds of mail, all keyed by a stable UID so the
calendar updates the same entry instead of duplicating it:
    new     METHOD:REQUEST, SEQUENCE 0
    update  METHOD:REQUEST, SEQUENCE+1   (description/teacher/room/... changed, or an all-day
                                          entry gained a confident time; never the reverse)
    cancel  METHOD:CANCEL,  SEQUENCE+1   (gone from Librus, still in the window)
    move    METHOD:REQUEST, SEQUENCE+1   (same event on a new date/lesson, old UID kept)

Librus has no public API, so scraping will break eventually. A failing account
is isolated (the others still run, and its events are never cancelled), and
the admin is emailed on first failure, again at most daily, and on recovery.

Unread inbox messages are announced in a digest (sender, subject, date - never the text). Only the
inbox LIST endpoint is used: opening a message would mark it as read, so no code path may fetch one.

Each event is reduced to what / when / details. Without --send nothing is
emailed and the state is not touched (dry run).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import logging
import os
import pathlib
import re
import smtplib
import ssl
import sys
import unicodedata
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import librus
import requests_html
import yaml
from librus_pyapi import LibrusAPI

logger = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_CANDIDATES = [HERE / "config.yml", HERE / "config.yaml"]
DEFAULT_STATE = HERE / "state.json"
CALENDAR_URL = "https://synergia.librus.pl/terminarz"
# The ONLY messages endpoint this tool may call. Fetching a single message (.../messages/<id>)
# marks it as read; the list below does not (verified: unread counts stay unchanged).
INBOX_LIST_URL = "https://wiadomosci.librus.pl/api/inbox/messages"
MAX_MESSAGE_PAGES = 20
# Categories/subjects containing any of these (case-insensitive) are skipped.
DEFAULT_EXCLUDE = ["nieobecn", "absen"]
# An account returning nothing while at least this many tracked events are
# upcoming is treated as a broken scraper, not as "everything was removed".
SUSPICIOUS_EMPTY_MIN_TRACKED = 3
REALERT_AFTER = datetime.timedelta(hours=24)
MOVE_WINDOW_DAYS = 21  # a removed + a new event this close in time may be one rescheduled event
WARSAW = ZoneInfo("Europe/Warsaw")
RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


class ScrapeError(Exception):
    """Librus answered, but not with the page we expect (access denied, layout changed)."""

    def __init__(self, message: str, snippet: str = "") -> None:
        super().__init__(message)
        self.snippet = snippet


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
    # Derived, Warsaw wall-clock "HH:MM"; empty = all-day. Deliberately outside key and
    # content_hash, so adding or refining times never triggers 'Updated:' mails.
    start: str = ""
    end: str = ""
    # Calendar UID when it differs from `key` (a rescheduled event keeps the UID it was invited under).
    uid: str = ""
    # Transient, set only on a detected move; never persisted.
    moved_from: str = ""
    was: str = ""

    @property
    def what(self) -> str:
        return " ".join(p for p in [self.subject, f"({self.category})" if self.category else ""] if p) or "(no title)"

    @property
    def when_detail(self) -> str:
        timing = self.interval or (f"{self.start}-{self.end}" if self.start else "")
        return ", ".join(p for p in [f"lesson {self.lesson}" if self.lesson else "", timing] if p)

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

    @property
    def calendar_uid(self) -> str:
        return self.uid or self.key

    def snapshot(self) -> Dict[str, str]:
        data = {**asdict(self), "when": self.when.isoformat()}
        data.pop("moved_from")
        data.pop("was")
        return data

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
    if not accounts:
        raise SystemExit(f"No accounts in {path}")
    for acc in accounts:
        if not acc["recipients"]:
            raise SystemExit(f"Account {acc['name']} has no recipients")
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


def login(username: str, password: str) -> Tuple[LibrusAPI, requests_html.HTMLSession]:
    """Log in with librus_pyapi (current auth flow); hand the cookies to an HTML session for scraping."""
    api = LibrusAPI(username, password)
    html = requests_html.HTMLSession()
    html.cookies.update(api.session.cookies)
    return api, html


def _snippet(page: Any) -> str:
    return re.sub(r"\s+", " ", page.html.text or "").strip()[:300]


def check_calendar_page(page: Any) -> None:
    """Distinguish 'no events' from 'page is not the calendar'."""
    if "Brak dostępu" in page.text:
        raise ScrapeError("access denied (Librus answered 'Brak dostępu')", _snippet(page))
    if not page.html.find("table.kalendarz") or not page.html.find(".kalendarz-dzien"):
        raise ScrapeError("calendar markup not found (layout changed?)", _snippet(page))


def _hhmm(h: str, m: str) -> str:
    return f"{int(h):02d}:{m}"


def parse_range(text: str) -> Optional[Tuple[str, str]]:
    """'09:45 - 10:30' -> ('09:45', '10:30'); None unless a valid forward range."""
    m = RANGE_RE.fullmatch((text or "").strip())
    if not m:
        return None
    start, end = _hhmm(m[1], m[2]), _hhmm(m[3], m[4])
    return (start, end) if start < end else None


def _words(text: str) -> List[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = re.sub(r"[^a-z0-9 ]", " ", text).split()
    return [w for w in words if w not in ("jezyk", "j")]  # shared by every language subject


def same_subject(a: str, b: str) -> bool:
    """Strict on purpose: a wrong match would put an event at the wrong hour."""
    return any(x[:4] == y[:4] for x in _words(a) for y in _words(b) if len(x) >= 4 and len(y) >= 4)


def sane_bells(table: Dict[str, str]) -> bool:
    """Lessons parse, run in order without overlap, and last about 45 minutes."""
    minutes = lambda s: int(s[:2]) * 60 + int(s[3:])
    prev_end = ""
    for idx in sorted(table, key=lambda k: int(k) if k.isdigit() else 99):
        rng = parse_range(table[idx])
        if not rng or not 35 <= minutes(rng[1]) - minutes(rng[0]) <= 60 or rng[0] < prev_end:
            return False
        prev_end = rng[1]
    return bool(table)


def fetch_timing(http: requests_html.HTMLSession) -> Dict[str, Any]:
    """Bell table (lesson -> time) and template-week grid (weekday|lesson -> subjects). Raises if unusable."""
    session = librus.LibrusSession()
    session._html_session = http
    table: Dict[str, str] = {}
    grid: Dict[str, List[str]] = {}
    for lesson in session.schedule():
        rng = parse_range(lesson.time)
        if rng:
            table[lesson.index] = f"{rng[0]}-{rng[1]}"
        grid.setdefault(f"{lesson.day}|{lesson.index}", []).append(lesson.name)
    if not sane_bells(table):
        raise ValueError(f"bell table failed sanity check: {table}")
    return {"table": table, "grid": grid}


def apply_timing(events: List[Event], timing: Optional[Dict[str, Any]]) -> None:
    """Explicit range first; else the lesson's bell time if the timetable agrees on the subject; else all-day."""
    for e in events:
        rng = parse_range(e.interval)
        if not rng and timing and e.lesson in timing["table"]:
            subjects = timing["grid"].get(f"{e.when.weekday()}|{e.lesson}", [])
            if any(same_subject(e.subject, s) for s in subjects):
                rng = parse_range(timing["table"][e.lesson])
        if rng:
            e.start, e.end = rng


def fetch_account(
    acc: Dict[str, Any], days: int, http: requests_html.HTMLSession, cached: Optional[Dict[str, Any]] = None
) -> Tuple[List[Event], Optional[Dict[str, Any]]]:
    """Events of one account within the window, plus a fresh timing table if it could be read.

    Raises on any calendar scraping problem. A timetable problem is not fatal: timing falls back to
    the cached table, or to all-day events.
    """
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    page = http.get(CALENDAR_URL)
    page.raise_for_status()
    check_calendar_page(page)

    events: List[Event] = []
    for (event_id,) in page.html.search_all("szczegoly/{}'"):
        details = http.get(f"{CALENDAR_URL}/szczegoly/{event_id}")
        details.raise_for_status()
        try:
            exam = librus.LibrusSession._parse_exam(details.html.find("table.decorated.medium"))
        except Exception as exc:  # parser asserts/IndexErrors mean the layout changed
            raise ScrapeError(f"could not parse event {event_id}: {type(exc).__name__}: {exc}", _snippet(details)) from exc
        event = to_event(acc["name"], exam)
        if event and today <= event.when <= horizon:
            events.append(event)

    fresh: Optional[Dict[str, Any]] = None
    try:
        fresh = fetch_timing(http)
    except Exception as exc:
        logger.warning(
            "%s: timetable unusable (%s: %s); %s", acc["name"], type(exc).__name__, exc,
            "using cached bell table" if cached else "events stay all-day",
        )
    apply_timing(events, fresh or cached)
    return events, fresh


@dataclass
class Msg:
    """What a digest may show. Deliberately has no body field."""

    id: str
    sender: str
    subject: str
    sent: str


def fetch_unread(api: LibrusAPI) -> List[Msg]:
    """Unread inbox messages via the list endpoint only. Never reads the 'content' snippet it returns."""
    out: List[Msg] = []
    returned = 0
    for page in range(1, MAX_MESSAGE_PAGES + 1):
        response = api.session.get(INBOX_LIST_URL, params={"unreadOnly": 1, "page": page, "limit": 50})
        response.raise_for_status()
        try:
            body = response.json()
            items, total = body["data"], int(body["total"])
            returned += len(items)
            for item in items:
                if item.get("readDate") is None:  # belt and braces in case 'unreadOnly' is ever ignored
                    out.append(Msg(str(item["messageId"]), item.get("senderName") or "", item.get("topic") or "",
                                   item.get("sendDate") or ""))
        except (ValueError, KeyError, TypeError) as exc:
            raise ScrapeError(f"inbox list has an unexpected shape ({type(exc).__name__}: {exc})", response.text[:200]) from exc
        if not items or returned >= total:
            return out
    return out


# name -> (error message, page snippet)
Failures = Dict[str, Tuple[str, str]]


def fetch_all(
    accounts: List[Dict[str, Any]], days: int, cache: Dict[str, Any]
) -> Tuple[Dict[str, List[Event]], Failures, Dict[str, Any], Dict[str, List[Msg]]]:
    """Per-account isolation: one broken account never blocks the others, and a broken inbox never blocks the calendar."""
    by_account: Dict[str, List[Event]] = {}
    failures: Failures = {}
    fresh_timing: Dict[str, Any] = {}
    unread: Dict[str, List[Msg]] = {}
    for acc in accounts:
        name = acc["name"]
        try:
            api, http = login(acc["username"], acc["password"])
        except Exception as exc:
            failures[name] = (f"{type(exc).__name__}: {exc}", "")
            logger.error("Account %s failed: %s", name, failures[name][0])
            continue
        try:
            by_account[name], fresh = fetch_account(acc, days, http, cache.get(name))
            if fresh:
                fresh_timing[name] = fresh
        except ScrapeError as exc:
            failures[name] = (str(exc), exc.snippet)
        except Exception as exc:
            failures[name] = (f"{type(exc).__name__}: {exc}", "")
        if name in failures:
            logger.error("Account %s failed: %s", name, failures[name][0])
        try:
            unread[name] = fetch_unread(api)
        except ScrapeError as exc:
            failures[f"{name} messages"] = (str(exc), exc.snippet)
        except Exception as exc:
            failures[f"{name} messages"] = (f"{type(exc).__name__}: {exc}", "")
        if f"{name} messages" in failures:
            logger.error("Inbox of %s failed: %s", name, failures[f"{name} messages"][0])
    return by_account, failures, fresh_timing, unread


# --- filter -----------------------------------------------------------------

def keep(event: Event, exclude: List[str]) -> bool:
    haystack = f"{event.category} {event.subject}".lower()
    return not any(word.lower() in haystack for word in exclude)


# --- state + diff -----------------------------------------------------------

# state.json: {"events": {key: {"seq", "hash", "cancelled", "sent", "event": snapshot}},
#              "health": {account: {"failures", "since", "error", "last_alert"}},
#              "messages": {account: {message id: iso time first seen unread}},
#              "checked": {account: iso time of the last inbox check that was fully handled},
#              "timing": {account: {"table": {lesson: "HH:MM-HH:MM"}, "grid": {"weekday|lesson": [subjects]}}}}
Doc = Dict[str, Dict[str, Any]]


def load_state(path: pathlib.Path) -> Doc:
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"events": raw.get("events", {}), "health": raw.get("health", {}), "timing": raw.get("timing", {}), "messages": raw.get("messages", {}), "checked": raw.get("checked", {})}


def save_state(path: pathlib.Path, doc: Doc) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record(events_state: Dict[str, Any], event: Event, seq: int, cancelled: bool = False) -> None:
    events_state[event.key] = {
        "seq": seq,
        "hash": event.content_hash,
        "cancelled": cancelled,
        "sent": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event.snapshot(),
    }


# (action, event, sequence); action in "new" | "update" | "cancel" | "move"
Change = Tuple[str, Event, int]


def tracked_upcoming(events_state: Dict[str, Any], account: str, days: int) -> int:
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    count = 0
    for entry in events_state.values():
        snap = entry["event"]
        if snap["account"] == account and not entry["cancelled"]:
            if today <= datetime.date.fromisoformat(snap["when"]) <= horizon:
                count += 1
    return count


def pair_moves(changes: List[Change]) -> List[Change]:
    """A removed and a new event of the same account/subject/category, close in time, are one move.

    Only unambiguous pairs: with two candidates on either side nothing is paired (cancel + new stays),
    because guessing would reschedule the wrong entry.
    """
    groups: Dict[Tuple[str, str, str], Tuple[List[Change], List[Change]]] = collections.defaultdict(lambda: ([], []))
    for change in changes:
        action, e, seq = change
        if action == "cancel":
            groups[(e.account, e.subject, e.category)][0].append(change)
        elif action == "new" and seq == 0:  # seq > 0 means it reappeared, not that it is a fresh event
            groups[(e.account, e.subject, e.category)][1].append(change)

    paired: set = set()
    moves: List[Change] = []
    for cancels, news in groups.values():
        if len(cancels) != 1 or len(news) != 1:
            continue
        (_, old, seq), (_, new, _) = cancels[0], news[0]
        if abs((new.when - old.when).days) > MOVE_WINDOW_DAYS:
            continue
        new.uid = old.calendar_uid
        new.moved_from = old.key
        new.was = f"{old.when:%a %d.%m}" + (f", {old.when_detail}" if old.when_detail else "")
        moves.append(("move", new, seq))
        paired.update({id(cancels[0]), id(news[0])})
    return [c for c in changes if id(c) not in paired] + moves


def diff(events: List[Event], events_state: Dict[str, Any], days: int, skip: Optional[set] = None) -> List[Change]:
    """`skip` holds accounts whose fetch failed: nothing is concluded about them."""
    skip = skip or set()
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    changes: List[Change] = []
    current = {e.key: e for e in events if e.account not in skip}

    for key, e in current.items():
        entry = events_state.get(key)
        if entry is None:
            changes.append(("new", e, 0))
        elif entry["cancelled"]:
            e.uid = entry["event"].get("uid", "")
            changes.append(("new", e, entry["seq"] + 1))  # reappeared: re-issue with a newer sequence
        elif entry["hash"] != e.content_hash or (e.start and not entry["event"].get("start")):
            old = entry["event"]
            e.uid = old.get("uid", "")
            if old.get("start") and e.interval == old.get("interval", ""):
                # Times derive from the lesson: once sent, keep them. An edited explicit range is real news.
                e.start, e.end = old["start"], old.get("end", "")
            if entry["hash"] == e.content_hash:
                e.was = "all-day"  # nothing changed in Librus; the event merely gained a time
            changes.append(("update", e, entry["seq"] + 1))

    for key, entry in events_state.items():
        if key in current or entry["cancelled"] or entry["event"]["account"] in skip:
            continue
        old = Event.from_snapshot(entry["event"])
        if today <= old.when <= horizon:  # only judge events the fetch could actually have seen
            changes.append(("cancel", old, entry["seq"] + 1))
    return pair_moves(changes)


# --- message digest ---------------------------------------------------------

def plan_digests(unread: Dict[str, List[Msg]], msgs_state: Dict[str, Any]) -> Tuple[List[Tuple[str, List[Msg]]], List[str]]:
    """New unread messages per account. An account never seen before is baselined silently, not announced."""
    digests: List[Tuple[str, List[Msg]]] = []
    first_seen: List[str] = []
    for name, msgs in unread.items():
        seen = msgs_state.get(name)
        if seen is None:
            first_seen.append(name)
            continue
        new = [m for m in msgs if m.id not in seen]
        if new:
            digests.append((name, new))
    return digests, first_seen


def settle_messages(doc: Doc, name: str, msgs: List[Msg], now: datetime.datetime) -> None:
    """Remember what is currently unread (keeps first-seen times, forgets messages that were read meanwhile)
    and stamp the check time that the next digest's 'since' refers to."""
    stamp = now.isoformat(timespec="seconds")
    known = doc["messages"].get(name, {})
    doc["messages"][name] = {m.id: known.get(m.id, stamp) for m in msgs}
    doc["checked"][name] = stamp


def digest_subject(account: str, new: List[Msg], since: Optional[str]) -> str:
    """One message: who and what. Several: how many, since the last check."""
    if len(new) == 1:
        topic = new[0].subject or "(no subject)"
        topic = topic if len(topic) <= 80 else topic[:79] + "…"
        return f'[{account}] New message from {new[0].sender or "(unknown)"}: "{topic}"'
    when = f" since {datetime.datetime.fromisoformat(since):%d.%m %H:%M}" if since else ""
    return f"[{account}] {len(new)} new messages{when}"


def build_digest(account: str, new: List[Msg], sender: str, to: List[str], since: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = digest_subject(account, new, since)
    lines = [f"{account} has {len(new)} new unread message{'s' if len(new) != 1 else ''} in Librus:", ""]
    lines += [f"- From: {m.sender or '(unknown)'}\n  Subject: {m.subject or '(no subject)'}\n  Sent: {m.sent[:16]}" for m in new]
    lines += ["", "The messages were not opened, so they are still unread in Librus."]
    msg.set_content("\n".join(lines) + "\n")
    return msg


# --- alerts -----------------------------------------------------------------

@dataclass
class Alert:
    kind: str            # "failing" | "recovered"
    account: str
    error: str
    snippet: str
    failures: int
    since: str


def plan_alerts(health: Dict[str, Any], failures: Failures, ok: List[str], now: datetime.datetime) -> List[Alert]:
    """Alert on first failure, at most daily while it persists, and once on recovery."""
    alerts: List[Alert] = []
    for account, (error, snippet) in failures.items():
        entry = health.get(account)
        last = datetime.datetime.fromisoformat(entry["last_alert"]) if entry and entry.get("last_alert") else None
        count = (entry["failures"] if entry else 0) + 1
        since = entry["since"] if entry else now.isoformat(timespec="seconds")
        if last is None or now - last >= REALERT_AFTER:
            alerts.append(Alert("failing", account, error, snippet, count, since))
    for account in ok:
        entry = health.get(account)
        if entry:
            alerts.append(Alert("recovered", account, entry.get("error", ""), "", entry["failures"], entry["since"]))
    return alerts


def note_failures(health: Dict[str, Any], failures: Failures, now: datetime.datetime) -> None:
    for account, (error, _) in failures.items():
        entry = health.setdefault(account, {"failures": 0, "since": now.isoformat(timespec="seconds")})
        entry["failures"] += 1
        entry["error"] = error


def scrub(text: str, secrets: List[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def build_alert(alert: Alert, sender: str, admin: str, secrets: List[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = admin
    if alert.kind == "recovered":
        msg["Subject"] = f"[librus-sync] RECOVERED: {alert.account}"
        body = (
            f"Account {alert.account} works again.\n"
            f"It had failed {alert.failures} consecutive run(s) since {alert.since}.\n"
            f"Last error was: {alert.error}\n"
        )
    else:
        msg["Subject"] = f"[librus-sync] FAILING: {alert.account}"
        body = (
            f"Account {alert.account} could not be read from Librus.\n\n"
            f"Error:   {alert.error}\n"
            f"Since:   {alert.since} ({alert.failures} consecutive run(s))\n"
        )
        if alert.snippet:
            body += f"\nPage said (first 300 chars):\n  {alert.snippet}\n"
        body += (
            "\nWhile this lasts, the account's events are neither sent nor cancelled; "
            "other accounts keep working. Likely causes: Librus changed its login or the "
            "calendar layout, or the credentials changed. You get at most one reminder a day.\n"
        )
    msg.set_content(scrub(body, secrets))
    return msg


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


def _utc(day: datetime.date, hhmm: str) -> str:
    """Warsaw wall-clock time -> UTC iCalendar stamp (DST handled by zoneinfo)."""
    local = datetime.datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=WARSAW)
    return local.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
        f"UID:{event.calendar_uid}@librus-sync",
        f"DTSTAMP:{stamp}",
        *(
            [f"DTSTART:{_utc(event.when, event.start)}", f"DTEND:{_utc(event.when, event.end)}"]
            if event.start
            else [f"DTSTART;VALUE=DATE:{event.when:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
        ),
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
        "dates": (
            f"{_utc(event.when, event.start)}/{_utc(event.when, event.end)}"
            if event.start
            else f"{event.when:%Y%m%d}/{end:%Y%m%d}"
        ),
        "details": (event.when_detail + "\n" if event.when_detail else "") + event.details,
    }
    if event.classroom:
        params["location"] = event.classroom
    return "https://calendar.google.com/calendar/render?" + urlencode(params, quote_via=quote)


def build_message(change: Change, sender: str, to: List[str], test: bool = False) -> EmailMessage:
    action, event, sequence = change
    cancel = action == "cancel"
    prefix = ("[TEST] " if test else "") + {"new": "", "update": "Updated: ", "cancel": "Cancelled: ", "move": "Rescheduled: "}[action]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = f"{prefix}[{event.account}] {event.when:%a %d.%m} {event.what}"
    when = f"{event.when:%A, %Y-%m-%d}" + (f" ({event.when_detail})" if event.when_detail else "")
    body = (
        f"{'This event was removed from Librus.' + chr(10) if cancel else ''}"
        f"{('This event now has a time (it was an all-day entry).' if event.was else 'This event was changed in Librus.') + chr(10) if action == 'update' else ''}"
        f"{'This event was moved in Librus.' + chr(10) + 'Was:     ' + event.was + chr(10) if action == 'move' else ''}"
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
    parser.add_argument("--test-alert", metavar="ADDR", help="send a sample FAILING alert to ADDR and exit; state is untouched")
    parser.add_argument("--baseline", action="store_true", help="mark all current events as seen without emailing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config(find_config(args.config))
    exclude = cfg.get("exclude", DEFAULT_EXCLUDE)
    recipients = {acc["name"]: acc["recipients"] for acc in cfg["accounts"]}
    secrets = [s for acc in cfg["accounts"] for s in (acc["username"], acc["password"])]
    sender = cfg["email"]["sender"]
    admin = cfg.get("admin")

    if args.test_alert:
        sample = Alert("failing", "TEST", "sample error: this is only a test of the alert path",
                       "Brak dostępu (sample page text)", 1, datetime.datetime.now().isoformat(timespec="seconds"))
        with Mailer(cfg["email"]) as mailer:
            mailer.send(build_alert(sample, sender, args.test_alert, secrets))
        logger.info("Sample alert sent to %s", args.test_alert)
        return

    now = datetime.datetime.now()
    doc = load_state(args.state)
    events_state, health = doc["events"], doc["health"]
    msgs_state = doc["messages"]
    last_checked = dict(doc["checked"])  # as of the previous run, for the digest subjects
    by_account, failures, fresh_timing, unread = fetch_all(cfg["accounts"], args.days, doc["timing"])
    doc["timing"].update(fresh_timing)  # persisted by whichever save_state runs below

    # An account that suddenly returns nothing while we track upcoming events is broken, not empty.
    for name, evs in list(by_account.items()):
        tracked = tracked_upcoming(events_state, name, args.days)
        if not evs and tracked >= SUSPICIOUS_EMPTY_MIN_TRACKED:
            failures[name] = (f"calendar returned no events, but {tracked} tracked events are upcoming", "")
            del by_account[name]
            logger.error("Account %s failed: %s", name, failures[name][0])

    fetched = [e for evs in by_account.values() for e in evs]
    fetched.sort(key=lambda e: (e.when, e.account, e.what))
    events = [e for e in fetched if keep(e, exclude)]

    if args.baseline:
        if failures:
            logger.error("Baseline incomplete (%s failed); nothing saved so the next run retries.", ", ".join(failures))
            raise SystemExit(1)
        for e in events:
            if e.key not in events_state:
                record(events_state, e, 0)
        for name, msgs in unread.items():
            settle_messages(doc, name, msgs, now)
        save_state(args.state, doc)
        logger.info("Baseline saved: %d events and %d unread messages marked as seen", len(events), sum(map(len, unread.values())))
        return

    changes = [c for c in diff(events, events_state, args.days, skip=set(failures)) if c[1].account in recipients]
    digests, first_seen = plan_digests(unread, msgs_state)
    alerts = plan_alerts(health, failures, list(by_account) + [f"{n} messages" for n in unread], now)
    counts = {a: sum(1 for c in changes if c[0] == a) for a in ("new", "update", "cancel", "move")}
    logger.info("%d fetched (%d timed), %d after filter: %s; %d digest(s), %d inbox(es) to baseline; %d failing, %d alert(s)",
                len(fetched), sum(1 for e in fetched if e.start), len(events), counts, len(digests), len(first_seen),
                len(failures), len(alerts))
    if failures and not admin:
        logger.error("Accounts are failing but no 'admin' address is configured; nobody will be told.")

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
        for name, new in digests:
            (args.outbox / f"digest_{name}.eml").write_bytes(
                bytes(build_digest(name, new, sender, recipients[name], last_checked.get(name))))
        for i, alert in enumerate(alerts, 1):
            path = args.outbox / f"alert{i:02d}_{alert.kind}_{alert.account}.eml"
            path.write_bytes(bytes(build_alert(alert, sender, admin or "(no admin)", secrets)))
        logger.info("Wrote %d .eml file(s) to %s", len(changes) + len(digests) + len(alerts), args.outbox)
    elif not args.send:
        for action, e, seq in changes:
            print(f"{action.upper():6} [{e.account}] {e.what}  (seq {seq})")
            print(f"  to:      {', '.join(recipients[e.account])}")
            print(f"  when:    {e.when:%Y-%m-%d} {e.when_detail}\n  details: {e.details or '-'}\n")
        for name, new in digests:
            print(f"DIGEST {digest_subject(name, new, last_checked.get(name))}  to: {', '.join(recipients[name])}")
            for m in new:
                print(f"  - {m.sender}: {m.subject}  ({m.sent[:16]})")
        for name in first_seen:
            print(f"MESSAGES [{name}] first run: {len(unread[name])} unread would be recorded silently")
        for a in alerts:
            print(f"ALERT  {a.kind.upper()} [{a.account}] {a.error}  -> {admin}")
        print(f"DRY RUN: {len(changes)} event email(s), {len(digests)} digest(s) and {len(alerts)} alert(s) would be sent")
    else:
        note_failures(health, failures, now)
        pending = {name for name, _ in digests}
        for name, msgs in unread.items():
            if name not in pending:  # baseline silently / forget messages read meanwhile
                settle_messages(doc, name, msgs, now)
        save_state(args.state, doc)  # count failures even if sending below fails
        if changes or alerts or digests:
            with Mailer(cfg["email"]) as mailer:
                for alert in alerts:
                    if admin:
                        mailer.send(build_alert(alert, sender, admin, secrets))
                    if alert.kind == "recovered":
                        health.pop(alert.account, None)
                    elif admin:
                        health[alert.account]["last_alert"] = now.isoformat(timespec="seconds")
                    save_state(args.state, doc)
                    logger.info("Alert %s: %s", alert.kind, alert.account)
                for name, new in digests:
                    mailer.send(build_digest(name, new, sender, recipients[name], last_checked.get(name)))
                    settle_messages(doc, name, unread[name], now)
                    save_state(args.state, doc)
                    logger.info("Sent digest: [%s] %d new message(s)", name, len(new))
                for change in changes:
                    action, e, seq = change
                    mailer.send(build_message(change, sender, recipients[e.account]))
                    record(events_state, e, seq, cancelled=action == "cancel")
                    if action == "move":
                        events_state[e.moved_from]["cancelled"] = True  # its UID now lives on the new entry
                    save_state(args.state, doc)  # per email, so a crash never re-sends
                    logger.info("Sent %s: [%s] %s %s", action, e.account, e.when, e.what)

    if failures:
        raise SystemExit(1)  # the deployer's status shows the unit as failed


if __name__ == "__main__":
    main()
