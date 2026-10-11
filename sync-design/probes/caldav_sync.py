# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""CalDAV sync behavior over raw HTTP.

Usage: caldav_sync.py SERVER BASE_URL CALENDAR_PATH USER PASSWORD

Each numbered section is one row of the server compatibility page.
"""

import re
import sys
import uuid

from _common import (
    COLLECTION,
    DAV,
    QUOT,
    XML,
    capture,
    check,
    finish,
    ics,
    multiget_body,
    propfind_body,
    responses,
    result,
)

SERVER, BASE, CALENDAR, USER, PASSWORD = sys.argv[1:6]
dav = DAV(BASE, CALENDAR, USER, PASSWORD)
dav.reset()

# 1. supported-report-set (RFC 3253 section 3.1.5)
r = dav.request(
    "1 supported-report-set",
    "PROPFIND",
    CALENDAR,
    propfind_body("supported-report-set"),
    {"Depth": "0", **XML},
)
reports = sorted(set(re.findall(r"<(?:\w+:)?report>\s*<(?:\w+:)?([a-z-]+)", r.text)))
result(
    SERVER,
    "1 sync-collection in supported-report-set",
    f"{'sync-collection' in reports}; {reports}",
)

# 2. initial listing and an incremental report (RFC 6578 section 3.2)
_, _, t0 = dav.sync("2a empty token, empty calendar", "")
for n in "abc":
    dav.put(f"2 create {n}", f"{n}.ics", ics(f"uid-{n}", f"{n} v1"))
_, m, t1 = dav.sync("2b empty token, three members", "")
result(SERVER, "2 initial listing", m)
dav.put("2 create d", "d.ics", ics("uid-d", "d v1"))
dav.put("2 modify a", "a.ics", ics("uid-a", "a v2"))
dav.delete("2 delete b", "b.ics")
_, m, t2 = dav.sync("2c incremental after create d, modify a, delete b", t1)
result(SERVER, "2 incremental", m)

# 3. created and deleted between two reports (RFC 6578 section 3.5.2: reported as removed)
dav.put("3 create e", "e.ics", ics("uid-e", "e"))
dav.delete("3 delete e", "e.ics")
_, m, t3 = dav.sync("3 report after e was created and deleted", t2)
result(
    SERVER,
    "3 created and deleted between reports",
    f"{m}: {'reported as removed' if ('e.ics', '404') in m else 'not reported'}",
)

# 4. deleted and re-created at one href (RFC 6578 sections 3.5.1 and 3.2: once, as changed)
dav.delete("4 delete c", "c.ics")
dav.put("4 re-create c", "c.ics", ics("uid-c", "c v2"))
_, m, t4 = dav.sync("4 report after c was deleted and re-created", t3)
result(
    SERVER, "4 deleted and re-created", f"entries for c.ics: {[s for n, s in m if n == 'c.ics']}"
)

# 5. tokens the server never issued (RFC 6578 section 3.2: DAV:valid-sync-token)
last_char = "1" if t1[-1] != "1" else "2"
for label, tok in (
    ("well-formed URI", "http://example.invalid/never-issued"),
    ("not a URI", "not a uri"),
    ("real token with two characters replaced", t1[:-2] + "zz"),
    ("real token with its last character changed", t1[:-1] + last_char),
):
    r, m, _ = dav.sync(f"5 token: {label}", tok)
    result(
        SERVER,
        f"5 token {label}",
        f"{r.status_code}, valid-sync-token: {'valid-sync-token' in r.text}, members {m[:5]}",
    )

# 6. empty token after deletions (RFC 6578 section 3.4: no removed members)
_, m, _ = dav.sync("6 empty token after deletions", "")
result(
    SERVER,
    "6 empty token after deletions",
    f"removed members listed: {[x for x in m if x[1] == '404']}; "
    f"collection listed: {any(n == COLLECTION for n, _ in m)}",
)

# 7. DAV:limit, paged from an old token and from an empty one, each followed to the end
# (RFC 6578 sections 3.6, 3.7)
live = {n for n, s in dav.sync("7 live state", "")[1] if n != COLLECTION and s == "200"}


def paged(label, tok):
    state, pages, saw_507 = {}, 0, False
    while pages < 10:
        _, m, nxt = dav.sync(f"7 {label}, page {pages}, nresults=2", tok, limit=2)
        pages += 1
        truncated = (COLLECTION, "507") in m
        saw_507 |= truncated
        state.update({n: s for n, s in m if n != COLLECTION})
        tok = nxt
        if not truncated:
            break
    _, m, _ = dav.sync(f"7 {label}, follow-up from the last page's token", tok)
    state.update({n: s for n, s in m if n != COLLECTION})
    got = {n for n, s in state.items() if s == "200"}
    result(
        SERVER,
        f"7 DAV:limit {label}",
        f"507 sent: {saw_507}; pages {pages}; "
        f"paged + follow-up equals live: {got == live}; missing {sorted(live - got)}",
    )


paged("from an old token", t0)
paged("from an empty token", "")

# 8. identical PUT, and a PUT the server may store differently (RFC 4791 section 5.3.4)
r1 = dav.request("8 GET a", "GET", dav.href("a.ics"))
dav.put("8 PUT a's stored content back unchanged", "a.ics", r1.text)
result(SERVER, "8 identical PUT", f"ETag unchanged: {r1.headers.get('ETag') == dav.etag('a.ics')}")
sent = ics("uid-f", "no DTSTAMP", dtstamp=False)
rp = dav.put("8 PUT without DTSTAMP", "f.ics", sent)
rg = dav.request("8 GET f", "GET", dav.href("f.ics"))
result(
    SERVER,
    "8 PUT stored differently",
    f"stored equals sent: {rg.text == sent}; ETag in the PUT answer: {rp.headers.get('ETag')}",
)

# 9. conditional writes (RFC 9110 sections 13.1.1, 13.1.2)
#    and a duplicate UID (RFC 4791 section 5.3.2.1)
etag_a = dav.etag("a.ics")
for label, name, body, headers in (
    ("PUT If-Match wrong", "a.ics", ics("uid-a", "a v3"), {"If-Match": '"wrong"'}),
    ("PUT If-Match right", "a.ics", ics("uid-a", "a v3"), {"If-Match": etag_a}),
    ("PUT If-None-Match * on existing", "a.ics", ics("uid-a", "a v4"), {"If-None-Match": "*"}),
    ("PUT If-Match on missing", f"{uuid.uuid4()}.ics", ics("uid-h", "h"), {"If-Match": '"x"'}),
):
    result(SERVER, f"9 {label}", dav.put(f"9 {label}", name, body, headers).status_code)
r = dav.put(
    "9 create with a UID already used", "g.ics", ics("uid-a", "dup"), {"If-None-Match": "*"}
)
collision = re.findall(r"<(?:\w+:)?(no-uid-conflict|unique-scheduling-object-resource)", r.text)
result(SERVER, "9 duplicate UID", f"{r.status_code}, element {collision}")
result(
    SERVER,
    "9 DELETE If-Match wrong",
    dav.delete("9 DELETE If-Match wrong", "d.ics", {"If-Match": '"wrong"'}).status_code,
)
etag_d = dav.etag("d.ics")
result(
    SERVER,
    "9 DELETE If-Match right",
    dav.delete("9 DELETE If-Match right", "d.ics", {"If-Match": etag_d}).status_code,
)
result(
    SERVER,
    "9 DELETE If-Match on gone",
    dav.delete("9 DELETE If-Match on gone", "d.ics", {"If-Match": etag_d}).status_code,
)

# 10. an href made of unreserved characters (RFC 3986 section 2.3) round-trips unchanged
name = f"{uuid.uuid4()}.ics"
dav.put("10 create with a UUID href", name, ics(name, "round trip"), {"If-None-Match": "*"})
_, m, t10 = dav.sync("10 report after the UUID create", t4)
check(SERVER, "10 unreserved href round-trips in the sync report", (name, "200") in m)

# 11. calendar-multiget against changes made after the report
#     (RFC 4791 section 7.9, RFC 6578 section 3.1)
dav.put("11 create keep", "keep.ics", ics("keep", "v1"))
dav.put("11 create gone", "gone.ics", ics("gone", "v1"))
_, m, t11 = dav.sync("11 report", t10)
report_etag = dav.etag("keep.ics")
dav.put("11 change keep after the report", "keep.ics", ics("keep", "v2 changed after the report"))
dav.delete("11 delete gone after the report", "gone.ics")
live_etag = dav.etag("keep.ics")
query = multiget_body([dav.href(n) for n in ("keep.ics", "gone.ics")])
r = dav.request("11 calendar-multiget, no Depth header", "REPORT", CALENDAR, query, XML)
entries = responses(r.text)
keep = entries.get("keep.ics", "")
fetched_etag = capture(r"<(?:\w+:)?getetag>([^<]*)<", keep, "").replace(QUOT, '"')
check(SERVER, "11 multiget is 207", r.status_code == 207, str(r.status_code))
check(
    SERVER,
    "11 multiget returns the newer ETag with the newer content",
    fetched_etag == live_etag != report_etag and "v2 changed after the report" in keep,
    f"report-time {report_etag}, fetched {fetched_etag}, live {live_etag}",
)
gone = entries.get("gone.ics")
gone_status = re.findall(r"HTTP/1.1 (\d+)", gone) if gone is not None else None
result(
    SERVER,
    "11 multiget for a member deleted after the report",
    "omitted from the response" if gone is None else f"entry with status {gone_status}",
)
_, m, _ = dav.sync("11 next report", t11)
check(SERVER, "11 the removal arrives in the next report", ("gone.ics", "404") in m)

# 12. a full listing without sync-collection: the DAV:sync-token property first (RFC 6578
#     section 4), then members and ETags by PROPFIND (RFC 4918 section 9.1)
r = dav.request(
    "12 DAV:sync-token property",
    "PROPFIND",
    CALENDAR,
    propfind_body("sync-token"),
    {"Depth": "0", **XML},
)
t12 = capture(r"sync-token>([^<]*)<", r.text, "")
check(SERVER, "12 DAV:sync-token property is defined", bool(t12), t12)
r = dav.request(
    "12 members and ETags", "PROPFIND", CALENDAR, propfind_body("getetag"), {"Depth": "1", **XML}
)
listed = {n for n, resp in responses(r.text).items() if n != COLLECTION and "getetag>" in resp}
live = {n for n, s in dav.sync("12 live state", "")[1] if n != COLLECTION and s == "200"}
check(SERVER, "12 PROPFIND lists every member", listed == live, f"missing {sorted(live - listed)}")
dav.put("12 change keep after the property was read", "keep.ics", ics("keep", "v3"))
_, m, _ = dav.sync("12 report from the property's token", t12)
check(SERVER, "12 a change after the property arrives in the next report", ("keep.ics", "200") in m)

finish()
