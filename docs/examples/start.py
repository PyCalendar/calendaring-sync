# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where the how-to examples start: a replica synced with a server that holds one event."""

from calendaring_sync import MemoryStore, Replica

from memory_adapter import MemoryAdapter, MemoryServer

server = MemoryServer()
server.put("meeting", b"Team meeting", if_version=None)
replica = Replica(MemoryStore())
adapter = MemoryAdapter(replica, server, "work")
adapter.sync()
(meeting,) = replica.records("work")


def edit_on_server(content: bytes | None) -> None:
    """Another client gives the meeting this content on the server, or deletes it for ``None``."""
    version = server.objects["meeting"][0]
    if content is None:
        server.delete("meeting", if_version=version)
    else:
        server.put("meeting", content, if_version=version)
