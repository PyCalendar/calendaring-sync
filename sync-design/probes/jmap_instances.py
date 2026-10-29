# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Events that share a uid, and how a refused calendarIds reads, over raw JMAP.

Usage: jmap_instances.py SERVER BASE_URL USER PASSWORD NEEDS_VERSION(yes|no)

draft-ietf-jmap-calendars-33 section 1.4.1 lets an account hold several
events with one uid when their recurrenceId values differ. A server that
refuses that names a property; this probe records which, and whether the
same property is named when the calendar itself is wrong.
"""

import sys
import uuid

from _common import JMAP, finish, result

SERVER, BASE, USER, PASSWORD, NEEDS_VERSION = sys.argv[1:6]
jmap = JMAP(BASE, USER, PASSWORD, NEEDS_VERSION == "yes")
first = jmap.event("probe-instances-calendar", "x")["calendarIds"]
_, made = jmap.call("a second calendar", "Calendar/set", {"create": {"c": {"name": "probe"}}})
second = (made.get("created") or {}).get("c", {}).get("id")
uid = f"probe-instances-{uuid.uuid4()}"
created = []


def create(label, calendars, **members):
    """Create one event with the probe's uid; record what the server answered."""
    event = {**jmap.event(uid, "instance"), "calendarIds": calendars, **members}
    _, out = jmap.call(label, "CalendarEvent/set", {"create": {"e": event}})
    if "e" in (out.get("created") or {}):
        created.append(out["created"]["e"]["id"])
        answer = "created"
    else:
        refusal = out["notCreated"]["e"]
        answer = f"{refusal['type']} on {refusal.get('properties')}"
    result(SERVER, label, answer)


create("1 first instance", first, recurrenceId="2026-11-02T10:00:00")
create("2 another recurrenceId, same calendar", first, recurrenceId="2026-11-09T10:00:00")
if second:
    create(
        "3 another recurrenceId, another calendar",
        {second: True},
        recurrenceId="2026-11-16T10:00:00",
    )
create("4 the same uid, no recurrenceId", first)
uid = f"probe-instances-{uuid.uuid4()}"
create("5 a calendar that doesn't exist", {"probe-no-such-calendar": True})
create("6 no calendar", {})

jmap.call("clean up", "CalendarEvent/set", {"destroy": created})
if second:
    jmap.call("clean up", "Calendar/set", {"destroy": [second], "onDestroyRemoveEvents": True})
finish()
