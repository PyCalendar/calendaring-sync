# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`store` kept in a SQLite database file."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from operator import attrgetter
from threading import Lock
from types import UnionType
from typing import Any, NamedTuple, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from calendaring_sync._checks import check_argument_types
from calendaring_sync.constants import DEFAULT_TIMEOUT, SCHEMA_VERSION
from calendaring_sync.error import (
    StoreBusyError,
    StoreError,
    StoreVersionError,
)
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore, Storage, StorageTransaction

_META = "store_meta"
_SCOPES = "scopes"
_RECORDS = "records"
_FEED = "feed"

_Row = TypeVar("_Row", Scope, Record, FeedEntry)


def _containers(text: str) -> frozenset[str]:
    """Read back the JSON list of strings a set of containers is stored as."""
    value = json.loads(text)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Containers aren't a list of strings: {text!r}")
    return frozenset(value)


def _encode_containers(value: frozenset[str]) -> str:
    return json.dumps(sorted(value))


def _bytes(value: object) -> bytes:
    """Check a value read back as content is bytes, as the store wrote it."""
    if not isinstance(value, bytes):
        raise ValueError(f"Content isn't bytes: {value!r}")
    return value


def _codec(tp: Any) -> tuple[Callable[[Any], Any] | None, Callable[[Any], Any] | None]:
    """How a field of type ``tp`` is written and read back; ``None`` keeps the value as is."""
    if isinstance(tp, type) and issubclass(tp, StrEnum):
        return None, tp
    if tp is datetime:
        return datetime.isoformat, datetime.fromisoformat
    if get_origin(tp) is frozenset:
        return _encode_containers, _containers
    if tp is bytes:
        return None, _bytes
    return None, None


class _Field(NamedTuple):
    """One field of a stored dataclass, as the table maps it."""

    name: str
    encode: Callable[[Any], Any] | None
    decode: Callable[[Any], Any] | None
    nested: type | None
    optional: bool


def _fields(cls: type) -> tuple[_Field, ...]:
    """The stored dataclass's fields, from its type hints.

    ``None`` is taken out of an optional type, and a field holding a
    dataclass is marked as nested, stored in columns of its own.

    Raises:
        TypeError: If a field's type is a union of more than one type
            besides ``None``, which one column can't hold.
    """
    hints = get_type_hints(cls)
    result = []
    for f in fields(cls):
        tp = hints[f.name]
        optional = get_origin(tp) in (Union, UnionType)
        if optional:
            types = [arg for arg in get_args(tp) if arg is not type(None)]
            if len(types) != 1:
                raise TypeError(f"{cls.__name__}.{f.name} is {tp}; a column holds one type")
            tp = types[0]
        nested = tp if isinstance(tp, type) and is_dataclass(tp) else None
        result.append(_Field(f.name, *_codec(tp), nested, optional))
    return tuple(result)


class _Column(NamedTuple):
    """One column of a table, how its value is read from the stored object, and how it's written."""

    name: str
    get: Callable[[Any], Any]
    encode: Callable[[Any], Any] | None


def _nested_getter(
    outer: Callable[[Any], Any], inner: Callable[[Any], Any]
) -> Callable[[Any], Any]:
    """Read ``inner`` from what ``outer`` reads, or ``None`` when that's ``None``."""

    def get(obj: Any) -> Any:
        value = outer(obj)
        return None if value is None else inner(value)

    return get


def _columns(cls: type) -> tuple[_Column, ...]:
    """A table's columns, named after its dataclass's fields.

    A field holding a dataclass maps to that dataclass's columns, prefixed
    with the field's name, so one row holds the whole object.
    """
    columns: list[_Column] = []
    for f in _fields(cls):
        if f.nested is None:
            columns.append(_Column(f.name, attrgetter(f.name), f.encode))
        else:
            columns.extend(
                _Column(f"{f.name}_{c.name}", _nested_getter(attrgetter(f.name), c.get), c.encode)
                for c in _columns(f.nested)
            )
    return tuple(columns)


