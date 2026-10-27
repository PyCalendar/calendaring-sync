# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# What the source holds on each run: the same twice, then one object moved
# and the other gone.
_snapshots = iter(
    [
        {"meeting": b"Team meeting", "lunch": b"Lunch with Ana"},
        {"meeting": b"Team meeting", "lunch": b"Lunch with Ana"},
        {"meeting": b"Team meeting, moved"},
    ]
)


def read_source() -> dict[str, bytes]:
    return next(_snapshots)


# start: track
import hashlib
import uuid

from calendaring_sync import Change, Changed, ChangeSet, MemoryStore, Replica, Unchanged

SCOPE = "team-calendar"
replica = Replica(MemoryStore())


def track(objects: dict[str, bytes]) -> None:
    """Hand the core everything the source holds now, by id."""
    with replica.session(SCOPE, fingerprint_scheme="sha256", media_type="text/plain") as session:
        since = replica.scope(SCOPE).token
        known = replica.known_versions(SCOPE)
        changes: dict[str, Change] = {}
        for object_id, content in objects.items():
            digest = hashlib.sha256(content).hexdigest()
            if known.get(object_id) == digest:
                changes[object_id] = Unchanged()
            else:
                changes[object_id] = Changed(digest, digest, content)
        session.ingest(ChangeSet(SCOPE, since, uuid.uuid4().hex, changes, full_listing=True))


# end: track

# start: run
for _ in range(3):
    track(read_source())

for entry in replica.read_feed():
    print(entry.kind, entry.object_id)
# end: run
