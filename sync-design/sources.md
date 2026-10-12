<!--
SPDX-FileCopyrightText: 2026 calendaring-sync contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Source references

The server and library code behind behaviors the logs show, linked at the version that was tested rather than copied here.

## Stalwart v0.16.21

- CalDAV sync reads the change log without checking whether it was truncated: [`crates/dav/src/common/propfind.rs` lines 1223 to 1226](https://github.com/stalwartlabs/stalwart/blob/v0.16.21/crates/dav/src/common/propfind.rs#L1223-L1226). The file contains no `is_truncated` check.
- JMAP `/changes` answers `cannotCalculateChanges` for a truncated log: [`crates/jmap/src/changes/get.rs` lines 283 to 290](https://github.com/stalwartlabs/stalwart/blob/v0.16.21/crates/jmap/src/changes/get.rs#L283-L290).
- Stalwart's own test truncates the log the way `servers/stalwart_admin.py expire-purge` does: [`tests/src/system/purge.rs` lines 38 to 48 and 145 to 152](https://github.com/stalwartlabs/stalwart/blob/v0.16.21/tests/src/system/purge.rs#L38-L48).

## Radicale 3.8.3

- An empty token reports deletion history as removed members: [`radicale/storage/multifilesystem/sync.py` lines 56 to 63 and 113 to 121](https://github.com/Kozea/Radicale/blob/v3.8.3/radicale/storage/multifilesystem/sync.py#L56-L63).

## Xandikos 0.4.8

- `DAV:limit` cuts the list with `itertools.islice` and the response still carries the newest token: [`xandikos/sync.py` lines 108 to 144](https://github.com/jelmer/xandikos/blob/v0.4.8/xandikos/sync.py#L108-L144).

## python-caldav master at 1a75f45

- `get_objects_by_sync_token()` follows truncated pages up to `sync_max_pages`: [`caldav/collection.py` lines 2460 to 2492](https://github.com/python-caldav/caldav/blob/1a75f457aab85e00a8dbafe69ac47aedc9c58e6b/caldav/collection.py#L2460-L2492).
- With the fallback on, a failed report becomes a full listing ([`caldav/collection.py` lines 2493 to 2499](https://github.com/python-caldav/caldav/blob/1a75f457aab85e00a8dbafe69ac47aedc9c58e6b/caldav/collection.py#L2493-L2499)) under a `fake-` token ([line 2407](https://github.com/python-caldav/caldav/blob/1a75f457aab85e00a8dbafe69ac47aedc9c58e6b/caldav/collection.py#L2407)).
- `AuthorizationError` keeps the body and the `DAV:error` preconditions; other errors don't: [`caldav/lib/error.py` lines 151 to 174](https://github.com/python-caldav/caldav/blob/1a75f457aab85e00a8dbafe69ac47aedc9c58e6b/caldav/lib/error.py#L151-L174).
- `save()` still increments `SEQUENCE` by default: [`caldav/calendarobjectresource.py` line 1379](https://github.com/python-caldav/caldav/blob/1a75f457aab85e00a8dbafe69ac47aedc9c58e6b/caldav/calendarobjectresource.py#L1379).
- `delete()` takes `if_match`: [`caldav/calendarobjectresource.py` line 1187](https://github.com/python-caldav/caldav/blob/1a75f457aab85e00a8dbafe69ac47aedc9c58e6b/caldav/calendarobjectresource.py#L1187).
