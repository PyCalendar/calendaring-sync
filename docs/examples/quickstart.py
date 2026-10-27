# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# start: sync
from caldav.davclient import DAVClient

from calendaring_sync import Replica, SQLiteStore
from calendaring_sync.adapters.caldav import CalDAVAdapter

URL = "https://caldav.example.com/"
USERNAME = "alice"
PASSWORD = "secret"

with (
    SQLiteStore("sync.db") as store,
    DAVClient(url=URL, username=USERNAME, password=PASSWORD) as client,
):
    calendar = client.get_calendars()[0]
    replica = Replica(store)
    adapter = CalDAVAdapter(replica, calendar)
    adapter.sync()

    for entry in replica.read_feed():
        print(entry.kind, entry.object_id)
# end: sync
