# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from start import adapter, edit_on_server, replica

handled: list[object] = []


def handle(entry):
    handled.append((entry.kind, entry.object_id))


def reload_everything():
    handled.clear()


# start: read
from pathlib import Path

from calendaring_sync import TrimmedFeedError

CURSOR = Path("feed-cursor.txt")

cursor = int(CURSOR.read_text()) if CURSOR.exists() else 0
try:
    while entries := replica.read_feed(after=cursor):
        for entry in entries:
            handle(entry)
        cursor = entries[-1].number
        CURSOR.write_text(str(cursor))
except TrimmedFeedError:
    reload_everything()
    CURSOR.write_text(str(replica.feed_head()))
# end: read

edit_on_server(None)
adapter.sync()
cursor = replica.feed_head()

# start: trim
replica.trim_feed(through=cursor)
replica.purge(through=cursor)
# end: trim
