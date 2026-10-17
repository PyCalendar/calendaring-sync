# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`store` kept in a SQLite database file."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import fields
from datetime import datetime
from typing import Any, TypeVar, cast

from calendaring_sync.constants import DEFAULT_TIMEOUT, SCHEMA_VERSION
from calendaring_sync.error import (
    CalendaringSyncError,
    StoreBusyError,
    StoreError,
    StoreVersionError,
)
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore

_META = "store_meta"
_SCOPES = "scopes"
_RECORDS = "records"
_FEED = "feed"

_Row = TypeVar("_Row", Scope, Record, FeedEntry)


def _columns(cls: type[_Row]) -> tuple[str, ...]:
    """A table's columns, which carry the names of its dataclass's fields."""
    return tuple(f.name for f in fields(cls))


_SCOPE_COLUMNS = _columns(Scope)
_RECORD_COLUMNS = _columns(Record)
_FEED_COLUMNS = _columns(FeedEntry)

#: The schema, one statement each: ``executescript`` would commit the open transaction.
_SCHEMA = (
    f"""CREATE TABLE {_META} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    feed_head INTEGER NOT NULL,
    feed_trimmed INTEGER NOT NULL
)""",
    f"""INSERT INTO {_META} (id, schema_version, feed_head, feed_trimmed)
    VALUES (1, {SCHEMA_VERSION}, 0, 0)""",
    f"""CREATE TABLE {_SCOPES} (
    id TEXT PRIMARY KEY,
    token TEXT,
    fingerprint_scheme TEXT NOT NULL,
    media_type TEXT NOT NULL,
    last_sync_at TEXT,
    last_full_listing_at TEXT,
    listing_token TEXT
)""",
    f"""CREATE TABLE {_RECORDS} (
    local_key INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL REFERENCES {_SCOPES}(id) ON DELETE CASCADE,
    object_id TEXT,
    state TEXT NOT NULL,
    version TEXT,
    fingerprint TEXT,
    content TEXT NOT NULL,
    containers TEXT NOT NULL,
    logical_key TEXT,
    synced_at TEXT,
    last_entry INTEGER NOT NULL
)""",
    f"""CREATE UNIQUE INDEX {_RECORDS}_by_object_id ON {_RECORDS} (scope, object_id)""",
    f"""CREATE INDEX {_RECORDS}_by_state ON {_RECORDS} (state, last_entry)""",
    f"""CREATE TABLE {_FEED} (
    number INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    local_key INTEGER,
    object_id TEXT,
    at TEXT NOT NULL
)""",
)


def _containers(text: str) -> frozenset[str]:
    """Read back the JSON list of strings a record's containers are stored as."""
    value = json.loads(text)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Containers aren't a list of strings: {text!r}")
    return frozenset(value)


#: How the stored text of these columns turns back into a field's value;
#: every other column is stored as the value itself.
_DECODERS: dict[str, Callable[[Any], Any]] = {
    "state": RecordState,
    "kind": FeedEntryKind,
    "containers": _containers,
    "synced_at": datetime.fromisoformat,
    "last_sync_at": datetime.fromisoformat,
    "last_full_listing_at": datetime.fromisoformat,
    "at": datetime.fromisoformat,
}


def _encode(value: Any) -> Any:
    """A field's value as SQLite stores it."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, frozenset):
        return json.dumps(sorted(value))
    return value


def _values(obj: Scope | Record | FeedEntry, columns: tuple[str, ...]) -> tuple[Any, ...]:
    """The stored values of an object's fields, in ``columns`` order."""
    return tuple(_encode(getattr(obj, name)) for name in columns)


def _load(cls: type[_Row], row: sqlite3.Row) -> _Row:
    """Build an object from a row selected with its class's columns.

    Raises:
        StoreError: If a column holds a value the store never writes there.
    """
    values: dict[str, Any] = {}
    for name in row.keys():
        raw, decode = row[name], _DECODERS.get(name)
        try:
            values[name] = raw if raw is None or decode is None else decode(raw)
        except (ValueError, TypeError) as exc:
            raise StoreError(f"The store's {name} column holds a damaged value") from exc
    return cls(**values)


