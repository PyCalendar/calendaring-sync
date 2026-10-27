# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from calendaring_sync import MemoryStore, Replica

replica = Replica(MemoryStore())
URL = "https://caldav.example.com/"
USERNAME = "alice"
PASSWORD = "secret"

# start: sync
import asyncio

from caldav.aio import AsyncDAVClient

from calendaring_sync.adapters.caldav import AsyncCalDAVAdapter


async def sync() -> None:
    async with AsyncDAVClient(url=URL, username=USERNAME, password=PASSWORD) as client:
        calendar = (await client.get_calendars())[0]
        adapter = AsyncCalDAVAdapter(replica, calendar)
        await adapter.sync()


asyncio.run(sync())
# end: sync
