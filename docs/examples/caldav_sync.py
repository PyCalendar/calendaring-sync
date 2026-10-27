# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from calendaring_sync import MemoryStore, Replica

replica = Replica(MemoryStore())
URL = "https://caldav.example.com/"
CALENDAR_URL = "https://caldav.example.com/calendars/alice/work/"
USERNAME = "alice"
PASSWORD = "secret"
SERVER = "radicale"

# start: client
from caldav.davclient import DAVClient

from calendaring_sync.adapters.caldav import CalDAVAdapter

client = DAVClient(url=URL, username=USERNAME, password=PASSWORD, features=SERVER)
calendar = client.calendar(url=CALENDAR_URL)
adapter = CalDAVAdapter(replica, calendar)
# end: client

# start: listings
from datetime import UTC, datetime, timedelta

adapter.sync()
listed = replica.scope(adapter.scope_id).last_full_listing_at
stale = listed is None or datetime.now(UTC) - listed > timedelta(days=1)
if adapter.needs_full_listings and stale:
    adapter.sync(full_listing=True)
# end: listings

client.close()
