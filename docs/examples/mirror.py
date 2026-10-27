# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from start import adapter, edit_on_server, replica

# isort: split
# start: mirror
import sqlite3

from calendaring_sync import FeedEntryKind, RecordState

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    local_key INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    cursor INTEGER NOT NULL
);
"""


def mirror(replica, db):
    """Bring the events table up to date with the replica's change feed."""
    row = db.execute("SELECT cursor FROM feed_cursor").fetchone()
    cursor = row[0] if row else 0
    while entries := replica.read_feed(after=cursor):
        with db:
            for entry in entries:
                apply(replica, db, entry)
            cursor = entries[-1].number
            db.execute(
                "INSERT INTO feed_cursor VALUES (0, ?) ON CONFLICT(id) DO UPDATE SET cursor = ?",
                (cursor, cursor),
            )


def apply(replica, db, entry):
    """Bring the row of one feed entry's record up to date."""
    if entry.kind is FeedEntryKind.SCOPE_DROPPED:
        db.execute("DELETE FROM events WHERE scope = ?", (entry.scope,))
        return
    record = replica.record(entry.local_key)
    if record is None or record.state is RecordState.REMOVED or record.content is None:
        db.execute("DELETE FROM events WHERE local_key = ?", (entry.local_key,))
    else:
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?) ON CONFLICT(local_key) DO UPDATE SET content = ?",
            (record.local_key, record.scope, record.content, record.content),
        )


db = sqlite3.connect("app.db")
db.executescript(SCHEMA)
adapter.sync()
mirror(replica, db)
# end: mirror

# The server moves the meeting, then deletes it; each sync's mirror follows.
edit_on_server(b"Team meeting, moved")
adapter.sync()
mirror(replica, db)
moved = db.execute("SELECT content FROM events").fetchall()
edit_on_server(None)
adapter.sync()
mirror(replica, db)
after_delete = db.execute("SELECT content FROM events").fetchall()
db.close()