class _Reader(NamedTuple):
    """How a row turns back into one dataclass: its fields, each with its column."""

    cls: type
    fields: tuple[tuple[_Field, str, _Reader | None], ...]


def _reader(cls: type, prefix: str = "") -> _Reader:
    """The reader for ``cls`` stored in columns starting with ``prefix``.

    A nested field is read from its dataclass's first column, which is never
    ``NULL`` when the field holds a value, so it tells whether there is one.

    Raises:
        TypeError: If a nested dataclass has no fields, or its first field
            can be ``None``, so no column can tell whether the field holds
            a value.
    """
    entries: list[tuple[_Field, str, _Reader | None]] = []
    for f in _fields(cls):
        if f.nested is None:
            entries.append((f, prefix + f.name, None))
        else:
            nested_fields = _fields(f.nested)
            if not nested_fields or nested_fields[0].optional:
                raise TypeError(
                    f"{f.nested.__name__} needs a first field that always holds a value, "
                    f"to mark whether {cls.__name__}.{f.name} holds one"
                )
            nested = _reader(f.nested, f"{prefix}{f.name}_")
            entries.append((f, nested.fields[0][1], nested))
    return _Reader(cls, tuple(entries))


def _names(columns: tuple[_Column, ...]) -> str:
    return ", ".join(column.name for column in columns)


def _marks(columns: tuple[_Column, ...]) -> str:
    return ", ".join("?" for _ in columns)


_SCOPE_COLUMNS = _columns(Scope)
_RECORD_COLUMNS = _columns(Record)
_FEED_COLUMNS = _columns(FeedEntry)

#: How each stored class is selected, and read back from its rows. Built
#: once here, so nothing in the module changes while stores run.
_SELECTS: dict[type, str] = {
    Scope: _names(_SCOPE_COLUMNS),
    Record: _names(_RECORD_COLUMNS),
    FeedEntry: _names(_FEED_COLUMNS),
}
_READERS: dict[type, _Reader] = {cls: _reader(cls) for cls in (Scope, Record, FeedEntry)}

# Every column but the key, which SQLite assigns.
_RECORD_VALUES = _RECORD_COLUMNS[1:]
_INSERT_RECORD = (
    f"INSERT INTO {_RECORDS} ({_names(_RECORD_VALUES)}) VALUES ({_marks(_RECORD_VALUES)})"
)
_UPDATE_RECORD = (
    f"UPDATE {_RECORDS} SET {', '.join(f'{c.name} = ?' for c in _RECORD_VALUES)} "
    "WHERE local_key = ?"
)
_INSERT_ENTRY = f"INSERT INTO {_FEED} ({_names(_FEED_COLUMNS)}) VALUES ({_marks(_FEED_COLUMNS)})"
# An upsert, not INSERT OR REPLACE: replacing deletes the row first, and the
# cascade would take the scope's records with it.
_PUT_SCOPE = (
    f"INSERT INTO {_SCOPES} ({_names(_SCOPE_COLUMNS)}) VALUES ({_marks(_SCOPE_COLUMNS)}) "
    f"ON CONFLICT (id) DO UPDATE SET "
    f"{', '.join(f'{c.name} = excluded.{c.name}' for c in _SCOPE_COLUMNS[1:])}"
)

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
    content BLOB,
    containers TEXT NOT NULL,
    logical_key TEXT,
    synced_at TEXT,
    last_entry INTEGER NOT NULL,
    edit_kind TEXT,
    edit_content BLOB,
    edit_fingerprint TEXT,
    edit_containers TEXT,
    edit_logical_key TEXT,
    edit_recorded_at TEXT,
    conflict_kind TEXT,
    conflict_remote_version TEXT,
    conflict_remote_fingerprint TEXT,
    conflict_remote_content BLOB,
    conflict_remote_containers TEXT,
    conflict_remote_logical_key TEXT,
    conflict_opened_at TEXT,
    conflict_holder TEXT,
    claims INTEGER NOT NULL
)""",
    f"""CREATE UNIQUE INDEX {_RECORDS}_by_object_id ON {_RECORDS} (scope, object_id)""",
    f"""CREATE INDEX {_RECORDS}_by_state ON {_RECORDS} (state, last_entry)""",
    f"""CREATE INDEX {_RECORDS}_by_scope_state ON {_RECORDS} (scope, state)""",
    f"""CREATE INDEX {_RECORDS}_by_logical_key ON {_RECORDS} (scope, logical_key)""",
    f"""CREATE INDEX {_RECORDS}_by_edit_logical_key ON {_RECORDS} (scope, edit_logical_key)""",
    f"""CREATE TABLE {_FEED} (
    number INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    local_key INTEGER,
    object_id TEXT,
    at TEXT NOT NULL
)""",
)


def _values(obj: Scope | Record | FeedEntry, columns: tuple[_Column, ...]) -> tuple[Any, ...]:
    """The stored values of an object's fields, in ``columns`` order."""
    values = []
    for column in columns:
        value = column.get(obj)
        values.append(value if value is None or column.encode is None else column.encode(value))
    return tuple(values)


