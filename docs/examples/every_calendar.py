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
    SQLiteStore("alice.db") as store,
    DAVClient(url=URL, username=USERNAME, password=PASSWORD) as client,
):
    replica = Replica(store)
    adapters = [CalDAVAdapter(replica, c) for c in client.get_calendars()]
    for adapter in adapters:
        adapter.sync()

    still_there = {adapter.scope_id for adapter in adapters}
    dropped = [replica.drop_scope(s.id) for s in replica.scopes() if s.id not in still_there]
# end: sync
