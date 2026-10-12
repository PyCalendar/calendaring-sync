# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a server's copy of a write fingerprints the same as what was sent.

Usage:
  fingerprint.py caldav SERVER BASE_URL CALENDAR_PATH USER PASSWORD
  fingerprint.py jmap SERVER BASE_URL USER PASSWORD NEEDS_VERSION

CalDAV: the canonical form unfolds lines, sorts properties and parameters within each
component, sorts components and the parts of a recurrence rule (RFC 5545 section 3.3.10),
compares other values as sent, and leaves out the properties a server stamps itself.

JMAP: sorted-keys JSON without the properties the server sets, with defaults filled in for
isDraft, useDefaultAlerts and version.
"""

import hashlib
import json
import sys

from icalendar.parser import Contentline, Contentlines

from _common import DAV, JMAP, check, finish, ics, result

ICS_IGNORED = {"DTSTAMP", "LAST-MODIFIED", "PRODID"}
# Recurrence rules whose parts may come in any order (RFC 5545 section 3.3.10).
ICS_RECUR = {"RRULE", "EXRULE"}
JMAP_IGNORED = {
    "id",
    "baseEventId",
    "isOrigin",
    "utcStart",
    "utcEnd",
    "x-href",
    "created",
    "updated",
    "sequence",
    "prodId",
}
# A server MUST return isDraft and useDefaultAlerts (draft-ietf-jmap-calendars-32 section 5.7).
JMAP_DEFAULTS = {"isDraft": False, "useDefaultAlerts": False, "version": "2.0"}


def ics_canonical(text):
    """The canonical form of an iCalendar text, as nested sorted tuples."""
    stack = [("ROOT", [], [])]
    for line in Contentlines.from_ical(text):
        if not line:
            continue
        name, params, value = Contentline(line).parts()
        name = name.upper()
        if name == "BEGIN":
            stack.append((value.upper(), [], []))
        elif name == "END":
            comp, props, children = stack.pop()
            stack[-1][2].append(repr((comp, sorted(props), sorted(children))))
        elif name not in ICS_IGNORED:
            norm = tuple(sorted((k.upper(), str(v)) for k, v in params.items()))
            if name in ICS_RECUR:
                value = ";".join(sorted(value.split(";")))
            stack[-1][1].append((name, norm, value))
    return stack[0][2]


def ics_fingerprint(text):
    return hashlib.sha256(repr(ics_canonical(text)).encode()).hexdigest()


def jmap_fingerprint(event):
    kept = {k: v for k, v in event.items() if k not in JMAP_IGNORED}
    filled = {**JMAP_DEFAULTS, **kept}
    raw = json.dumps(filled, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def differences(sent, stored):
    """Canonical lines present on one side only, for the log."""
    a, b = set(ics_canonical(sent)), set(ics_canonical(stored))
    return {"only sent": sorted(a - b)[:3], "only stored": sorted(b - a)[:3]}


mode, SERVER = sys.argv[1], sys.argv[2]
if mode == "caldav":
    BASE, CALENDAR, USER, PASSWORD = sys.argv[3:7]
    dav = DAV(BASE, CALENDAR, USER, PASSWORD)
    dav.reset()
    for fixture in ("event.ics", "event-rich.ics"):
        name = fixture.replace(".ics", "-fp.ics")
        sent = ics(f"fp-{fixture}", "fingerprint", fixture=fixture)
        put = dav.put(f"PUT {fixture}", name, sent)
        # iCalendar defaults to UTF-8 (RFC 5545 section 3.1.4); a server may send no charset.
        stored = dav.request(f"GET {fixture}", "GET", dav.href(name)).content.decode("utf-8")
        raw_equal = sent == stored
        same = ics_fingerprint(sent) == ics_fingerprint(stored)
        result(SERVER, f"{fixture}: PUT status", put.status_code)
        result(SERVER, f"{fixture}: raw text equal", raw_equal)
        check(
            SERVER,
            f"{fixture}: canonical fingerprint equal",
            same,
            "" if same else str(differences(sent, stored)),
        )
else:
    BASE, USER, PASSWORD, needs_version = sys.argv[3:7]
    jm = JMAP(BASE, USER, PASSWORD, needs_version == "yes")
    sent = jm.event("fp-jmap", "fingerprint")
    event_id = jm.create("create", {"c": ("fp-jmap", "fingerprint")})["c"]
    for step in ("after create", "after an update"):
        if step == "after an update":
            sent = {**sent, "title": "fingerprint, updated"}
            jm.call("update", "CalendarEvent/set", {"update": {event_id: {"title": sent["title"]}}})
        stored = jm.call("get", "CalendarEvent/get", {"ids": [event_id]})[1]["list"][0]
        result(SERVER, f"{step}: properties the server added", sorted(set(stored) - set(sent)))
        check(
            SERVER,
            f"{step}: fingerprint equal",
            jmap_fingerprint(sent) == jmap_fingerprint(stored),
        )
finish()
