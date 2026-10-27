# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from start import adapter, meeting, replica

adapter.change(meeting.local_key, b"Team meeting, room 3")

# start: inspect
from calendaring_sync import RecordState

scope = replica.scope(adapter.scope_id)
token, last_sync, last_listing = scope.token, scope.last_sync_at, scope.last_full_listing_at

unsent = replica.records(adapter.scope_id, states=frozenset({RecordState.PENDING}))
conflicts = replica.records(adapter.scope_id, states=frozenset({RecordState.CONFLICT}))
blocked = adapter.waiting()
# end: inspect

# start: repair
adapter.sync(full_listing=True)

replica.reset(adapter.scope_id)
adapter.sync()
# end: repair
