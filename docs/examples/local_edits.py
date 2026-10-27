# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from start import adapter, replica

# start: record
lunch = adapter.create(b"Lunch with Ana")
meeting = replica.find_record("work", "meeting")
if meeting is not None:
    adapter.change(meeting.local_key, b"Team meeting, room 3")
adapter.sync()
# end: record

# start: undo
adapter.change(lunch, b"Lunch with Ana and Ben")
replica.discard(lunch)
# end: undo

# start: delete
replica.delete(lunch)
adapter.sync()
# end: delete