def _load(reader: _Reader, row: sqlite3.Row) -> Any:
    """Build an object from a row selected with its class's columns.

    Raises:
        StoreError: If a column holds a value the store never writes there.
    """
    values: dict[str, Any] = {}
    for f, column, nested in reader.fields:
        raw = row[column]
        if nested is not None:
            values[f.name] = None if raw is None else _load(nested, row)
        elif raw is None or f.decode is None:
            values[f.name] = raw
        else:
            try:
                values[f.name] = f.decode(raw)
            except (ValueError, TypeError) as exc:
                raise StoreError(f"The store's {column} column holds a damaged value") from exc
    return reader.cls(**values)


# The paths at which SQLite opens a database private to one connection: in
# memory, or in a temporary file.
_PRIVATE = (":memory:", "")


def _connect(path: str | os.PathLike[str], timeout: float) -> sqlite3.Connection:
    """Open a connection to the store's file the way every one of its connections is opened."""
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(conn: sqlite3.Connection, columns: str) -> sqlite3.Row:
    """Read ``columns`` from the one row of the store's own data."""
    row: sqlite3.Row | None = conn.execute(f"SELECT {columns} FROM {_META}").fetchone()
    if row is None:
        raise StoreError(f"The store's {_META} table has lost its row")
    return row


