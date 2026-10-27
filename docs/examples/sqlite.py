# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# start: open
import sqlite3

from calendaring_sync import Replica, SQLiteStore

connection = sqlite3.connect("sync.db")
connection.execute("PRAGMA journal_mode = WAL")
connection.close()

store = SQLiteStore("sync.db", timeout=10.0)
replica = Replica(store)
# end: open

from memory_adapter import MemoryAdapter, MemoryServer

adapter = MemoryAdapter(replica, MemoryServer(), "work")

# start: busy
import time

from calendaring_sync import StoreBusyError

for _ in range(3):
    try:
        adapter.sync()
        break
    except StoreBusyError:
        time.sleep(1)
# end: busy

store.close()
