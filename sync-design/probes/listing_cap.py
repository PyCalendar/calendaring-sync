# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A full listing of a calendar larger than the server's result limit.

Usage: listing_cap.py SERVER BASE_URL CALENDAR_PATH USER PASSWORD LIMIT

The server must cap results at LIMIT; the probe stores LIMIT + 3 events and lists them with
PROPFIND Depth: 1 and with an empty-token sync-collection report.
"""

import sys

from _common import COLLECTION, DAV, XML, check, finish, ics, members, propfind_body, result

SERVER, BASE, CALENDAR, USER, PASSWORD = sys.argv[1:6]
LIMIT = int(sys.argv[6])
STORED = LIMIT + 3
dav = DAV(BASE, CALENDAR, USER, PASSWORD)
dav.reset()
for n in range(STORED):
    dav.put(f"store event {n}", f"cap-{n}.ics", ics(f"cap-{n}", "cap"))


def listed(m):
    return [name for name, status in m if name != COLLECTION and status == "200"]


r = dav.request(
    "PROPFIND Depth: 1", "PROPFIND", CALENDAR, propfind_body("getetag"), {"Depth": "1", **XML}
)
m = members(r.text)
check(SERVER, "PROPFIND marks the cut with a 507 for the collection", (COLLECTION, "507") in m)
result(SERVER, "PROPFIND Depth: 1", f"{len(listed(m))} of {STORED} members listed")

_, m, token = dav.sync("empty-token report", "")
check(SERVER, "empty-token report marks the cut with a 507", (COLLECTION, "507") in m)
_, rest, _ = dav.sync("report from the token it returned", token)
result(
    SERVER,
    "empty-token report",
    f"{len(listed(m))} of {STORED} members listed; "
    f"following its token lists {len(listed(rest))} more",
)
finish()
