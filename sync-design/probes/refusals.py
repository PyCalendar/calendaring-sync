# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a server refuses a write it won't take as sent: content it won't store, or no rights.

Usage:
  refusals.py content SERVER BASE_URL HOME USER PASSWORD
  refusals.py rights SERVER BASE_URL CALENDAR_PATH OWNER OWNER_PASSWORD
                     READER READER_PASSWORD [share]
  refusals.py jmap SERVER BASE_URL OWNER OWNER_PASSWORD READER READER_PASSWORD

content: makes a calendar for events only (RFC 4791 section 5.2.3) and PUTs into it an event
without DTSTART and a task, each with only a UID, a DTSTAMP and a summary.

rights: the owner makes CALENDAR_PATH and puts an event in it, then the reader, who may at most
read it, changes it, creates another and deletes it. With ``share``, the owner first shares its
first JMAP calendar with the reader to read, and CALENDAR_PATH must be that calendar.

jmap: the owner shares its first calendar with the reader to read (mayReadItems) and creates
an event in it; the reader then updates it, creates another and destroys it with
CalendarEvent/set (RFC 8620 section 5.3).

Every write's status, precondition elements and body are logged, and summed up as RESULT lines.
"""

import json
import re
import sys
import uuid

from _common import DAV, JMAP, XML, finish, ics, result

EVENTS_ONLY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:set><D:prop>'
    '<C:supported-calendar-component-set><C:comp name="VEVENT"/>'
    "</C:supported-calendar-component-set>"
    "</D:prop></D:set></C:mkcalendar>"
)
READ = {"mayReadItems": True, "mayReadFreeBusy": True}


def minimal(component, uid, summary):
    """A component with only a UID, a DTSTAMP and a summary.

    That's a valid task, and an event without a start.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//calendaring-sync probes//EN",
        f"BEGIN:{component}",
        f"UID:{uid}",
        "DTSTAMP:20261101T100000Z",
        f"SUMMARY:{summary}",
        f"END:{component}",
        "END:VCALENDAR",
    ]
    return "".join(f"{line}\r\n" for line in lines)


def answer(r):
    """A write's status, the precondition elements its body names, and a plain-text body."""
    elements = re.findall(r"<(?:\w+:)?([\w-]+)\s*/>", r.text or "")
    text = " ".join((r.text or "").split())
    said = f", body {text[:120]!r}" if text and not text.startswith("<") else ""
    return f"{r.status_code}" + (f" {elements}" if elements else "") + said


def content(server, base, home, user, password):
    dav = DAV(base, f"{home}refusals/", user, password)
    dav.request("reset: DELETE calendar", "DELETE", dav.calendar)
    made = dav.request("MKCALENDAR for events only", "MKCALENDAR", dav.calendar, EVENTS_ONLY, XML)
    result(server, "MKCALENDAR for events only", str(made.status_code))
    event = minimal("VEVENT", f"nostart-{uuid.uuid4()}", "no start")
    r = dav.put("event without DTSTART", "nostart.ics", event, {"If-None-Match": "*"})
    result(server, "event without DTSTART", answer(r))
    task = minimal("VTODO", f"task-{uuid.uuid4()}", "a task")
    r = dav.put("task in a calendar for events", "task.ics", task, {"If-None-Match": "*"})
    result(server, "task in a calendar for events", answer(r))


def rights(server, base, calendar, owner, owner_password, reader, reader_password, share=None):
    own = DAV(base, calendar, owner, owner_password)
    if share:
        jmap = JMAP(base, owner, owner_password, needs_version=False)
        reading = JMAP(base, reader, reader_password, needs_version=False)
        jmap.event("probe", "probe")  # looks the first calendar up
        jmap.call(
            "share to read",
            "Calendar/set",
            {"update": {jmap.calendar_id: {f"shareWith/{reading.account}": READ}}},
        )
    else:
        own.reset()
    uid = f"shared-{uuid.uuid4()}"
    own.put("owner PUT", "shared.ics", ics(uid, "one"))
    etag = own.etag("shared.ics")
    them = DAV(base, calendar, reader, reader_password)
    r = them.request("reader GET", "GET", them.href("shared.ics"))
    result(server, "reader GET", str(r.status_code))
    r = them.put("reader change", "shared.ics", ics(uid, "mine"), {"If-Match": etag or "*"})
    result(server, "reader change", answer(r))
    r = them.put(
        "reader create", "mine.ics", ics(f"mine-{uuid.uuid4()}", "mine"), {"If-None-Match": "*"}
    )
    result(server, "reader create", answer(r))
    r = them.delete("reader delete", "shared.ics", {"If-Match": etag or "*"})
    result(server, "reader delete", answer(r))


def jmap_rights(server, base, owner, owner_password, reader, reader_password):
    own = JMAP(base, owner, owner_password, needs_version=False)
    them = JMAP(base, reader, reader_password, needs_version=False)
    event = own.event(f"shared-{uuid.uuid4()}", "one")
    own.call(
        "share to read",
        "Calendar/set",
        {"update": {own.calendar_id: {f"shareWith/{them.account}": READ}}},
    )
    created = own.call("owner create", "CalendarEvent/set", {"create": {"e": event}})[1]["created"][
        "e"
    ]["id"]
    session = them.session["accounts"].get(own.account, {})
    result(
        server,
        "reader's session marks the owner's account read-only",
        str(session.get("isReadOnly")),
    )
    them.account = own.account
    state = them.state()
    for label, args, key in (
        ("reader update", {"update": {created: {"title": "mine"}}}, "notUpdated"),
        ("reader create", {"create": {"n": {**event, "uid": f"n-{uuid.uuid4()}"}}}, "notCreated"),
        ("reader destroy", {"destroy": [created]}, "notDestroyed"),
    ):
        name, out = them.call(label, "CalendarEvent/set", {"ifInState": state, **args})
        refused = next(iter((out.get(key) or {}).values()), None) if name != "error" else out
        moved = "moved" if out.get("newState") not in (None, state) else "unchanged"
        result(server, label, f"{name} {json.dumps(refused)}, state {moved}")


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    {"content": content, "rights": rights, "jmap": jmap_rights}[mode](*args)
    finish()