class SQLiteStore(StateStore):
    """A store in a SQLite database file, shared between threads and processes.

    One connection serves every thread, one transaction at a time. Writes
    take SQLite's write lock for their whole transaction, so two processes
    on one file never interleave. Reads don't take that lock, but an open
    read still makes another process's commit wait, so the store keeps
    reads short. A write waits up to ``timeout`` seconds for another
    process's transaction to end.

    Setting the file to SQLite's WAL journal mode lets one process's reads
    and another's writes overlap. The mode is kept in the file, so set it
    once with ``PRAGMA journal_mode = WAL``; WAL doesn't work on a network
    filesystem.

    Args:
        path: The database file. It's created, with the schema, if missing.
        timeout: Seconds to wait for another process before giving up.

    Raises:
        StoreVersionError: If a newer calendaring-sync wrote the file.
        StoreBusyError: If another process kept the file busy past ``timeout``.
        StoreError: If the file can't be opened or isn't a store.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__()
        with self._errors():
            self._conn = sqlite3.connect(
                path, timeout=timeout, isolation_level=None, check_same_thread=False
            )
        self._conn.row_factory = sqlite3.Row
        try:
            with self._errors():
                self._conn.execute("PRAGMA foreign_keys = ON")
                self._ensure_schema()
        except BaseException:
            self._conn.close()
            raise

    def _store_error(self, exc: Exception) -> CalendaringSyncError:
        if not isinstance(exc, sqlite3.Error):
            return super()._store_error(exc)
        # Errors from the sqlite3 module itself, such as a closed connection, carry no code.
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
            return StoreBusyError(str(exc))
        return StoreError(str(exc))

    def _meta(self, columns: str) -> sqlite3.Row:
        """Read ``columns`` from the one row of the store's own data."""
        row: sqlite3.Row | None = self._conn.execute(f"SELECT {columns} FROM {_META}").fetchone()
        if row is None:
            raise StoreError(f"The store's {_META} table has lost its row")
        return row

    def _version(self) -> int:
        """The schema version in ``store_meta``, ``0`` for a file with no schema yet."""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_META,)
        ).fetchone()
        if exists is None:
            return 0
        version: int = self._meta("schema_version")[0]
        return version

    def _ensure_schema(self) -> None:
        if self._version() == 0:
            # Checked again under the write lock: another process may have
            # created the schema in the meantime.
            with self.transaction():
                if self._version() == 0:
                    for statement in _SCHEMA:
                        self._conn.execute(statement)
        version = self._version()
        if version > SCHEMA_VERSION:
            raise StoreVersionError(
                f"The store has schema version {version}; "
                f"this calendaring-sync reads up to {SCHEMA_VERSION}"
            )

    def _close(self) -> None:
        self._conn.close()

    def _begin(self, *, write: bool) -> None:
        self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def _select(
        self, cls: type[_Row], table: str, clause: str, params: tuple[Any, ...] = ()
    ) -> list[_Row]:
        """Load the rows of ``table`` that ``clause`` selects, as ``cls``."""
        rows = self._conn.execute(
            f"SELECT {', '.join(_columns(cls))} FROM {table} {clause}", params
        )
        return [_load(cls, row) for row in rows]

    def _select_one(
        self, cls: type[_Row], table: str, clause: str, params: tuple[Any, ...]
    ) -> _Row | None:
        found = self._select(cls, table, clause, params)
        return found[0] if found else None

    def _insert(self, table: str, columns: tuple[str, ...], values: tuple[Any, ...]) -> int:
        marks = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", values
        )
        # Typed as optional, but an INSERT always sets it to the new row's id.
        return cast(int, cursor.lastrowid)

    def _get_scope(self, scope_id: str) -> Scope | None:
        return self._select_one(Scope, _SCOPES, "WHERE id = ?", (scope_id,))

    def _put_scope(self, scope: Scope) -> None:
        # An upsert, not INSERT OR REPLACE: replacing deletes the row first,
        # and the cascade would take the scope's records with it.
        marks = ", ".join("?" for _ in _SCOPE_COLUMNS)
        updates = ", ".join(f"{name} = excluded.{name}" for name in _SCOPE_COLUMNS[1:])
        self._conn.execute(
            f"INSERT INTO {_SCOPES} ({', '.join(_SCOPE_COLUMNS)}) VALUES ({marks}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            _values(scope, _SCOPE_COLUMNS),
        )

    def _delete_scope(self, scope_id: str) -> None:
        self._conn.execute(f"DELETE FROM {_SCOPES} WHERE id = ?", (scope_id,))

    def _scopes(self) -> list[Scope]:
        return self._select(Scope, _SCOPES, "ORDER BY id")

    def _insert_record(self, record: Record) -> int:
        columns = _RECORD_COLUMNS[1:]
        return self._insert(_RECORDS, columns, _values(record, columns))

    def _update_record(self, record: Record) -> None:
        columns = _RECORD_COLUMNS[1:]
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self._conn.execute(
            f"UPDATE {_RECORDS} SET {assignments} WHERE local_key = ?",
            (*_values(record, columns), record.local_key),
        )

    def _delete_record(self, local_key: int) -> None:
        self._conn.execute(f"DELETE FROM {_RECORDS} WHERE local_key = ?", (local_key,))

    def _get_record(self, local_key: int) -> Record | None:
        return self._select_one(Record, _RECORDS, "WHERE local_key = ?", (local_key,))

    def _find_record(self, scope_id: str, object_id: str) -> Record | None:
        return self._select_one(
            Record, _RECORDS, "WHERE scope = ? AND object_id = ?", (scope_id, object_id)
        )

    def _scope_records(self, scope_id: str) -> list[Record]:
        return self._select(Record, _RECORDS, "WHERE scope = ? ORDER BY local_key", (scope_id,))

    def _removed_records(self, through: int) -> list[Record]:
        return self._select(
            Record,
            _RECORDS,
            "WHERE state = ? AND last_entry <= ? ORDER BY local_key",
            (RecordState.REMOVED, through),
        )

    def _append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        self._conn.execute(f"UPDATE {_META} SET feed_head = feed_head + 1")
        number: int = self._meta("feed_head")[0]
        entry = FeedEntry(number, scope_id, kind, local_key, object_id, at)
        self._insert(_FEED, _FEED_COLUMNS, _values(entry, _FEED_COLUMNS))
        return entry

    def _entries(self, after: int, limit: int) -> list[FeedEntry]:
        return self._select(
            FeedEntry, _FEED, "WHERE number > ? ORDER BY number LIMIT ?", (after, limit)
        )

    def _feed_bounds(self) -> tuple[int, int]:
        trimmed, head = self._meta("feed_trimmed, feed_head")
        return trimmed, head

    def _trim_entries(self, through: int) -> None:
        self._conn.execute(f"DELETE FROM {_FEED} WHERE number <= ?", (through,))
        self._conn.execute(f"UPDATE {_META} SET feed_trimmed = max(feed_trimmed, ?)", (through,))
