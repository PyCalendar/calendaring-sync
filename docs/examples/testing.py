# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# start: server
from calendaring_sync import Changed, ChangeSet, MemoryStore, Removed, Replica


def server_sends(replica, since, token, changes, *, full_listing=False):
    """Ingest what a server would have sent for the "work" scope."""
    with replica.session("work", fingerprint_scheme="test", media_type="text/plain") as session:
        session.ingest(ChangeSet("work", since, token, changes, full_listing=full_listing))


replica = Replica(MemoryStore())
server_sends(replica, None, "t1", {"a": Changed("v1", "a1", b"Team meeting")}, full_listing=True)
server_sends(replica, "t1", "t2", {"a": Changed("v2", "a2", b"Team meeting, moved")})
server_sends(replica, "t2", "t3", {"a": Removed()})
# end: server

# start: check
from calendaring_sync import FeedEntryKind

kinds = [entry.kind for entry in replica.read_feed()]
assert kinds == [FeedEntryKind.CHANGED, FeedEntryKind.CHANGED, FeedEntryKind.REMOVED]
# end: check
