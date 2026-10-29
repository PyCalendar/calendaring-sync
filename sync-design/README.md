<!--
SPDX-FileCopyrightText: 2026 calendaring-sync contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Sync state design

Evidence for these pages on `main`:

- [`docs/explanation/design.rst`](https://github.com/PyCalendar/calendaring-sync/blob/main/docs/explanation/design.rst)
- [`docs/explanation/adapters.rst`](https://github.com/PyCalendar/calendaring-sync/blob/main/docs/explanation/adapters.rst)
- [`docs/reference/server-compatibility.rst`](https://github.com/PyCalendar/calendaring-sync/blob/main/docs/reference/server-compatibility.rst)

Tested between 2026-10-10 and 2026-10-12 with Radicale 3.8.3, Xandikos 0.4.8, Cyrus 3.13.7, Stalwart 0.16.21 (and `latest` for JMAP capabilities), Baïkal 0.10.1, python-caldav `master` at 1a75f45, and calendaring-jmap `main` at 004c6b4. The Stalwart logs in `logs/` are from 0.16.21. `logs/stalwart-1.0.1/` holds the same probes run on 2026-10-29 against Stalwart 1.0.1 from calendaring-sync's own fixture, with python-caldav 3.6.0 and calendaring-jmap `main` at 21a3f25; the `stalwart` section of `run.sh` now writes there. `jmap-instances-cyrus.log` and `fingerprint-jmap-cyrus.log` were run the same day against the same Cyrus image.

## Logs

| Log | Backs |
|---|---|
| `caldav-sync-<server>.log` | The CalDAV rows of the server compatibility page |
| `jmap-sync-<server>.log` | The JMAP rows of the server compatibility page |
| `caldav-client-<server>.log` | What python-caldav does with the same responses and with conditional writes |
| `jmap-client-<server>.log` | What calendaring-jmap does with the same responses |
| `expiry-*.log` | How servers answer an expired token or state |
| `listing-cap-stalwart.log` | A full listing of a calendar larger than Stalwart's result limit |
| `fingerprint-<server>.log`, `fingerprint-jmap-<server>.log` | A server's copy of a write fingerprints the same as what was sent, once what servers rewrite is left out; the JMAP logs use the JMAP adapter's own fingerprint |
| `jmap-instances-<server>.log` | Events that share a `uid`, and how a refused `calendarIds` reads |
| `retry-radicale.log` | An expired token and a real refusal, told apart |
| `unsupported-sync-collection.log` | python-caldav on a server without `sync-collection` |
| `jmap-capabilities-stalwart-latest.log` | JMAP Tasks on the latest Stalwart image |
| `model.log` | The design's rules, checked on a SQLite model |
| `citations.log` | Every cited section exists and every quoted phrase is in it |

The probes send the calendar data in `fixtures/`; `event-rich.ics` adds a time zone, a recurrence with an override, an alarm, attendees, folded lines and non-ASCII text. Each log has the requests and responses, then one `RESULT` line per check. `server-*.log` files are server output. [`sources.md`](sources.md) links the server and library code behind the findings.

## Reproduce

Requirements: Python 3.11+ with `requests`, python-caldav and calendaring-jmap from their default branches, `radicale==3.8.3` and `xandikos==0.4.8`; Docker; Bash and curl; checkouts of [calendaring-jmap](https://github.com/PyCalendar/calendaring-jmap) (for its Cyrus fixture) and calendaring-sync, installed in that Python (for its Stalwart fixture, the JMAP fingerprint and the citation check).

```sh
PYTHON=/path/to/python CALENDARING_JMAP=/path/to/calendaring-jmap CALENDARING_SYNC=/path/to/calendaring-sync ./run.sh
```

Pass section names to run part of it: `radicale`, `xandikos`, `radicale-expiry`, `unsupported`, `cyrus`, `stalwart`, `stalwart-latest`, `baikal`, `model`, `citations`. Every section stops its server when done.

`servers/stalwart_admin.py` expires a Stalwart token the way Stalwart's own purge test does, and lowers its result limit for the listing-cap probe. Its settings reload fails on the fixture's default spam-filter entries, so the script deletes those first, inside the throwaway container.