def _version(conn: sqlite3.Connection) -> int:
    """The schema version in ``store_meta``, ``0`` for a file with no schema yet."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_META,)
    ).fetchone()
    if exists is None:
        return 0
    version: int = _meta(conn, "schema_version")[0]
    return version


class _SQLiteStorage(Storage):
    """A SQLite database file: a connection for writes, and one for reads where the file allows."""

    def __init__(self, path: str | os.PathLike[str], timeout: float) -> None:
        self._path = path
        self._timeout = timeout
        self._writer: sqlite3.Connection | None = None
        self._reader: sqlite3.Connection | None = None
        self.reads_beside_writes = True
        # Whether a write locks reads out of the file: in the default journal
        # mode it does, in WAL mode it doesn't.
        self._locks_reads_out = False
        # Read transactions take turns on the reader's connection, and where a
        # commit locks reads out, it takes a turn too, so no read in this
        # process is open while it runs.
        self._read_turn = Lock()

    def open(self) -> None:
        """Connect, and create the schema if the file has none."""
        self._writer = _connect(self._path, self._timeout)
        self._writer.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema(self._writer)
        if os.fspath(self._path) in _PRIVATE:
            # A second connection would open an empty database of its own.
            self.reads_beside_writes = False
            return
        self._reader = _connect(self._path, self._timeout)
        self._reader.execute("PRAGMA query_only = ON")
        mode = self._writer.execute("PRAGMA journal_mode").fetchone()[0]
        self._locks_reads_out = mode != "wal"
        if self._locks_reads_out:
            # A write that outgrows SQLite's page cache would otherwise write
            # into the file before it commits, which locks the reads out until
            # the commit.
            self._writer.execute("PRAGMA cache_spill = OFF")

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        if _version(conn) == 0:
            # Checked again under the write lock: another process may have
            # created the schema in the meantime.
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _version(conn) == 0:
                    for statement in _SCHEMA:
                        conn.execute(statement)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        version = _version(conn)
        if version > SCHEMA_VERSION:
            raise StoreVersionError(
                f"The store has schema version {version}; "
                f"this calendaring-sync reads up to {SCHEMA_VERSION}"
            )

    def begin(self, *, write: bool) -> StorageTransaction:
        writer = cast(sqlite3.Connection, self._writer)
        if write or self._reader is None:
            writer.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            return _SQLiteTransaction(self, writer, write=write)
        # Reads take turns on the reader's connection.
        self._read_turn.acquire()
        try:
            self._reader.execute("BEGIN")
        except BaseException:
            self._read_turn.release()
            raise
        return _SQLiteTransaction(self, self._reader, write=False, read_turn=True)

    def close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.close()
        finally:
            if self._writer is not None:
                self._writer.close()

    def error(self, exc: Exception) -> StoreError | None:
        if not isinstance(exc, sqlite3.Error):
            return None
        # Errors from the sqlite3 module itself, such as a closed connection, carry no code.
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
            return StoreBusyError(str(exc))
        return StoreError(str(exc))


class _SQLiteTransaction(StorageTransaction):
    """A transaction on one of the storage's connections."""

    def __init__(
        self,
        storage: _SQLiteStorage,
        conn: sqlite3.Connection,
        *,
        write: bool,
        read_turn: bool = False,
    ) -> None:
        super().__init__(write=write)
        self._storage = storage
        self._conn = conn
        # Whether the transaction holds the storage's read turn, and so gives it back.
        self._read_turn = read_turn

    def commit(self) -> None:
        if self.write and self._storage._locks_reads_out:
            # The reads open in this process end first, and new ones wait, so
            # only another process can keep the commit busy.
            self._storage._read_turn.acquire()
            self._read_turn = True
        self._conn.execute("COMMIT")
        self._end_read()

    def rollback(self) -> None:
        try:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
        finally:
            self._end_read()

    def _end_read(self) -> None:
        """Give the read turn back, if the transaction holds it."""
        if self._read_turn:
            self._read_turn = False
            self._storage._read_turn.release()

    def _select(
        self, cls: type[_Row], table: str, clause: str, params: tuple[Any, ...] = ()
    ) -> list[_Row]:
        """Load the rows of ``table`` that ``clause`` selects, as ``cls``."""
        reader = _READERS[cls]
        rows = self._conn.execute(f"SELECT {_SELECTS[cls]} FROM {table} {clause}", params)
        return [_load(reader, row) for row in rows]

    def _select_one(
        self, cls: type[_Row], table: str, clause: str, params: tuple[Any, ...]
    ) -> _Row | None:
        found = self._select(cls, table, clause, params)
        return found[0] if found else None

    def get_scope(self, scope_id: str) -> Scope | None:
        return self._select_one(Scope, _SCOPES, "WHERE id = ?", (scope_id,))

    def put_scope(self, scope: Scope) -> None:
        self._conn.execute(_PUT_SCOPE, _values(scope, _SCOPE_COLUMNS))

    def delete_scope(self, scope_id: str) -> None:
        self._conn.execute(f"DELETE FROM {_SCOPES} WHERE id = ?", (scope_id,))

    def scopes(self) -> list[Scope]:
        return self._select(Scope, _SCOPES, "ORDER BY id")

    def insert_record(self, record: Record) -> int:
        cursor = self._conn.execute(_INSERT_RECORD, _values(record, _RECORD_VALUES))
        # Typed as optional, but an INSERT always sets it to the new row's id.
        return cast(int, cursor.lastrowid)

    def update_record(self, record: Record) -> None:
        self._conn.execute(_UPDATE_RECORD, (*_values(record, _RECORD_VALUES), record.local_key))

    def delete_record(self, local_key: int) -> None:
        self._conn.execute(f"DELETE FROM {_RECORDS} WHERE local_key = ?", (local_key,))

    def get_record(self, local_key: int) -> Record | None:
        return self._select_one(Record, _RECORDS, "WHERE local_key = ?", (local_key,))

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        return self._select_one(
            Record, _RECORDS, "WHERE scope = ? AND object_id = ?", (scope_id, object_id)
        )

    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        # A UNION, not an OR: SQLite answers each half from its own index,
        # and would scan the table for the OR.
        return self._select(
            Record,
            _RECORDS,
            f"WHERE local_key IN (SELECT local_key FROM {_RECORDS} WHERE scope = ? "
            f"AND logical_key = ? UNION SELECT local_key FROM {_RECORDS} WHERE scope = ? "
            "AND edit_logical_key = ?) ORDER BY local_key",
            (scope_id, logical_key, scope_id, logical_key),
        )

    def scope_records(self, scope_id: str) -> list[Record]:
        return self._select(Record, _RECORDS, "WHERE scope = ? ORDER BY local_key", (scope_id,))

    def scope_records_in(self, scope_id: str, states: frozenset[RecordState]) -> list[Record]:
        marks = ", ".join("?" for _ in states)
        return self._select(
            Record,
            _RECORDS,
            f"WHERE scope = ? AND state IN ({marks}) ORDER BY local_key",
            (scope_id, *states),
        )

    def removed_records(self, through: int) -> list[Record]:
        return self._select(
            Record,
            _RECORDS,
            "WHERE state = ? AND last_entry <= ? ORDER BY local_key",
            (RecordState.REMOVED, through),
        )

    def append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        self._conn.execute(f"UPDATE {_META} SET feed_head = feed_head + 1")
        number: int = _meta(self._conn, "feed_head")[0]
        entry = FeedEntry(number, scope_id, kind, local_key, object_id, at)
        self._conn.execute(_INSERT_ENTRY, _values(entry, _FEED_COLUMNS))
        return entry

    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        return self._select(
            FeedEntry, _FEED, "WHERE number > ? ORDER BY number LIMIT ?", (after, limit)
        )

    def feed_bounds(self) -> tuple[int, int]:
        trimmed, head = _meta(self._conn, "feed_trimmed, feed_head")
        return trimmed, head

    def trim_entries(self, through: int) -> None:
        self._conn.execute(f"DELETE FROM {_FEED} WHERE number <= ?", (through,))
        self._conn.execute(f"UPDATE {_META} SET feed_trimmed = max(feed_trimmed, ?)", (through,))


