# librus-sync

Reads the Librus Synergia accounts of the children and tells their parents about what matters,
without ever marking anything as read.

- **Calendar events** (tests, quizzes, homework, deadlines) arrive as `.ics` invitations, so they
  land in Google Calendar by themselves. They are kept in step afterwards: an edit becomes an
  update, a removal a cancellation, and a move to another day or lesson a single "Rescheduled"
  update. Events get a time when it is certain (an explicit range, or the lesson's bell time when
  the timetable agrees on the subject), otherwise they stay all-day.
- **Unread messages** produce a short digest (sender, subject, date) to the parents only. The text
  is never fetched, so the messages stay unread in Librus.
- **Breakage** is reported to an admin address: Librus has no public API, so this is a scraper and
  will stop working sooner or later.

It replaces `librus-notifier`, which marked messages as read by opening them.

## How it works

```
fetch (per account) -> filter -> diff against state.json -> email
```

- Only the calendar pages and the inbox *list* are read. Opening a single message would mark it as
  read, so no code path does it (see `INBOX_LIST_URL` in `sync.py`).
- `state.json` remembers what was already sent (one entry per event, with a revision number) and
  which messages were already announced. Events keep a stable calendar UID, so a re-sent invitation
  updates the existing entry instead of duplicating it.
- Accounts are isolated: one failing account never blocks the others, and its events are neither
  sent nor cancelled while it is failing. Failures alert the admin on the first occurrence, at most
  once a day while they persist, and once on recovery. A run with a failure exits non-zero.
- A fresh state records a baseline instead of sending, so a new deployment never re-mails what the
  family already received.

## Configuration

Copy [`config.example.yml`](config.example.yml) to `config.yml` and fill it in. `config.yml` and
`config.yaml` are gitignored; never commit real data.

| Key | Meaning |
| --- | --- |
| `accounts[].recipients` | who gets calendar emails (parents and children) |
| `accounts[].message_recipients` | who gets message digests; parents only. Omit it and that account gets no digest |
| `email` | SMTP settings and the sender address |
| `admin` | receives failure and recovery alerts |
| `exclude` | optional; words that drop calendar entries (default: absences) |

Any credential may be a literal value or a `*_env` reference to an environment variable; the
deployed config uses only references.

## Running locally

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Scripts/ on Windows, bin/ elsewhere
.venv/Scripts/python sync.py                                # dry run: prints what would be sent
```

A plain run is a dry run and changes nothing. Useful options:

| Option | Effect |
| --- | --- |
| `--send` | send the emails and update `state.json` |
| `--outbox [DIR]` | write the emails as `.eml` files instead of sending |
| `--baseline` | mark everything currently visible as seen, send nothing |
| `--test-to ADDR` | send one pending calendar email to `ADDR` only; state untouched |
| `--test-digest ADDR` | send sample message digests to `ADDR` only; state untouched |
| `--test-alert ADDR` | send a sample failure alert to `ADDR` |
| `--send-unread` | one-off: send every account's full unread digest to its `message_recipients`; state untouched |
| `--days N` | look-ahead window for events (default 21) |
| `--config PATH`, `--state PATH` | alternative config and state files |

Do not run `--send` locally once the deployed job is live: each side keeps its own state, so both
would email the same new event.

## Deployment

The service is deployed to the production VPS with [vps-deployer](../vps-deployer), driven by the
desired-state manifests in [service-infra](../service-infra). This repository only holds the
application and its entrypoint in `.deployer/`; everything about the host lives in `service-infra`.

| Piece | Where |
| --- | --- |
| Application and entrypoint | this repo: `sync.py`, `.deployer/run.sh`, `.deployer/expect.yaml`, `.deployer/ignore.txt` |
| Deployment manifest (`librus-sync-prod`) | `service-infra/deployments/librus-sync/prod.yaml` |
| Production config (no secrets) | `service-infra/configs/librus-sync/prod.yaml`, installed as `config/production.yaml` |
| Secrets | Windows User environment variables, referenced from the manifest (see `service-infra/docs/secrets.md`) |

It runs as a systemd oneshot on a timer, at 07:30, 13:30 and 19:30 Europe/Warsaw (up to five
minutes of jitter). Each run builds (or reuses) a virtual environment under
`/var/lib/librus-sync-prod`, which also holds `state.json`, then runs `sync.py --send`. When there is
no state yet it records a baseline instead.

The deployer builds the release from the working tree of this repository, so commit before
applying. With `vps-deployer` installed (`pipx install vps-deployer`, 1.3.1 or newer) and
`PROJECTS_DIR`, `SERVICE_INFRA_DIR` and the secret variables set:

```powershell
vps-deployer validate
vps-deployer secrets check librus-sync-prod
vps-deployer plan  librus-sync-prod     # review it
vps-deployer apply librus-sync-prod
vps-deployer status librus-sync-prod    # healthy; shows the last job's result
vps-deployer logs   librus-sync-prod
```

A repeated `plan` after a successful `apply` reports "No changes". `status` reads "unhealthy" while
the working tree differs from the active release, which just means a deploy is pending.

## Dependencies

- [`librus`](https://pypi.org/project/librus/) 0.0.15 for parsing the calendar and timetable pages.
  Its own login no longer works, so it is only used for parsing.
- [`librus_pyapi`](https://github.com/emsi/librus_pyapi), pinned to a commit tarball in
  `requirements.txt`, for the current login flow and the inbox. AGPL-licensed.
- `tzdata` for Warsaw time, PyYAML for the config.

## Caveats

- This is unofficial scraping of a service that can change without notice. When it does, the admin
  gets an alert and the affected account pauses, rather than sending wrong information.
- A timed event assumes the nominal bell schedule; a shortened school day is not reflected.
- Delivery of invitations into Google Calendar depends on the recipient's calendar settings
  ("add invitations to my calendar"). Every calendar email also carries an "Add to Google Calendar"
  link.
