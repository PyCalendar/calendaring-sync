# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from start import adapter, edit_on_server, meeting, replica

# Another client moves the meeting while the application renames it.
edit_on_server(b"Team meeting, moved")
adapter.change(meeting.local_key, b"Team sync")
adapter.sync()

# start: one
from calendaring_sync import KeepLocal, RecordState

for record in replica.records("work", states=frozenset({RecordState.CONFLICT})):
    replica.resolve(record, KeepLocal())
adapter.sync()
# end: one

edit_on_server(b"Team sync, room 3")
adapter.change(meeting.local_key, b"Team sync, at 10")
adapter.sync()


# start: merge
from calendaring_sync import ConflictKind, KeepServer, Merge


def resolver(record):
    conflict = record.conflict
    if conflict.kind is ConflictKind.BOTH_CHANGED:
        merged = conflict.remote_content + b" / " + record.edit.content
        return Merge(merged, adapter.edit_fields(merged))
    if conflict.kind is ConflictKind.REMOVED_REMOTELY:
        return KeepServer()
    return None


replica.resolve_conflicts(resolver)
adapter.sync()
# end: merge