@check_argument_types
class SQLiteStore(StateStore):
    """A store in a SQLite database file, shared between threads and processes.

    Writes take one connection, one transaction at a time, and SQLite's write
    lock for their whole transaction, so two processes on one file never
    interleave. Reads take a second connection, one at a time, so a read in
    one thread doesn't wait for another thread's write. A database at
    ``":memory:"`` or an empty path belongs to one connection, so it has
    only that one, and its transactions take turns.

    In SQLite's default journal mode, a commit locks reads out of the file.
    So it waits for the reads open in this process, and reads in another
    process make it wait up to ``timeout`` seconds, then raise
    :class:`~calendaring_sync.error.StoreBusyError`; the store keeps reads
    short for that. A write also keeps what it changes in memory until it
    commits, so that reads aren't locked out before then: a full listing
    takes about as much memory again as its change set holds. In WAL mode
    reads and a commit don't lock each other out, so a commit waits for no
    read and a write keeps nothing extra in memory. The mode is kept in the
    file, so set it once with ``PRAGMA journal_mode = WAL``; WAL doesn't
    work on a network filesystem.

    Args:
        path: The database file. It's created, with the schema, if missing.
        timeout: Seconds to wait for another process before giving up.

    Raises:
        TypeError: If ``path`` or ``timeout`` has the wrong type.
        StoreVersionError: If a newer calendaring-sync wrote the file.
        StoreBusyError: If another process kept the file busy past ``timeout``.
        StoreError: If the file can't be opened or isn't a store.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = DEFAULT_TIMEOUT) -> None:
        storage = _SQLiteStorage(path, timeout)
        super().__init__(storage)
        try:
            with self._errors():
                storage.open()
        except BaseException:
            storage.close()
            raise
