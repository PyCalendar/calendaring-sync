# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from start import adapter, meeting, replica, server

adapter.change(meeting.local_key, b"Team meeting, room 3")
# Someone deletes the whole calendar on the server.
server.removed = True

# start: sync
from calendaring_sync import ScopeGoneError, StaleChangeSetError, StoreBusyError

unsent = []
try:
    adapter.sync()
except (StoreBusyError, StaleChangeSetError):
    pass  # Nothing changed; sync again later.
except ScopeGoneError:
    dropped = replica.drop_scope(adapter.scope_id)
    unsent = [record.edit.content for record in dropped.pending + dropped.conflicts if record.edit]
# end: sync
