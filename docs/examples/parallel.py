# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from calendaring_sync import MemoryStore, Replica

from memory_adapter import MemoryAdapter, MemoryServer

replica = Replica(MemoryStore())
servers = {name: MemoryServer() for name in ("work", "home", "team")}
for name, server in servers.items():
    server.put("meeting", f"{name} meeting".encode(), if_version=None)
adapters = [MemoryAdapter(replica, server, name) for name, server in servers.items()]

# start: threads
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor() as pool:
    for done in [pool.submit(adapter.sync) for adapter in adapters]:
        done.result()
# end: threads

# start: retry
from calendaring_sync import StaleChangeSetError


def sync(adapter, attempts=3):
    """Sync once, again if another process ingested the scope first."""
    for _ in range(attempts - 1):
        try:
            return adapter.sync()
        except StaleChangeSetError:
            pass
    return adapter.sync()


for adapter in adapters:
    sync(adapter)
# end: retry
