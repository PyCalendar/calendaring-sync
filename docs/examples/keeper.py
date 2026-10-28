# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# start: keeper
import pickle
from pathlib import Path

from calendaring_sync import MemoryData, MemoryKeeper, MemoryStore, Replica, StoreError


class FileKeeper(MemoryKeeper):
    """Keeps a memory store's data in one file, replaced whole on each save."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> MemoryData | None:
        if not self.path.exists():
            return None
        data: MemoryData = pickle.loads(self.path.read_bytes())
        return data

    def save(self, data: MemoryData) -> None:
        # Written beside the file and moved over it, so a failure halfway keeps the last save.
        written = self.path.with_suffix(".new")
        written.write_bytes(pickle.dumps(data))
        written.replace(self.path)

    def error(self, exc: Exception) -> StoreError | None:
        return StoreError(str(exc)) if isinstance(exc, OSError) else None


replica = Replica(MemoryStore(keeper=FileKeeper(Path("sync.pickle"))))
# end: keeper

from memory_adapter import MemoryAdapter, MemoryServer

server = MemoryServer()
server.put("meeting", b"Team meeting", if_version=None)
MemoryAdapter(replica, server, "work").sync()

# The next process loads what the last one saved.
reopened = Replica(MemoryStore(keeper=FileKeeper(Path("sync.pickle"))))
meeting = reopened.find_record("work", "meeting")
