# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The stores: transactions, errors, threads, and what each one keeps."""

from __future__ import annotations

import gc
import pickle
import re
import sqlite3
import weakref
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import cast
from unittest.mock import patch

import pytest

from calendaring_sync import (
    Change,
    Conflict,
    ConflictKind,
    DuplicateObjectError,
    EditKind,
    FeedEntryKind,
    LogicalKeyTaken,
    MemoryData,
    MemoryKeeper,
    MemoryStore,
    RecordState,
    RecordValueError,
    Removed,
    Replica,
    SessionState,
    SQLiteStore,
    StateStore,
    StorageTransaction,
    StoreBusyError,
    StoreError,
    StoreVersionError,
    TransactionError,
    TransactionPermissionError,
    TransactionRequiredError,
    Unchanged,
    UnexpectedStoreError,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.constants import ISSUES_URL, SCHEMA_VERSION
from calendaring_sync.store.memory import _MemoryStorage
from calendaring_sync.store.sqlite import (
    _FEED,
    _META,
    _RECORDS,
    _SCHEMA,
    _reader,
    _SQLiteStorage,
    _SQLiteTransaction,
    _version,
)
from calendaring_sync.tests._helpers import (
    _CONFLICT,
    _EDIT,
    _FIELDS,
    _OTHER,
    _REMOVED,
    _SCOPE,
    _START,
    _WAIT,
    _assert_refused,
    _change,
    _changed,
    _claim,
    _conflicted,
    _content,
    _create,
    _edited,
    _enter,
    _key,
    _local_record,
    _new_scope,
    _open_session,
    _page,
    _pending,
    _pushing,
    _seeded,
    _snapshot,
    _states,
    _summary,
    _sync,
)
from calendaring_sync.tests._storage import OneAtATime

# How long a test store waits for another, where the wait is meant to run out.
_SHORT_TIMEOUT = 0.1


@contextmanager
def _write_with_scope(store: StateStore) -> Iterator[None]:
    """A write transaction on a store that already holds the test scope."""
    with store.transaction():
        store.put_scope(_new_scope())
        yield


def _add_scope(store: StateStore) -> None:
    """Write the test scope to a store, in a transaction of its own."""
    with _write_with_scope(store):
        pass


def _scope_ids(store: StateStore) -> list[str]:
    """The ids of the scopes a store holds, read in a transaction of their own."""
    with store.transaction(write=False):
        return [scope.id for scope in store.scopes()]


@contextmanager
def _held_open(
    store: StateStore, *, write: bool, work: Callable[[], object] = lambda: None
) -> Iterator[None]:
    """A transaction that runs ``work`` in another thread, then stays open for the block."""
    started, finish = Event(), Event()

    def hold() -> None:
        with store.transaction(write=write):
            work()
            started.set()
            finish.wait(_WAIT)

    with ThreadPoolExecutor(1) as pool:
        held = pool.submit(hold)
        assert started.wait(_WAIT)
        try:
            yield
        finally:
            finish.set()
        held.result(_WAIT)


@contextmanager
def _busy_pair(db_path: Path) -> Iterator[tuple[SQLiteStore, SQLiteStore]]:
    """Two stores on one file; the second gives up quickly when the first holds it."""
    with SQLiteStore(db_path) as holder, SQLiteStore(db_path, timeout=_SHORT_TIMEOUT) as waiter:
        yield holder, waiter


def _edit_file(path: Path, sql: str, params: tuple[object, ...]) -> None:
    """Change a store's file directly, as a damaged or newer file would look."""
    conn = sqlite3.connect(path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


# The server's copy in a conflict where the server removed the object.
_GONE = Conflict(ConflictKind.REMOVED_REMOTELY, None, None, None, frozenset(), None, _START)


_PENDING = _local_record(RecordState.PENDING)


_CONFLICTED = _local_record(RecordState.CONFLICT)


# Records holding what their state can't, one for each rule a store checks.
_BAD_RECORDS = {
    "pending without an edit": replace(_PENDING, edit=None),
    "synced with an edit": replace(_local_record(RecordState.SYNCED), edit=_EDIT),
    "conflict without its server side": replace(_CONFLICTED, conflict=None),
    "synced with a server side": replace(_local_record(RecordState.SYNCED), conflict=_CONFLICT),
    "no synced copy for a change": replace(_PENDING, content=None),
    "synced copy for a create": replace(_PENDING, edit=replace(_EDIT, kind=EditKind.CREATE)),
    "a delete holding content": replace(_PENDING, edit=replace(_EDIT, kind=EditKind.DELETE)),
    "a change without content": replace(_PENDING, edit=replace(_EDIT, content=None)),
    "a conflict about the wrong edit": replace(
        _CONFLICTED, conflict=replace(_CONFLICT, kind=ConflictKind.DELETED_LOCALLY)
    ),
    "a removal holding the server's copy": replace(
        _CONFLICTED, conflict=replace(_CONFLICT, kind=ConflictKind.REMOVED_REMOTELY)
    ),
    "a change the server holds without its copy": replace(
        _CONFLICTED, conflict=replace(_GONE, kind=ConflictKind.BOTH_CHANGED)
    ),
    "part of a removed server copy": replace(
        _CONFLICTED, conflict=replace(_GONE, remote_version="v2")
    ),
    "a refused create holding the server's copy": replace(
        _CONFLICTED,
        content=None,
        edit=replace(_EDIT, kind=EditKind.CREATE),
        conflict=replace(_CONFLICT, kind=ConflictKind.LOGICAL_KEY_TAKEN),
    ),
    "a holder on another kind": replace(_CONFLICTED, conflict=replace(_CONFLICT, holder="b")),
}


# Every store operation that only reads, called so it works on an empty store.
_READS: dict[str, Callable[[StateStore], object]] = {
    "get_scope": lambda store: store.get_scope(_SCOPE),
    "scopes": lambda store: store.scopes(),
    "get_record": lambda store: store.get_record(1),
    "find_record": lambda store: store.find_record(_SCOPE, "a"),
    "find_by_logical_key": lambda store: store.find_by_logical_key(_SCOPE, "k"),
    "scope_records": lambda store: store.scope_records(_SCOPE),
    "removed_records": lambda store: store.removed_records(0),
    "entries": lambda store: store.entries(0, 1),
    "feed_bounds": lambda store: store.feed_bounds(),
}


# Every store operation that writes.
_WRITES: dict[str, Callable[[StateStore], object]] = {
    "put_scope": lambda store: store.put_scope(_new_scope()),
    "delete_scope": lambda store: store.delete_scope(_SCOPE),
    "insert_record": lambda store: store.insert_record(_local_record(RecordState.SYNCED)),
    "update_record": lambda store: store.update_record(
        _local_record(RecordState.SYNCED, local_key=1)
    ),
    "delete_record": lambda store: store.delete_record(1),
    "append_entry": lambda store: store.append_entry(
        _SCOPE, FeedEntryKind.CHANGED, None, None, _START
    ),
    "trim_entries": lambda store: store.trim_entries(0),
}


def _current(store: StateStore) -> StorageTransaction:
    """The storage's transaction this thread has open in ``store``."""
    current = store._thread.current
    assert current is not None
    return current


def _writer(store: StateStore) -> sqlite3.Connection:
    """The connection a SQLite store writes on."""
    return cast(sqlite3.Connection, cast(_SQLiteStorage, store._storage)._writer)


def _break(
    store: StateStore, monkeypatch: pytest.MonkeyPatch, step: str, error: BaseException
) -> None:
    """Make ``step`` raise ``error``: the storage's ``begin``, or each transaction's method."""
    storage = store._storage

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    if step == "begin":
        monkeypatch.setattr(storage, "begin", fail)
        return
    begin = storage.begin

    def begin_broken(*, write: bool) -> StorageTransaction:
        current = begin(write=write)
        setattr(current, step, fail)
        return current

    monkeypatch.setattr(storage, "begin", begin_broken)


class _Pickled(MemoryKeeper):
    """A keeper that saves a memory store's data in a file with :mod:`pickle`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> MemoryData | None:
        loaded: MemoryData | None = (
            pickle.loads(self._path.read_bytes()) if self._path.exists() else None
        )
        return loaded

    def save(self, data: MemoryData) -> None:
        # Written beside the file, then moved over it, so a failure halfway
        # leaves the last saved data.
        written = self._path.with_name(self._path.name + ".new")
        written.write_bytes(pickle.dumps(data))
        written.replace(self._path)

    def error(self, exc: Exception) -> StoreError | None:
        return StoreError(str(exc)) if isinstance(exc, OSError) else None


# Dataclasses the SQLite store can't map: a column holds one type, and the
# first field of a nested dataclass marks whether it's there, so it must
# exist and can't be optional. At module level, so their type hints resolve.
@dataclass(frozen=True)
class _TwoTypes:
    value: str | int | None


@dataclass(frozen=True)
class _Inner:
    note: str | None


@dataclass(frozen=True)
class _Outer:
    inner: _Inner | None


@dataclass(frozen=True)
class _Empty:
    pass


@dataclass(frozen=True)
class _HoldsEmpty:
    inner: _Empty | None


class TestTransactions:
    """Reads and writes run inside a transaction, and only there."""

    @pytest.mark.parametrize("call", _READS.values(), ids=_READS)
    def test_reads_run_in_a_read_transaction(self, store, call):
        with store.transaction(write=False):
            call(store)

    def test_every_operation_is_in_the_tables(self):
        public = {name for name in vars(StateStore) if not name.startswith("_")}
        operations = public - {"transaction", "close"}
        assert operations == _READS.keys() | _WRITES.keys()
        # A storage's transaction offers the same operations, plus its own ends.
        offered = {name for name in vars(StorageTransaction) if not name.startswith("_")}
        assert offered - {"commit", "rollback", "scope_records_in"} == operations

    @pytest.mark.parametrize("call", [*_READS.values(), *_WRITES.values()], ids=[*_READS, *_WRITES])
    def test_operations_outside_a_transaction_raise(self, store, call):
        with pytest.raises(TransactionRequiredError):
            call(store)

    @pytest.mark.parametrize("call", _WRITES.values(), ids=_WRITES)
    def test_a_read_transaction_refuses_writes(self, store, call):
        with store.transaction(write=False), pytest.raises(TransactionPermissionError):
            call(store)
        with store.transaction(write=False):
            assert (store.scopes(), store.feed_bounds()) == ([], (0, 0))

    def test_transactions_do_not_nest(self, replica, store):
        with store.transaction(), pytest.raises(TransactionError, match="already open"):
            _enter(store.transaction())
        # A replica's methods each open their own, so they can't run inside one.
        with store.transaction(write=False), pytest.raises(TransactionError, match="already open"):
            replica.scopes()


class TestRecords:
    """What a store keeps for each record, and what it refuses to keep."""

    @pytest.mark.parametrize(
        "states, expected",
        [
            (None, ["a", "b"]),
            (frozenset(), []),
            (frozenset({RecordState.PENDING}), ["a"]),
            (frozenset({RecordState.PENDING, RecordState.CONFLICT}), ["a", "b"]),
            (frozenset({RecordState.SYNCED, RecordState.REMOVED}), []),
        ],
    )
    def test_scope_records_filter_by_state(self, replica, store, states, expected):
        _edited(replica)
        with store.transaction(write=False):
            current = _current(store)
            with (
                patch.object(current, "scope_records", wraps=current.scope_records) as every,
                patch.object(current, "scope_records_in", wraps=current.scope_records_in) as some,
            ):
                records = store.scope_records(_SCOPE, states=states)
        assert [r.object_id for r in records] == expected
        assert replica.records(_SCOPE, states=states) == records
        # An empty set is answered by the store, so no storage sees one.
        assert (every.called or some.called) == (states != frozenset())
        assert some.called == bool(states)

    @pytest.mark.parametrize("holder", [None, "b"])
    def test_a_refused_create_keeps_its_holder(self, store, holder):
        record = replace(
            _CONFLICTED,
            content=None,
            edit=replace(_EDIT, kind=EditKind.CREATE),
            conflict=replace(_GONE, kind=ConflictKind.LOGICAL_KEY_TAKEN, holder=holder),
        )
        with _write_with_scope(store):
            key = store.insert_record(record)
        with store.transaction(write=False):
            assert store.get_record(key) == replace(record, local_key=key)

    @pytest.mark.parametrize("record", _BAD_RECORDS.values(), ids=_BAD_RECORDS)
    def test_records_holding_what_their_state_cant_are_refused(self, store, record):
        with _write_with_scope(store):
            key = store.insert_record(_local_record(RecordState.SYNCED))
        for write in (store.insert_record, store.update_record):
            # The record is checked before the transaction, in or out of one.
            with pytest.raises(RecordValueError):
                write(replace(record, local_key=key))
            with store.transaction(), pytest.raises(RecordValueError):
                write(replace(record, local_key=key))
        with store.transaction(write=False):
            assert store.scope_records(_SCOPE) == [_local_record(RecordState.SYNCED, local_key=key)]

    def test_object_ids_are_unique_per_scope(self, replica, store):
        _seeded(replica)
        _, b = replica.records(_SCOPE)
        with store.transaction(), pytest.raises(DuplicateObjectError):
            store.insert_record(_local_record(RecordState.SYNCED, object_id="a"))
        with store.transaction(), pytest.raises(DuplicateObjectError):
            store.update_record(_local_record(b.state, object_id="a", local_key=b.local_key))

    def test_inserting_into_a_missing_scope_raises(self, store):
        with store.transaction(), pytest.raises(UnknownScopeError):
            store.insert_record(_local_record(RecordState.SYNCED))

    def test_updating_a_missing_record_raises(self, store):
        with store.transaction(), pytest.raises(UnknownRecordError):
            store.update_record(_local_record(RecordState.SYNCED, local_key=999))


class TestFailures:
    """A failure inside a storage reaches the caller as a library error."""

    @pytest.mark.parametrize("write", [True, False], ids=["write", "read"])
    @pytest.mark.parametrize("step", ["begin", "commit", "scopes"])
    def test_a_failure_inside_the_store_is_unexpected_and_keeps_nothing(
        self, store, monkeypatch, step, write
    ):
        bug = RuntimeError(f"{step} has a bug")
        _break(store, monkeypatch, step, bug)
        with (
            pytest.raises(UnexpectedStoreError, match=re.escape(ISSUES_URL)) as raised,
            _write_with_scope(store) if write else store.transaction(write=False),
        ):
            store.scopes()
        assert raised.value.__cause__ is bug
        assert not isinstance(raised.value, StoreError)
        monkeypatch.undo()
        # Neither kind of transaction is left waiting on the one that failed.
        assert _scope_ids(store) == []
        with store.transaction():
            pass

    def test_a_bug_in_someone_elses_storage_is_reported_to_them(self, monkeypatch):
        storage = OneAtATime()
        monkeypatch.setattr(type(storage), "__module__", "their_storage")
        store = StateStore(storage)
        _break(store, monkeypatch, "scopes", RuntimeError("a bug"))
        with pytest.raises(UnexpectedStoreError) as raised, store.transaction(write=False):
            store.scopes()
        assert str(raised.value).endswith("This is a bug in their_storage.OneAtATime")
        assert ISSUES_URL not in str(raised.value)

    def test_interrupts_pass_through_unchanged(self, store, monkeypatch):
        _break(store, monkeypatch, "scopes", KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt), store.transaction(write=False):
            store.scopes()
        monkeypatch.undo()
        with store.transaction(write=False):
            assert store.feed_bounds() == (0, 0)

    @pytest.mark.parametrize("first", [KeyboardInterrupt(), RuntimeError("the block failed")])
    def test_a_failed_rollback_keeps_what_ended_the_block(self, store, monkeypatch, first):
        _break(store, monkeypatch, "rollback", RuntimeError("rollback broke"))
        with pytest.raises(type(first)) as raised, store.transaction():
            raise first
        assert raised.value is first
        assert "Rolling the transaction back failed too" in raised.value.__notes__[0]
        monkeypatch.undo()
        assert _scope_ids(store) == []


class TestThreads:
    """Threads sharing one store."""

    def test_threads_share_one_store(self, replica):
        _seeded(replica)

        def write(n: int) -> None:
            scope = f"scope-{n}"
            objects = {f"o{i}": _changed(f"f{n}-{i}") for i in range(20)}
            _sync(replica, _page(None, "T1", objects, full=True, scope=scope))
            if n % 2:
                replica.drop_scope(scope)

        def edit(n: int) -> None:
            for i in range(3):
                key = _create(replica, f"n{n}-{i}")
                _change(replica, key, f"m{n}-{i}")

        def read(_: int) -> None:
            for scope in replica.scopes():
                # A writer may drop the scope between the two calls.
                with suppress(UnknownScopeError):
                    replica.records(scope.id)

        def work(n: int) -> None:
            write(n)
            edit(n)
            read(n)

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(work, range(16)))
        kept = [f"scope-{n}" for n in range(0, 16, 2)]
        assert [s.id for s in replica.scopes()] == sorted([_SCOPE, *kept])
        assert all(len(replica.records(scope)) == 20 for scope in kept)
        created = [r for r in replica.records(_SCOPE) if r.edit is not None]
        assert len(created) == 48
        assert all(r.edit.content == _content(r.edit.fingerprint) for r in created)
        assert [e.number for e in replica.read_feed()] == list(range(1, replica.feed_head() + 1))

    @pytest.mark.parametrize(
        "open_store, beside",
        [
            (lambda path: MemoryStore(), True),
            (SQLiteStore, True),
            (lambda path: StateStore(OneAtATime()), False),
            (lambda path: SQLiteStore(":memory:"), False),
        ],
        ids=["memory", "sqlite", "memory one at a time", "sqlite in memory"],
    )
    def test_a_read_runs_beside_a_write(self, db_path, open_store, beside):
        with open_store(db_path) as store:
            _add_scope(store)

            def write() -> None:
                store.put_scope(replace(_new_scope(), id=_OTHER))
                # More than SQLite's page cache holds, as a large listing writes.
                big = replace(_local_record(RecordState.SYNCED), content=b"c" * 4_000_000)
                store.insert_record(big)

            with ThreadPoolExecutor(1) as pool, _held_open(store, write=True, work=write):
                reader = pool.submit(_scope_ids, store)
                if beside:
                    # It sees what was committed, not the write still open.
                    assert reader.result(_WAIT) == [_SCOPE]
                else:
                    with pytest.raises(TimeoutError):
                        reader.result(_SHORT_TIMEOUT)
            assert reader.result() == ([_SCOPE] if beside else [_SCOPE, _OTHER])
            # The read left the write's work alone.
            assert _scope_ids(store) == [_SCOPE, _OTHER]

    def test_a_write_isnt_starved_by_reads(self, replica):
        _seeded(replica)
        done = Event()

        def read() -> None:
            while not done.is_set():
                replica.records(_SCOPE)

        with ThreadPoolExecutor(3) as pool:
            readers = [pool.submit(read) for _ in range(3)]
            try:
                for i in range(20):
                    _create(replica, f"n{i}")
            finally:
                done.set()
            for reader in readers:
                reader.result(_WAIT)
        assert len(replica.records(_SCOPE)) == 22


class TestClosing:
    """Closing a store."""

    def test_a_store_is_a_context_manager(self, make_store):
        with make_store() as store:
            assert _scope_ids(store) == []

    def test_a_closed_store_refuses_transactions(self, store):
        store.close()
        store.close()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction(write=False))

    def test_closing_inside_a_transaction_is_refused(self, store):
        with (
            _write_with_scope(store),
            pytest.raises(TransactionError, match="after its transaction"),
        ):
            store.close()
        assert _scope_ids(store) == [_SCOPE]

    def test_close_waits_for_open_transactions(self, store):
        with ThreadPoolExecutor(1) as pool, _held_open(store, write=False):
            closing = pool.submit(store.close)
            with pytest.raises(TimeoutError):
                closing.result(_SHORT_TIMEOUT)
        closing.result()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction(write=False))


class TestMemoryStore:
    """A memory store, and a keeper that keeps its data elsewhere."""

    def test_a_memory_store_can_keep_its_data_elsewhere(self, tmp_path, clock):
        path = tmp_path / "store.pickle"
        keeper = _Pickled(path)
        store = MemoryStore(keeper=keeper)
        replica = _edited(Replica(store, clock=clock))
        expected = _snapshot(replica, store)
        # Reads never save, and never load again.
        with (
            patch.object(keeper, "save", wraps=keeper.save) as save,
            patch.object(keeper, "load", wraps=keeper.load) as load,
        ):
            replica.records(_SCOPE)
            replica.read_feed()
        assert not (save.called or load.called)
        # A write that fails saves nothing.
        with pytest.raises(RuntimeError), store.transaction():
            store.put_scope(replace(_new_scope(), id=_OTHER))
            raise RuntimeError
        keeper = _Pickled(path)
        reopened = MemoryStore(keeper=keeper)
        again = Replica(reopened, clock=clock)
        assert _snapshot(again, reopened) == expected

        # A save that fails fails the write, keeps the last data and leaves the store
        # usable; the keeper says which of its failures are store errors.
        def add_scope() -> None:
            with reopened.transaction():
                reopened.put_scope(replace(_new_scope(), id=_OTHER))

        with patch.object(keeper, "save", side_effect=OSError("disk full")):
            _assert_refused(again, reopened, add_scope, StoreError, "^disk full$")
        _sync(again, _page("T2", "T3", {"a": Removed()}))
        expected = _snapshot(again, reopened)
        third = MemoryStore(keeper=_Pickled(path))
        assert _snapshot(Replica(third, clock=clock), third) == expected

    def test_a_memory_store_lets_go_of_old_data(self):
        store = MemoryStore()
        with ThreadPoolExecutor(1) as pool:
            # The thread stays alive, idle, after its read.
            pool.submit(_scope_ids, store).result()
            old = weakref.ref(cast(_MemoryStorage, store._storage)._committed)
            _add_scope(store)
            gc.collect()
            assert old() is None

    @pytest.mark.parametrize("step", ["load", "save"])
    def test_a_keepers_failure_it_doesnt_name_is_a_bug(self, step):
        class Failing(MemoryKeeper):
            def load(self) -> MemoryData | None:
                if step == "load":
                    raise OSError("disk full")
                return None

            def save(self, data: MemoryData) -> None:
                raise OSError("disk full")

        bug = r"\.Failing failed unexpectedly with OSError"
        if step == "load":
            with pytest.raises(UnexpectedStoreError, match=bug):
                MemoryStore(keeper=Failing())
            return
        store = MemoryStore(keeper=Failing())
        with pytest.raises(UnexpectedStoreError, match=bug):
            _add_scope(store)


class TestSQLiteFile:
    """A SQLite store's file and its schema."""

    def test_state_survives_reopening(self, db_path, clock):
        with SQLiteStore(db_path) as store:
            replica = _edited(Replica(store, clock=clock))
            created = _create(
                replica,
                "fn",
                **_FIELDS,
            )
            # The edit and server side that store no content, a delete and a
            # removal, and a record the server removed.
            changes: dict[str, Change] = {"c": _changed("fc"), "d": _changed("fd"), "b": Removed()}
            _sync(replica, _page("T2", "T3", changes))
            replica.delete(_key(replica, "c"))
            _sync(replica, _page("T3", "T4", {"d": Removed()}))
            # A create refused with the object holding its key, and a push in flight.
            with _pushing(replica, "T4", "T5") as session:
                session.settle(_claim(session, created), LogicalKeyTaken("b"))
                session.claim(_key(replica))
            expected = _snapshot(replica, store)
            assert [_summary(r) for r in replica.records(_SCOPE)] == [
                (RecordState.IN_FLIGHT, EditKind.CHANGE, None),
                _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
                _conflicted(EditKind.CREATE, ConflictKind.LOGICAL_KEY_TAKEN),
                _pending(EditKind.DELETE),
                _REMOVED,
            ]
        with SQLiteStore(db_path) as store:
            assert _snapshot(Replica(store, clock=clock), store) == expected

    def test_interrupted_ingest_leaves_the_token_after_reopening(self, db_path, clock):
        with SQLiteStore(db_path) as store:
            replica = _seeded(Replica(store, clock=clock))
            with pytest.raises(RuntimeError), _open_session(replica) as session:
                session.ingest(_page("T1", "L", {"a": Unchanged()}, full=True, truncated=True))
                raise RuntimeError("process stops")
        with SQLiteStore(db_path) as store:
            replica = Replica(store, clock=clock)
            assert replica.scope(_SCOPE).token == "T1"
            assert _states(replica) == {"a": RecordState.SYNCED, "b": RecordState.SYNCED}

    def test_a_schema_another_process_created_first_is_used(self, db_path, monkeypatch):
        SQLiteStore(db_path).close()
        looks: list[int] = []

        def version(conn: sqlite3.Connection) -> int:
            # The first look sees an empty file; under the write lock the schema is there.
            looks.append(0)
            return 0 if len(looks) == 1 else _version(conn)

        monkeypatch.setattr("calendaring_sync.store.sqlite._version", version)
        with SQLiteStore(db_path) as store:
            assert _scope_ids(store) == []
        assert len(looks) == 3

    @pytest.mark.parametrize(
        "cls, message",
        [
            (_TwoTypes, "_TwoTypes.value is"),
            (_Outer, "_Inner needs a first field"),
            (_HoldsEmpty, "_Empty needs a first field"),
        ],
    )
    def test_a_dataclass_the_store_cant_map_is_refused(self, cls, message):
        with pytest.raises(TypeError, match=message):
            _reader(cls)

    def test_newer_schema_is_refused(self, db_path):
        SQLiteStore(db_path).close()
        _edit_file(db_path, f"UPDATE {_META} SET schema_version = ?", (SCHEMA_VERSION + 1,))
        with pytest.raises(StoreVersionError):
            SQLiteStore(db_path)

    # A statement that fails rolls the schema back; one that ends the
    # transaction early leaves the schema, and its own COMMIT fails.
    @pytest.mark.parametrize(
        "last, kept", [("CREATE TABLE broken (", False), ("COMMIT", True)], ids=["fails", "ends"]
    )
    def test_a_schema_that_fails_halfway_leaves_none(self, db_path, monkeypatch, last, kept):
        monkeypatch.setattr("calendaring_sync.store.sqlite._SCHEMA", (*_SCHEMA, last))
        with pytest.raises(StoreError):
            SQLiteStore(db_path)
        monkeypatch.undo()
        with closing(sqlite3.connect(db_path)) as connection:
            tables = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        assert bool(tables) is kept
        with SQLiteStore(db_path) as store:
            assert _scope_ids(store) == []


class TestSQLiteBusy:
    """A SQLite file another process or thread is using."""

    # Another process's write makes a write busy as it starts, its read as it commits.
    @pytest.mark.parametrize("held", ["write", "read"])
    def test_busy_file_raises_store_busy_error_and_stays_usable(self, db_path, held):
        with _busy_pair(db_path) as (holder, waiter):
            with (
                _held_open(holder, write=held == "write", work=holder.scopes),
                pytest.raises(StoreBusyError),
            ):
                _add_scope(waiter)
            assert _scope_ids(waiter) == []
            _add_scope(waiter)
            assert _scope_ids(holder) == [_SCOPE]

    def test_ingest_while_busy_changes_nothing_and_can_be_retried(self, db_path, clock):
        with _busy_pair(db_path) as (holder, waiter):
            replica = Replica(waiter, clock=clock)
            first = _page(None, "T1", {"a": _changed("fa")}, full=True)
            with _open_session(replica) as session:
                with holder.transaction(), pytest.raises(StoreBusyError):
                    session.ingest(first)
                assert (session.state, replica.records(_SCOPE)) == (SessionState.IDLE, [])
                session.ingest(first)
            assert replica.scope(_SCOPE).token == "T1"

    @pytest.mark.parametrize("mode, locks_reads_out", [("delete", True), ("wal", False)])
    def test_a_commit_waits_for_reads_here_where_it_locks_them_out(
        self, db_path, mode, locks_reads_out
    ):
        _edit_file(db_path, f"PRAGMA journal_mode = {mode}", ())
        with SQLiteStore(db_path, timeout=_SHORT_TIMEOUT) as store:
            # Where a commit locks reads out, a write keeps its changes in memory:
            # the pragma gives the pages it holds before spilling, 0 for never.
            spills = _writer(store).execute("PRAGMA cache_spill").fetchone()[0]
            assert bool(spills) is not locks_reads_out
            with (
                ThreadPoolExecutor(1) as pool,
                _held_open(store, write=False, work=store.scopes),
            ):
                writing = pool.submit(_add_scope, store)
                if locks_reads_out:
                    # Past the timeout it's still waiting, not busy.
                    with pytest.raises(TimeoutError):
                        writing.result(_SHORT_TIMEOUT * 3)
                else:
                    writing.result(_WAIT)
            writing.result()
            assert _scope_ids(store) == [_SCOPE]

    def test_a_read_that_cant_begin_gives_its_turn_back(self, db_path, monkeypatch):
        class Refusing:
            def execute(self, *args: object) -> None:
                raise sqlite3.OperationalError("disk I/O error")

        with SQLiteStore(db_path) as store:
            monkeypatch.setattr(store._storage, "_reader", Refusing())
            with pytest.raises(StoreError, match="disk I/O"):
                _enter(store.transaction(write=False))
            monkeypatch.undo()
            # A turn kept by the failed read would hold every later read forever.
            assert not cast(_SQLiteStorage, store._storage)._read_turn.locked()
            assert _scope_ids(store) == []


class TestSQLiteDamage:
    """A damaged SQLite file, a full disk, or a failing connection."""

    def test_unreadable_files_raise_store_error(self, tmp_path, db_path):
        not_a_store = tmp_path / "junk.db"
        not_a_store.write_bytes(b"not a database" * 100)
        for path in (not_a_store, tmp_path / "missing" / db_path.name):
            with pytest.raises(StoreError):
                SQLiteStore(path)

    @pytest.mark.parametrize(
        "column, damage",
        [
            ("state", "bogus"),
            ("containers", "not json"),
            ("containers", "5"),
            ("containers", '"abc"'),
            ("synced_at", "noon"),
            ("edit_kind", "bogus"),
            ("conflict_kind", "bogus"),
            ("content", "text, not bytes"),
        ],
    )
    def test_damaged_values_raise_store_error(self, db_path, clock, column, damage):
        with SQLiteStore(db_path) as store:
            _seeded(Replica(store, clock=clock))
        _edit_file(db_path, f"UPDATE {_RECORDS} SET {column} = ?", (damage,))
        with SQLiteStore(db_path) as store, pytest.raises(StoreError, match=column):
            Replica(store, clock=clock).records(_SCOPE)

    def test_a_lost_meta_row_raises_store_error(self, db_path, clock):
        with SQLiteStore(db_path) as store:
            replica = Replica(store, clock=clock)
            _edit_file(db_path, f"DELETE FROM {_META}", ())
            with pytest.raises(StoreError, match="lost its row"):
                replica.feed_head()
            with pytest.raises(StoreError, match="lost its row"):
                _seeded(replica)
        with pytest.raises(StoreError, match="lost its row"):
            SQLiteStore(db_path)

    def test_other_sqlite_errors_become_store_error(self, db_path):
        with SQLiteStore(db_path) as store:
            _edit_file(db_path, f"DROP TABLE {_FEED}", ())
            with store.transaction(write=False), pytest.raises(StoreError, match=_FEED):
                store.entries(0, 1)

    def test_a_full_disk_raises_store_error_and_keeps_nothing(self, db_path, clock):
        with SQLiteStore(db_path) as store:
            replica = _seeded(Replica(store, clock=clock))
            before = _snapshot(replica, store)
            # SQLite reports a file at its page limit as a full disk.
            writer = _writer(store)
            pages = writer.execute("PRAGMA page_count").fetchone()[0]
            writer.execute(f"PRAGMA max_page_count = {pages}")
            many = {f"o{n}": _changed(f"f{n}", content=b"x" * 10_000) for n in range(20)}
            with pytest.raises(StoreError, match="full"):
                _sync(replica, _page("T1", "T2", many))
            assert _snapshot(replica, store) == before

    def test_a_transaction_sqlite_already_ended_rolls_back_quietly(self, db_path):
        with SQLiteStore(db_path) as store, pytest.raises(OSError), store.transaction():
            cast(_SQLiteTransaction, _current(store))._conn.execute("COMMIT")
            raise OSError("after an implicit end")

    def test_a_failed_close_raises_store_error_and_still_closes(self, db_path, monkeypatch):
        store = SQLiteStore(db_path)
        close = store._storage.close

        def failing_close() -> None:
            close()
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store._storage, "close", failing_close)
        with pytest.raises(StoreError, match="disk I/O"):
            store.close()
        store.close()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction())
