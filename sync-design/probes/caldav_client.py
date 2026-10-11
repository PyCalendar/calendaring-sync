# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What python-caldav does with sync responses and conditional writes on a real server.

Usage: caldav_client.py SERVER BASE_URL CALENDAR_URL USER PASSWORD

The calendar at CALENDAR_URL must exist and may be empty.
"""

import sys

import caldav
from caldav.davclient import DAVClient
from caldav.elements import dav

from _common import (
    add_event,
    caldav_sync_report,
    capture,
    finish,
    ics,
    multiget_body,
    outcome,
    result,
    sync_body,
    sync_objects,
    tree,
)

SERVER, BASE, CALENDAR_URL, USER, PASSWORD = sys.argv[1:6]
client = DAVClient(url=BASE, username=USER, password=PASSWORD)
calendar = client.calendar(url=CALENDAR_URL)
NEVER_ISSUED = "http://example.invalid/never-issued"
NEVER_ISSUED_HEX = "0" * 40  # shaped like Xandikos's tokens, which answers other shapes with 500
LIMIT_1 = b"<D:limit><D:nresults>1</D:nresults></D:limit><D:prop>"
SEQUENCE = r"SEQUENCE:(\d+)"
WITH_SEQUENCE = "SEQUENCE:0\r\nUID:"  # the fixture has no SEQUENCE; this gives it one


def limited(raw):
    """Adds DAV:limit nresults=1 to a sync-collection body."""
    return raw.replace(b"<D:prop>", LIMIT_1, 1)


def token_and_count(**kwargs):
    """The token, object count and truncated flag get_objects_by_sync_token() returns."""
    objects = sync_objects(calendar, **kwargs)
    return objects.sync_token, len(list(objects)), getattr(objects, "truncated", None)


def etag(obj):
    """The object's ETag as the server reports it now."""
    return obj.get_property(dav.GetEtag())


def stored_sequence(obj):
    """The SEQUENCE the server stores for obj, or None when it has none."""
    return capture(SEQUENCE, client.request(str(obj.url)).raw)


result(SERVER, "python-caldav version", caldav.__version__)
result(
    SERVER,
    "unknown token, get_objects_by_sync_token(disable_fallback=True)",
    outcome(lambda: token_and_count(sync_token=NEVER_ISSUED, disable_fallback=True)),
)
result(
    SERVER,
    "unknown 40-hex-digit token, get_objects_by_sync_token(disable_fallback=True)",
    outcome(lambda: token_and_count(sync_token=NEVER_ISSUED_HEX, disable_fallback=True)),
)
result(
    SERVER,
    "unknown token, DAVClient.report()",
    outcome(lambda: caldav_sync_report(client, CALENDAR_URL, NEVER_ISSUED).status),
)
result(
    SERVER,
    "unknown token, default fallback",
    outcome(lambda: token_and_count(sync_token=NEVER_ISSUED)),
)

token = sync_objects(calendar, disable_fallback=True).sync_token
event = add_event(calendar, ics("client-removed", "removed"))
token = sync_objects(calendar, sync_token=token, disable_fallback=True).sync_token
event.delete()
removed = [
    (str(o.url).rsplit("/", 1)[-1], o.props.get("{DAV:}getetag"), o.data is None)
    for o in sync_objects(calendar, sync_token=token, disable_fallback=True)
]
result(SERVER, "a removed member as returned: (href, getetag, data is None)", removed)
listed = [str(o.url) for o in sync_objects(calendar, disable_fallback=True)]
result(
    SERVER,
    "full listing through get_objects_by_sync_token(): the collection itself among the members",
    any(u.rstrip("/") == CALENDAR_URL.rstrip("/") for u in listed),
)

for n in range(3):
    add_event(calendar, ics(f"client-{n}", "x"))
page = client.report(CALENDAR_URL, limited(sync_body(client)).decode(), depth=0)
has_507 = any("507" in (s.text or "") for s in tree(page).iter("{DAV:}status"))
result(
    SERVER,
    "DAV:limit 1 through DAVClient.report()",
    f"status {page.status}, 507 present: {has_507}, token {page.sync_token}",
)
original = client._build_sync_collection_body
client._build_sync_collection_body = lambda *a, **k: limited(original(*a, **k))
result(
    SERVER,
    "DAV:limit 1, get_objects_by_sync_token(disable_fallback=True): (token, objects, truncated)",
    outcome(lambda: token_and_count(disable_fallback=True)),
)
client._build_sync_collection_body = original
result(
    SERVER,
    "same calendar unlimited: (token, objects, truncated)",
    outcome(lambda: token_and_count(disable_fallback=True)),
)

hrefs = [str(o.url) for o in sync_objects(calendar, disable_fallback=True)][:2]
multiget = client.report(CALENDAR_URL, multiget_body(hrefs), depth=None)
entries = len(tree(multiget).findall("{DAV:}response"))
result(
    SERVER,
    "calendar-multiget through DAVClient.report(depth=None), no Depth header",
    f"status {multiget.status}, entries {entries} for {len(hrefs)} hrefs",
)

seq = add_event(calendar, ics("client-sequence", "sequence").replace("UID:", WITH_SEQUENCE, 1))
before = stored_sequence(seq)
seq.save(increase_seqno=False)
unchanged = stored_sequence(seq)
seq.save()
result(
    SERVER,
    "SEQUENCE stored: after create, after save(increase_seqno=False), after save()",
    (before, unchanged, stored_sequence(seq)),
)

target = add_event(calendar, ics("client-delete", "delete"))
old = etag(target)
elsewhere = calendar.event_by_url(target.url)
elsewhere.data = ics("client-delete", "changed elsewhere")
elsewhere.save(increase_seqno=False)
target.data = ics("client-delete", "changed here")
result(
    SERVER,
    "save(increase_seqno=False) of an object someone else saved since",
    outcome(lambda: target.save(increase_seqno=False)),
)
result(
    SERVER,
    "delete(if_match=<ETag before someone else's save>)",
    outcome(lambda: target.delete(if_match=old)),
)
target.delete(if_match=etag(target))
result(
    SERVER,
    "delete(if_match=<last ETag>) on an object already deleted",
    outcome(lambda: target.delete(if_match=old)),
)

finish()
