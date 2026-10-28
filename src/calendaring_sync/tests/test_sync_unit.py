# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for the calendaring_sync package.

Rule: zero network calls, zero Docker dependency, all tests are fast.
"""

from __future__ import annotations

import dataclasses
import doctest
import gc
import importlib
import inspect
import pickle
import pkgutil
import re
import runpy
import sqlite3
import sys
import weakref
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import patch

import pytest

import calendaring_sync
from calendaring_sync import (
    Applied,
    Change,
    Changed,
    ChangeSet,
    Conflict,
    ConflictKind,
    Delete,
    DuplicateObjectError,
    EditFields,
    EditKind,
    FeedEntry,
    FeedEntryKind,
    FeedRangeError,
    Gone,
    KeepLocal,
    KeepServer,
    ListingRequiredError,
    LocalEdit,
    LogicalKeyTaken,
    LoneSurrogateError,
    MemoryData,
    MemoryKeeper,
    MemoryStore,
    Merge,
    ObjectIdAssignedError,
    OpenConflictError,
    OtherScopeError,
    PendingEditError,
    PushInFlightError,
    PushOutcome,
    Record,
    RecordState,
    RecordValueError,
    Rejected,
    Removed,
    RemovedRecordError,
    Replica,
    ResolvedConflicts,
    Scope,
    ScopeGoneError,
    ScopeMismatchError,
    Session,
    SessionError,
    SessionState,
    SQLiteStore,
    StaleChangeSetError,
    StaleConflictError,
    StateStore,
    StorageTransaction,
    StoreBusyError,
    StoreError,
    StoreVersionError,
    TransactionError,
    TransactionPermissionError,
    TransactionRequiredError,
    TrimmedFeedError,
    Unchanged,
    UnexpectedStoreError,
    Unknown,
    UnknownHolderError,
    UnknownRecordError,
    UnknownScopeError,
    UnknownVersionError,
    UpdateHolder,
)
from calendaring_sync._checks import _CHECKED_WHEN_BUILT, _fields, argument_check
from calendaring_sync.adapters.base import Adapter, AsyncAdapter, Steps, SyncAdapter
from calendaring_sync.adapters.caldav import AsyncCalDAVAdapter, CalDAVAdapter, _CalDAVFlow
from calendaring_sync.adapters.jmap import AsyncJMAPAdapter, JMAPAdapter, _JMAPFlow
from calendaring_sync.constants import FEED_PAGE_SIZE, ISSUES_URL, SCHEMA_VERSION
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
from calendaring_sync.tests import _caldav_scenarios, _examples, _jmap_scenarios, benchmark
from calendaring_sync.tests._scenarios import done, run
from calendaring_sync.tests._storage import OneAtATime
from calendaring_sync.tests._tape import Tape

_SCOPE = "cal"
_OTHER = "other"
_SCHEME = "scheme-1"
_OTHER_SCHEME = "scheme-2"
_MEDIA = "text/plain"
_START = datetime(2026, 10, 1, tzinfo=UTC)
# How long a test store waits for another, where the wait is meant to run out.
_SHORT_TIMEOUT = 0.1
# How long a test waits for another thread before it counts as stuck.
_WAIT = 5.0
# The scope and event the docs' examples start from, in their start.py.
_EXAMPLE_SCOPE = "work"
_EXAMPLE_EVENT = "meeting"
# The tests that run the docs' examples, which an installed wheel doesn't hold.
_needs_examples = pytest.mark.skipif(
    not _examples.EXAMPLES.is_dir(), reason="The docs examples aren't installed"
)


class _Clock:
    """A clock that moves one second each time it's read."""

    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def replica(store: StateStore, clock: _Clock) -> Replica:
    return Replica(store, clock=clock)


def _changed(
    fingerprint: str,
    version: str | None = "v1",
    *,
    content: bytes | None = None,
    containers: frozenset[str] = frozenset(),
    logical_key: str | None = None,
) -> Changed:
    if content is None:
        content = _content(fingerprint)
    return Changed(version, fingerprint, content, containers, logical_key)


def _content(fingerprint: str) -> bytes:
    """The content a test gives each fingerprint, so equal fingerprints mean equal content."""
    return f"text of {fingerprint}".encode()


def _page(
    since: str | None,
    token: str,
    changes: Mapping[str, Change],
    *,
    full: bool = False,
    truncated: bool = False,
    scope: str = _SCOPE,
) -> ChangeSet:
    return ChangeSet(scope, since, token, changes, full_listing=full, truncated=truncated)


def _open_session(
    replica: Replica,
    scope: str = _SCOPE,
    *,
    scheme: str = _SCHEME,
    media: str = _MEDIA,
    guarded_by_version: bool = False,
    unique_logical_key: bool = False,
) -> AbstractContextManager[Session]:
    return replica.session(
        scope,
        fingerprint_scheme=scheme,
        media_type=media,
        guarded_by_version=guarded_by_version,
        unique_logical_key=unique_logical_key,
    )


def _enter(context: AbstractContextManager[object]) -> None:
    """Enter and leave a context, for tests about what happens on entry."""
    with ExitStack() as stack:
        stack.enter_context(context)


def _sync(replica: Replica, *pages: ChangeSet) -> SessionState:
    """Ingest change sets in one session, opened for their scope; return where it ended."""
    with _open_session(replica, pages[0].scope) as session:
        for change_set in pages:
            session.ingest(change_set)
        return session.state


def _states(replica: Replica) -> dict[str | None, RecordState]:
    return {r.object_id: r.state for r in replica.records(_SCOPE)}


def _feed(replica: Replica, after: int = 0) -> list[tuple[FeedEntryKind, str | None]]:
    return [(e.kind, e.object_id) for e in replica.read_feed(after)]


def _snapshot(replica: Replica, store: StateStore) -> tuple[object, ...]:
    """Everything a replica holds, to compare before and after; the feed from where it's trimmed."""
    with store.transaction(write=False):
        trimmed = store.feed_bounds()[0]
    feed = replica.read_feed(trimmed)
    return (replica.scopes(), replica.records(_SCOPE), feed, replica.feed_head())


def _timeless(value: object) -> object:
    """``value`` with every time left out, to compare two runs that can't share a clock."""
    if isinstance(value, datetime):
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(_timeless(getattr(value, f.name)) for f in dataclasses.fields(value))
    if isinstance(value, (list, tuple)):
        return [_timeless(item) for item in value]
    return value


def _new_scope() -> Scope:
    return Scope(_SCOPE, None, _SCHEME, _MEDIA)


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


# A local edit and a conflict for records written straight to a store.
_EDIT = LocalEdit(EditKind.CHANGE, b"c2", "f2", frozenset(), None, _START)
_CONFLICT = Conflict(ConflictKind.BOTH_CHANGED, "v2", "f3", b"c3", frozenset(), None, _START)


def _local_record(state: RecordState, object_id: str | None = None, local_key: int = 0) -> Record:
    """A record written straight to a store, holding what its state needs."""
    edit = None if state in (RecordState.SYNCED, RecordState.REMOVED) else _EDIT
    conflict = _CONFLICT if state is RecordState.CONFLICT else None
    return Record(
        local_key,
        _SCOPE,
        object_id,
        state,
        None,
        "f",
        b"c",
        frozenset(),
        None,
        None,
        0,
        edit,
        conflict,
    )


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


def _seeded(replica: Replica) -> Replica:
    """A scope at token T1 holding a and b."""
    _sync(replica, _page(None, "T1", {"a": _changed("fa"), "b": _changed("fb")}, full=True))
    return replica


def _key(replica: Replica, object_id: str = "a") -> int:
    record = replica.find_record(_SCOPE, object_id)
    assert record is not None
    return record.local_key


def _change(
    replica: Replica,
    local_key: int,
    fingerprint: str,
    *,
    containers: frozenset[str] = frozenset(),
    logical_key: str | None = None,
) -> None:
    replica.change(
        local_key,
        _content(fingerprint),
        fingerprint=fingerprint,
        containers=containers,
        logical_key=logical_key,
    )


def _create(
    replica: Replica,
    fingerprint: str,
    *,
    scope: str = _SCOPE,
    containers: frozenset[str] = frozenset(),
    logical_key: str | None = None,
    object_id: str | None = None,
) -> int:
    """Record a create holding the content a test gives ``fingerprint``; return its local key."""
    return replica.create(
        scope,
        _content(fingerprint),
        fingerprint=fingerprint,
        containers=containers,
        logical_key=logical_key,
        object_id=object_id,
    )


def _step(replica: Replica, local_key: int, step: str) -> None:
    """Record one edit written as ``change <fingerprint>``, ``delete`` or ``discard``."""
    action, *fingerprint = step.split()
    if action == "change":
        _change(replica, local_key, fingerprint[0])
    else:
        getattr(replica, action)(local_key)


def _edit_locally(
    replica: Replica, local: str | None, object_id: str = "a", *, logical_key: str | None = None
) -> tuple[int, str]:
    """Give a seeded scope one local edit; return its record's key and object id.

    A create gets the id ``n`` and ``logical_key``; every other edit goes to
    ``object_id``, a seeded object, and a change makes it ``fl``.
    """
    if local == "create":
        created = _create(replica, "fl", logical_key=logical_key, object_id="n")
        return created, "n"
    key = _key(replica, object_id)
    if local is not None:
        _step(replica, key, "change fl" if local == "change" else local)
    return key, object_id


def _remote(remote: str, version: str = "v2") -> Change:
    """The server's report written as ``removed``, or as a fingerprint at ``version``."""
    return Removed() if remote == "removed" else _changed(remote, version)


def _summary(record: Record | None) -> tuple[object, ...] | None:
    """A record's state, edit kind and conflict kind."""
    if record is None:
        return None
    return (
        record.state,
        record.edit and record.edit.kind,
        record.conflict and record.conflict.kind,
    )


def _assert_ended(
    replica: Replica,
    key: int,
    head: int,
    expected: tuple[object, ...] | None,
    feed: list[FeedEntryKind],
) -> Record | None:
    """Check the summary a record ended with and the feed kinds since ``head``, and return it."""
    record = replica.record(key)
    assert _summary(record) == expected
    assert [kind for kind, _ in _feed(replica, head)] == feed
    return record


def _edited(replica: Replica) -> Replica:
    """A seeded scope at token T2 where a holds a pending change and b an open conflict."""
    _seeded(replica)
    _edit_locally(replica, "change")
    _in_conflict(replica, "b")
    return replica


def _a_removed(replica: Replica) -> Replica:
    """A seeded scope at token T2, where a was removed."""
    _sync(_seeded(replica), _page("T1", "T2", {"a": Removed()}))
    return replica


@contextmanager
def _pushing(
    replica: Replica, since: str = "T1", token: str = "T2", *, guarded_by_version: bool = False
) -> Iterator[Session]:
    """A session that ingested a change set with nothing in it, so it may push."""
    with _open_session(replica, guarded_by_version=guarded_by_version) as session:
        session.ingest(_page(since, token, {}))
        yield session


def _claim(session: Session, local_key: int) -> Record:
    """Claim a record the test knows is pending."""
    claimed = session.claim(local_key)
    assert claimed is not None
    return claimed


def _push(
    replica: Replica,
    local_key: int,
    outcome: PushOutcome | None,
    since: str = "T1",
    token: str = "T2",
) -> None:
    """Push one record in a session of its own.

    ``None`` leaves the push in flight, as a process that stops before the
    server answers does.
    """
    with _pushing(replica, since, token) as session:
        claimed = _claim(session, local_key)
        if outcome is not None:
            assert session.settle(claimed, outcome)


def _refused_create(
    replica: Replica,
    holder: str | None,
    since: str = "T1",
    token: str = "T2",
    *,
    logical_key: str | None = "u",
    local: str = "create",
) -> int:
    """Push an edit that the server refuses for its logical key; return its local key.

    A create gets the id n; a change goes to a, as ``_edit_locally`` makes them.
    """
    key, _ = _edit_locally(replica, local, logical_key=logical_key)
    _push(replica, key, LogicalKeyTaken(holder), since, token)
    return key


def _refused_edit(replica: Replica, refused: EditKind) -> int:
    """Have the server refuse an edit for b's logical key u, at T2 to T3; return its key.

    A create gets the id n, and a change goes to a.
    """
    if refused is EditKind.CREATE:
        return _refused_create(replica, "b", "T2", "T3")
    key = _key(replica)
    _change(replica, key, "fl", logical_key="u")
    _push(replica, key, LogicalKeyTaken("b"), "T2", "T3")
    return key


# The local edit and the server's report that open each kind of conflict an
# ingest stores.
_OPENED_BY = {
    ConflictKind.BOTH_CHANGED: ("change", "fx"),
    ConflictKind.REMOVED_REMOTELY: ("change", "removed"),
    ConflictKind.DELETED_LOCALLY: ("delete", "fx"),
    ConflictKind.BOTH_CREATED: ("create", "fx"),
}


def _in_conflict(
    replica: Replica,
    object_id: str = "a",
    since: str = "T1",
    token: str = "T2",
    *,
    kind: ConflictKind = ConflictKind.BOTH_CHANGED,
    refused: str = "create",
) -> int:
    """Open a conflict of ``kind`` in a seeded scope; return its local key.

    A conflict about a create is at the id n, and a refused edit, the
    ``refused`` kind of edit, names b as the object holding its logical key u.
    """
    if kind is ConflictKind.LOGICAL_KEY_TAKEN:
        return _refused_create(replica, "b", since, token, local=refused)
    local, remote = _OPENED_BY[kind]
    key, object_id = _edit_locally(replica, local, object_id)
    _sync(replica, _page(since, token, {object_id: _remote(remote)}))
    return key


def _two_conflicts(replica: Replica) -> tuple[int, int]:
    """Seed a scope that ends at T3, where a and b both changed on both sides; return their keys."""
    return _in_conflict(_seeded(replica)), _in_conflict(replica, "b", "T2", "T3")


def _seeded_edit(replica: Replica, local: str | None) -> tuple[int, str]:
    """Seed a scope and give it one local edit, as ``_edit_locally`` does."""
    return _edit_locally(_seeded(replica), local)


def _assert_adapter_refuses(
    adapter: Any,
    store: StateStore,
    call: Callable[[Any], Any],
    error: type[Exception],
    match: str | None,
    before: bool,
) -> None:
    """:func:`_assert_refused` for one call of an adapter, run to its end if it's a coroutine."""
    _assert_refused(
        adapter._replica,
        store,
        lambda: done(call(adapter)),
        error,
        match,
        before_a_transaction=before,
    )


def _assert_refused(
    replica: Replica,
    store: StateStore,
    call: Callable[[], object],
    error: type[Exception],
    match: str | None = None,
    *,
    before_a_transaction: bool = False,
) -> None:
    """Check a call raises exactly ``error``, leaves no transaction open and changes nothing.

    With ``before_a_transaction``, also check it raised before opening one.
    """
    before = _snapshot(replica, store)
    with (
        patch.object(store._storage, "begin", wraps=store._storage.begin) as begin,
        pytest.raises(error, match=match) as raised,
    ):
        call()
    assert type(raised.value) is error
    assert not (before_a_transaction and begin.called)
    # A transaction left open in this thread would make this one raise.
    with store.transaction(write=False):
        pass
    assert _snapshot(replica, store) == before


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


# Examples that continue from the replica the Replica class example builds.
_CONTINUING = ("calendaring_sync.replica.Replica.", "calendaring_sync.session.Session")


def _docstring_examples() -> list[doctest.DocTest]:
    """Every docstring example in the package; a replica method's start from the class's."""
    finder = doctest.DocTestFinder()
    tests = [
        test
        for module in pkgutil.walk_packages(calendaring_sync.__path__, "calendaring_sync.")
        if ".tests" not in module.name
        for test in finder.find(importlib.import_module(module.name))
        if test.examples
    ]
    start = next(t for t in tests if t.name == "calendaring_sync.replica.Replica").examples
    for test in tests:
        if test.name.startswith(_CONTINUING):
            test.examples = start + test.examples
    return tests


def _sync_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception, failing: frozenset[int] = frozenset({1})
) -> list[int]:
    """Make every adapter's sync raise ``error`` on the calls numbered in ``failing``.

    Returns the list of calls, which grows by one with each.
    """
    sync = SyncAdapter.sync
    calls: list[int] = []

    def fails(adapter: SyncAdapter, **kwargs: bool) -> None:
        calls.append(1)
        if len(calls) in failing:
            raise error
        sync(adapter, **kwargs)

    monkeypatch.setattr(SyncAdapter, "sync", fails)
    return calls


def _memory_adapter(asynchronous: bool) -> tuple[Any, Replica, Any]:
    """The example adapter, synced with a server holding a; return the server, replica, adapter."""
    memory = _examples.load("memory_adapter")
    server = memory.MemoryServer()
    server.put("a", b"theirs", if_version=None)
    replica = Replica(MemoryStore())
    adapter = (
        memory.AsyncMemoryAdapter(replica, memory.AsyncMemoryServer(server), _SCOPE)
        if asynchronous
        else memory.MemoryAdapter(replica, server, _SCOPE)
    )
    done(adapter.sync())
    return server, replica, adapter


@_needs_examples
class TestExamples:
    """Each example a docs page shows does what the page says."""

    def test_every_example_is_run_and_shown(self):
        tested = {name.removeprefix("test_") for name in vars(TestExamples)}
        assert set(_examples.names()) - _examples.LIVE <= tested
        pages = "".join(
            p.read_text(encoding="utf-8") for p in _examples.EXAMPLES.parent.rglob("*.rst")
        )
        shown = {name for name in _examples.names() if f"examples/{name}.py" in pages}
        assert set(_examples.names()) - _examples.SHARED <= shown

    def test_memory_adapter(self):
        def synced(asynchronous: bool) -> object:
            server, replica, adapter = _memory_adapter(asynchronous)
            created = done(adapter.create(b"new"))
            done(adapter.change(_key(replica), b"ours"))
            done(adapter.sync())
            replica.delete(created)
            # The token expires, so the sync that sends the delete lists the scope again.
            server.oldest = int(server.token()) + 1
            done(adapter.sync())
            records = [(r.local_key, r.state, r.content) for r in replica.records(_SCOPE)]
            return records, sorted(content for _, content in server.objects.values())

        assert synced(asynchronous=True) == synced(asynchronous=False)
        assert synced(asynchronous=False) == (
            [(1, RecordState.SYNCED, b"ours"), (2, RecordState.REMOVED, b"new")],
            [b"ours"],
        )

    @pytest.mark.parametrize("asynchronous", [False, True])
    def test_memory_adapter_refusals(self, asynchronous):
        server, replica, adapter = _memory_adapter(asynchronous)
        key = _key(replica)
        # Written past the server's log, so no sync reports it and only the push meets it.
        server.objects["a"] = ("99", b"theirs, again")
        done(adapter.change(key, b"ours"))
        done(adapter.sync())
        assert _summary(replica.record(key)) == _pending(EditKind.CHANGE)
        del server.objects["a"]
        done(adapter.sync())
        assert _summary(replica.record(key)) == _conflicted(
            EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY
        )
        server.removed = True
        with pytest.raises(ScopeGoneError):
            done(adapter.sync(full_listing=True))

    def test_run_replaces_a_setting(self, tmp_path, monkeypatch):
        (tmp_path / "setting.py").write_text("answer = 1\ndoubled = answer * 2\n")
        monkeypatch.setattr(_examples, "EXAMPLES", tmp_path)
        monkeypatch.setattr(_examples, "load", lambda name: None)
        assert _examples.run("setting", monkeypatch, tmp_path, answer=21)["doubled"] == 42
        with pytest.raises(AssertionError, match="^setting has no missing to replace$"):
            _examples.run("setting", monkeypatch, tmp_path, missing=1)

    def test_readme_shows_the_quickstart(self):
        quickstart = (_examples.EXAMPLES / "quickstart.py").read_text(encoding="utf-8")
        shown = quickstart.split("# start: sync\n")[1].split("# end: sync\n")[0]
        readme = (_examples.EXAMPLES.parents[1] / "README.md").read_text(encoding="utf-8")
        assert f"```python\n{shown}```\n" in readme

    def test_start(self, tmp_path, monkeypatch):
        ran = _examples.run("start", monkeypatch, tmp_path)
        assert [r.content for r in ran["replica"].records(_EXAMPLE_SCOPE)] == [b"Team meeting"]

    @pytest.mark.parametrize(
        "found, content", [(True, b"Team meeting, room 3"), (False, b"Team meeting")]
    )
    def test_local_edits(self, tmp_path, monkeypatch, found, content):
        if not found:
            monkeypatch.setattr(Replica, "find_record", lambda *args: None)
        ran = _examples.run("local_edits", monkeypatch, tmp_path)
        replica = ran["replica"]
        meeting = replica.record(_examples.load("start").meeting.local_key)
        assert (meeting.state, meeting.content) == (RecordState.SYNCED, content)
        assert replica.record(ran["lunch"]).state is RecordState.REMOVED
        assert list(_examples.load("start").server.objects) == [_EXAMPLE_EVENT]

    def test_change_feed(self, tmp_path, monkeypatch):
        ran = _examples.run("change_feed", monkeypatch, tmp_path)
        assert ran["handled"] == [(FeedEntryKind.CHANGED, _EXAMPLE_EVENT)]
        assert (tmp_path / "feed-cursor.txt").read_text() == "1"
        replica = ran["replica"]
        # The meeting the server deleted is purged, and the feed trimmed.
        assert replica.records(_EXAMPLE_SCOPE) == []
        with pytest.raises(TrimmedFeedError):
            replica.read_feed(0)

    def test_change_feed_after_a_trim(self, tmp_path, monkeypatch):
        def trimmed(*args: object, **kwargs: object) -> list[FeedEntry]:
            raise TrimmedFeedError("trimmed")

        monkeypatch.setattr(Replica, "read_feed", trimmed)
        ran = _examples.run("change_feed", monkeypatch, tmp_path)
        assert ran["handled"] == []
        assert (tmp_path / "feed-cursor.txt").read_text() == "1"

    def test_conflicts(self, tmp_path, monkeypatch):
        ran = _examples.run("conflicts", monkeypatch, tmp_path)
        merged = b"Team sync, room 3 / Team sync, at 10"
        meeting = ran["replica"].find_record(_EXAMPLE_SCOPE, _EXAMPLE_EVENT)
        assert (meeting.state, meeting.content) == (RecordState.SYNCED, merged)
        assert _examples.load("start").server.objects[_EXAMPLE_EVENT][1] == merged

    @pytest.mark.parametrize(
        "kind, resolution",
        [(ConflictKind.REMOVED_REMOTELY, KeepServer()), (ConflictKind.DELETED_LOCALLY, None)],
    )
    def test_conflicts_resolver(self, tmp_path, monkeypatch, kind, resolution):
        ran = _examples.run("conflicts", monkeypatch, tmp_path)
        record = SimpleNamespace(conflict=SimpleNamespace(kind=kind))
        assert ran["resolver"](record) == resolution

    @pytest.mark.parametrize(
        "busy, unsent, scopes",
        [(False, [b"Team meeting, room 3"], []), (True, [], [_EXAMPLE_SCOPE])],
    )
    def test_errors(self, tmp_path, monkeypatch, busy, unsent, scopes):
        if busy:
            # The first sync is the starting state's, the second the example's.
            _sync_fails(monkeypatch, StoreBusyError("busy"), frozenset({2}))
        ran = _examples.run("errors", monkeypatch, tmp_path)
        assert ran["unsent"] == unsent
        assert [scope.id for scope in ran["replica"].scopes()] == scopes

    def test_mirror(self, tmp_path, monkeypatch):
        ran = _examples.run("mirror", monkeypatch, tmp_path)
        assert ran["moved"] == [(b"Team meeting, moved",)]
        assert ran["after_delete"] == []

    def test_mirror_after_a_drop(self, tmp_path, monkeypatch):
        ran = _examples.run("mirror", monkeypatch, tmp_path)
        replica = ran["replica"]
        db = sqlite3.connect(tmp_path / "app.db")
        _examples.load("start").server.put("lunch", b"Lunch", if_version=None)
        ran["adapter"].sync()
        ran["mirror"](replica, db)
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)
        replica.drop_scope(_EXAMPLE_SCOPE)
        ran["mirror"](replica, db)
        assert db.execute("SELECT count(*) FROM events").fetchone() == (0,)
        db.close()

    def test_parallel(self, tmp_path, monkeypatch):
        ran = _examples.run("parallel", monkeypatch, tmp_path)
        replica = ran["replica"]
        assert [scope.id for scope in replica.scopes()] == ["home", "team", "work"]

    @pytest.mark.parametrize("failing", [frozenset({1}), frozenset({1, 2})])
    def test_parallel_retry(self, tmp_path, monkeypatch, failing):
        ran = _examples.run("parallel", monkeypatch, tmp_path)
        calls = _sync_fails(monkeypatch, StaleChangeSetError("stale"), failing)
        ran["sync"](ran["adapters"][0])
        assert len(calls) == len(failing) + 1

    def test_inspect_scope(self, tmp_path, monkeypatch):
        ran = _examples.run("inspect_scope", monkeypatch, tmp_path)
        assert [r.edit.content for r in ran["unsent"]] == [b"Team meeting, room 3"]
        assert (ran["conflicts"], ran["blocked"]) == ([], [])
        assert ran["token"] is not None
        # The repairs sent the edit and left the scope listed again.
        meeting = ran["replica"].find_record(_EXAMPLE_SCOPE, _EXAMPLE_EVENT)
        assert (meeting.state, meeting.content) == (RecordState.SYNCED, b"Team meeting, room 3")

    def test_testing(self, tmp_path, monkeypatch):
        ran = _examples.run("testing", monkeypatch, tmp_path)
        assert ran["replica"].scope(_EXAMPLE_SCOPE).token == "t3"

    def test_snapshots(self, tmp_path, monkeypatch):
        ran = _examples.run("snapshots", monkeypatch, tmp_path)
        feed = [(entry.kind, entry.object_id) for entry in ran["replica"].read_feed()]
        # The second, unchanged snapshot adds nothing.
        assert feed == [
            (FeedEntryKind.CHANGED, _EXAMPLE_EVENT),
            (FeedEntryKind.CHANGED, "lunch"),
            (FeedEntryKind.CHANGED, _EXAMPLE_EVENT),
            (FeedEntryKind.REMOVED, "lunch"),
        ]

    # Busy once, the sync goes through on the retry; busy every time, it gives up after three.
    @pytest.mark.parametrize(
        "failing, calls, scopes",
        [(frozenset({1}), 2, [_EXAMPLE_SCOPE]), (frozenset({1, 2, 3}), 3, [])],
    )
    def test_sqlite(self, tmp_path, monkeypatch, failing, calls, scopes):
        made = _sync_fails(monkeypatch, StoreBusyError("busy"), failing)
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        _examples.run("sqlite", monkeypatch, tmp_path)
        assert len(made) == calls
        with closing(sqlite3.connect(tmp_path / "sync.db")) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        with SQLiteStore(tmp_path / "sync.db") as store:
            assert [scope.id for scope in Replica(store).scopes()] == scopes

    def test_keeper(self, tmp_path, monkeypatch):
        ran = _examples.run("keeper", monkeypatch, tmp_path)
        assert ran["meeting"].content == b"Team meeting"
        keeper = ran["FileKeeper"](tmp_path / "sync.pickle")
        assert isinstance(keeper.error(OSError("disk full")), StoreError)
        assert keeper.error(ValueError("a bug")) is None


class TestPackage:
    @pytest.mark.parametrize("test", _docstring_examples(), ids=lambda test: test.name)
    def test_docstring_examples(self, test):
        assert doctest.DocTestRunner().run(test).failed == 0

    def test_benchmarks_run_and_measure_what_they_say(self, monkeypatch, capsys):
        size = 200
        # Run the module as the contributing guide says to, at a small size.
        monkeypatch.setattr(sys, "argv", ["benchmark", "--size", str(size), "--runs", "1"])
        monkeypatch.delitem(sys.modules, benchmark.__name__)
        runpy.run_module("calendaring_sync.tests.benchmark", run_name="__main__")
        printed = capsys.readouterr().out.splitlines()
        assert [line.split(",")[0] for line in printed] == list(benchmark.SCENARIOS)
        edited = size // benchmark.EDITED_SHARE
        conflicted = edited // benchmark.CONFLICTED_SHARE
        assert benchmark.states_over_edits(size) == {
            RecordState.SYNCED: size - edited,
            RecordState.PENDING: edited - conflicted,
            RecordState.CONFLICT: conflicted,
        }
        assert benchmark.entries_after_pushes(size) == {FeedEntryKind.PUSHED: edited}

    @pytest.mark.parametrize(
        "adapter, dependency, extra",
        [
            ("calendaring_sync.adapters.caldav", "caldav.calendarobjectresource", "caldav"),
            ("calendaring_sync.adapters.jmap", "calendaring_jmap", "jmap"),
        ],
    )
    def test_an_adapter_without_its_extra_names_it_on_import(self, adapter, dependency, extra):
        # A module set to None in sys.modules can't be imported, as if it
        # weren't installed; the adapter is imported again under that.
        importlib.import_module(adapter)
        with patch.dict(sys.modules, {dependency: None, adapter: None}):
            del sys.modules[adapter]
            with pytest.raises(ImportError, match=rf"pip install 'calendaring-sync\[{extra}\]'"):
                importlib.import_module(adapter)

    @pytest.mark.parametrize("protocol, server", [("caldav", "cyrus"), ("jmap", "stalwart")])
    def test_a_recorder_records_every_recording_of_a_server(self, monkeypatch, protocol, server):
        # Run the module as its docstring says to.
        recorder = f"calendaring_sync.tests.record_{protocol}"
        scenarios = importlib.import_module(f"calendaring_sync.tests._{protocol}_scenarios")
        importlib.import_module(recorder)
        monkeypatch.setattr(sys, "argv", [recorder, server])
        monkeypatch.delitem(sys.modules, recorder)
        with patch("calendaring_sync.tests._scenarios.run") as recorded, patch.object(Tape, "save"):
            runpy.run_module(recorder, run_name="__main__")
        assert [c.args[2] for c in recorded.call_args_list] == [
            s for name, s in scenarios.RECORDINGS if name == server
        ]

    def test_every_entry_point_checks_its_types(self):
        for cls in (
            Replica,
            Session,
            StateStore,
            MemoryStore,
            SQLiteStore,
            Adapter,
            SyncAdapter,
            AsyncAdapter,
            CalDAVAdapter,
            AsyncCalDAVAdapter,
            JMAPAdapter,
            AsyncJMAPAdapter,
        ):
            for name, func in vars(cls).items():
                if name.startswith("_") and name != "__init__":
                    continue
                if isinstance(func, property):
                    # A property that only reads takes no argument to check.
                    assert func.fset is None, f"{cls.__name__}.{name} has a setter"
                    continue
                assert inspect.isfunction(func), f"{cls.__name__}.{name} isn't checked"
                assert getattr(func, "_checks_arguments", False), f"{cls.__name__}.{name}"
                # Builds the check; an annotation it can't read raises here.
                argument_check(inspect.unwrap(func))
        exported = [getattr(calendaring_sync, name) for name in calendaring_sync.__all__]
        for cls in exported:
            if isinstance(cls, type) and dataclasses.is_dataclass(cls):
                _fields(cls)
        # The values an adapter or application builds check themselves.
        assert _CHECKED_WHEN_BUILT == {
            Changed,
            ChangeSet,
            Applied,
            LogicalKeyTaken,
            Merge,
            EditFields,
        }

    def test_a_check_refuses_what_it_cant_read(self):
        def takes_many(*values: str) -> None: ...

        def takes_a_literal(kind: Literal["a"]) -> None: ...

        def takes_anything(value) -> None: ...

        for func, match in [
            (takes_many, r"takes \*values, which isn't checked"),
            (takes_a_literal, "^Can't check a value against"),
            (takes_anything, "takes value without a type to check$"),
        ]:
            with pytest.raises(TypeError, match=match):
                argument_check(func)

    def test_version_is_set(self):
        assert isinstance(calendaring_sync.__version__, str)
        assert calendaring_sync.__version__

    def test_exports_resolve(self):
        for name in calendaring_sync.__all__:
            assert getattr(calendaring_sync, name) is not None


class TestIngest:
    def test_first_listing_creates_records_and_sets_token(self, replica, clock):
        state = _sync(replica, _page(None, "T1", {"a": _changed("fa")}, full=True))
        scope = replica.scope(_SCOPE)
        assert state is SessionState.INGESTED
        assert scope.token == "T1"
        assert scope.last_sync_at == scope.last_full_listing_at == clock.now
        [record] = replica.records(_SCOPE)
        assert (record.object_id, record.state, record.version, record.content) == (
            "a",
            RecordState.SYNCED,
            "v1",
            b"text of fa",
        )
        [entry] = replica.read_feed()
        assert (entry.kind, entry.local_key, entry.at) == (
            FeedEntryKind.CHANGED,
            record.local_key,
            record.synced_at,
        )
        assert record.synced_at == clock.now

    def test_incremental_change_and_new_object(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "T2", {"a": _changed("fa2", "v2"), "c": _changed("fc")}))
        records = {r.object_id: r for r in replica.records(_SCOPE)}
        assert records["a"].fingerprint == "fa2" and records["a"].version == "v2"
        assert records["c"].state is RecordState.SYNCED
        assert _feed(replica, 2) == [(FeedEntryKind.CHANGED, "a"), (FeedEntryKind.CHANGED, "c")]
        assert replica.scope(_SCOPE).token == "T2"

    def test_equal_fingerprint_updates_version_and_text_silently(self, replica, clock):
        _seeded(replica)
        before = replica.records(_SCOPE)[0]
        _sync(
            replica,
            _page("T1", "T2", {"a": _changed("fa", "v2", content=b"rewritten by the server")}),
        )
        after = replica.record(before.local_key)
        assert after is not None
        assert (after.version, after.content, after.last_entry) == (
            "v2",
            b"rewritten by the server",
            before.last_entry,
        )
        assert after.synced_at == clock.now
        assert _feed(replica, 2) == []

    @pytest.mark.parametrize(
        "moved",
        [
            {"containers": frozenset({"cal2"}), "logical_key": "uid-1"},
            {"containers": frozenset({"cal1"}), "logical_key": "uid-2"},
        ],
    )
    def test_new_containers_or_logical_key_count_as_a_change(self, replica, moved):
        first = _changed("fa", containers=frozenset({"cal1"}), logical_key="uid-1")
        _sync(replica, _page(None, "T1", {"a": first}, full=True))
        _sync(replica, _page("T1", "T2", {"a": _changed("fa", "v2", **moved)}))
        [record] = replica.records(_SCOPE)
        assert (record.containers, record.logical_key) == (
            moved["containers"],
            moved["logical_key"],
        )
        assert _feed(replica, 1) == [(FeedEntryKind.CHANGED, "a")]

    def test_version_may_be_none(self, replica):
        _sync(replica, _page(None, "T1", {"a": _changed("fa", None)}, full=True))
        assert replica.records(_SCOPE)[0].version is None
        assert replica.known_versions(_SCOPE) == {}

    def test_removal(self, replica):
        _a_removed(replica)
        assert _states(replica) == {"a": RecordState.REMOVED, "b": RecordState.SYNCED}
        assert _feed(replica, 2) == [(FeedEntryKind.REMOVED, "a")]

    def test_removing_unknown_or_removed_object_does_nothing(self, replica):
        _a_removed(replica)
        _sync(replica, _page("T2", "T3", {"a": Removed(), "zz": Removed()}))
        assert _feed(replica, 3) == []
        assert "zz" not in _states(replica)

    def test_unknown_local_key_reads_as_none(self, replica):
        _seeded(replica)
        assert replica.record(999) is None

    def test_removed_record_comes_back_as_the_same_record(self, replica):
        _a_removed(replica)
        key = replica.records(_SCOPE)[0].local_key
        _sync(replica, _page("T2", "T3", {"a": _changed("fa")}))
        record = replica.record(key)
        assert record is not None and record.state is RecordState.SYNCED
        assert _feed(replica, 3) == [(FeedEntryKind.CHANGED, "a")]

    def test_stale_change_set_is_refused(self, replica, store):
        _seeded(replica)
        _sync(replica, _page("T1", "T2", {}))
        stale = _page("T1", "T3", {"a": _changed("x")})
        _assert_refused(replica, store, lambda: _sync(replica, stale), StaleChangeSetError)

    def test_second_handle_gets_the_stale_error(self, make_store, clock):
        first = _seeded(Replica(make_store(), clock=clock))
        store = make_store()
        second = Replica(store, clock=clock)
        _sync(first, _page("T1", "T2", {"a": _changed("fa2")}))
        stale = _page("T1", "T2b", {"b": _changed("fb2")})
        _assert_refused(second, store, lambda: _sync(second, stale), StaleChangeSetError)

    def test_incremental_without_token_is_refused(self, replica, store):
        with _open_session(replica) as session:
            page = _page(None, "T1", {"a": _changed("fa")})
            _assert_refused(
                replica,
                store,
                lambda: session.ingest(page),
                ListingRequiredError,
                "needs a full listing",
            )

    @pytest.mark.parametrize("object_id", ["zz", "a"])
    def test_unchanged_needs_a_live_record_with_a_version(self, replica, store, object_id):
        _a_removed(replica)
        page = _page("T2", "L", {object_id: Unchanged()}, full=True)
        _assert_refused(
            replica, store, lambda: _sync(replica, page), UnknownVersionError, "no version"
        )

    def test_change_set_for_another_scope_is_refused(self, replica, store):
        with _open_session(replica) as session:
            page = _page(None, "T1", {}, full=True, scope=_OTHER)
            _assert_refused(
                replica,
                store,
                lambda: session.ingest(page),
                OtherScopeError,
                "in a session for",
                before_a_transaction=True,
            )

    def test_truncated_incremental_pages_reach_the_final_token(self, replica):
        _seeded(replica)
        with _open_session(replica) as session:
            session.ingest(_page("T1", "C1", {"a": _changed("fa2")}, truncated=True))
            assert session.state is SessionState.INGESTING
            assert replica.scope(_SCOPE).token == "C1"
            session.ingest(_page("C1", "C2", {"b": _changed("fb2")}, truncated=True))
            session.ingest(_page("C2", "T2", {}))
            assert session.state is SessionState.INGESTED
        assert replica.scope(_SCOPE).token == "T2"

    def test_last_sync_at_moves_only_on_the_last_page(self, replica, clock):
        _seeded(replica)
        before = replica.scope(_SCOPE).last_sync_at
        with _open_session(replica) as session:
            session.ingest(_page("T1", "C1", {}, truncated=True))
            assert replica.scope(_SCOPE).last_sync_at == before
            session.ingest(_page("C1", "T2", {}))
        scope = replica.scope(_SCOPE)
        assert (scope.last_sync_at, scope.last_full_listing_at) == (clock.now, before)

    def test_failure_halfway_leaves_everything_as_it_was(self, replica, store, monkeypatch):
        _seeded(replica)
        before = _snapshot(replica, store)
        calls = 0
        original = store.update_record

        def fail_on_third(record: Record) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("disk gone")
            original(record)

        monkeypatch.setattr(store, "update_record", fail_on_third)
        with pytest.raises(OSError):
            _sync(
                replica,
                _page(
                    "T1", "T2", {"a": _changed("fa2"), "b": _changed("fb2"), "c": _changed("fc")}
                ),
            )
        monkeypatch.undo()
        assert _snapshot(replica, store) == before


class TestFullListing:
    def test_pages_set_the_token_only_on_the_last_page(self, replica):
        with _open_session(replica) as session:
            session.ingest(_page(None, "L1", {"a": _changed("fa")}, full=True, truncated=True))
            assert replica.scope(_SCOPE).token is None
            assert replica.scope(_SCOPE).last_full_listing_at is None
            session.ingest(_page(None, "L1", {"b": _changed("fb")}, full=True))
        assert replica.scope(_SCOPE).token == "L1"
        assert replica.scope(_SCOPE).listing_token is None

    def test_unmentioned_records_are_removed_on_the_last_page(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "T2", {"c": _changed("fc")}))
        with _open_session(replica) as session:
            session.ingest(_page("T2", "L", {"a": Unchanged()}, full=True, truncated=True))
            assert _states(replica)["b"] is RecordState.SYNCED
            session.ingest(_page("T2", "L", {"c": Unchanged()}, full=True))
        assert _states(replica) == {
            "a": RecordState.SYNCED,
            "b": RecordState.REMOVED,
            "c": RecordState.SYNCED,
        }
        assert replica.scope(_SCOPE).token == "L"

    def test_removal_inside_a_listing(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "L", {"a": Removed(), "b": Unchanged()}, full=True))
        assert _states(replica) == {"a": RecordState.REMOVED, "b": RecordState.SYNCED}
        assert _feed(replica, 2) == [(FeedEntryKind.REMOVED, "a")]

    def test_a_listing_meets_pending_edits(self, replica):
        _sync(
            replica,
            _page(None, "T1", {o: _changed(f"f{o}") for o in ("a", "b", "c")}, full=True),
        )
        _change(replica, _key(replica, "a"), "fl")
        replica.delete(_key(replica, "b"))
        creates = [
            _create(replica, "fn", object_id="n"),
            _create(replica, "n1"),
            _create(replica, "n2"),
        ]
        _sync(replica, _page("T1", "L", {}, full=True))
        assert [_summary(replica.find_record(_SCOPE, o)) for o in ("a", "b", "c")] == [
            _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
            _REMOVED,
            _REMOVED,
        ]
        assert [_summary(replica.record(key)) for key in creates] == [_pending(EditKind.CREATE)] * 3

    def test_interrupted_listing_then_a_new_one(self, replica):
        _seeded(replica)
        replica.reset(_SCOPE)
        with pytest.raises(RuntimeError):
            with _open_session(replica) as session:
                session.ingest(
                    _page(
                        None, "L1", {"a": Unchanged(), "b": Unchanged()}, full=True, truncated=True
                    )
                )
                raise RuntimeError("connection lost")
        assert replica.scope(_SCOPE).token is None
        _sync(replica, _page(None, "L2", {"a": Unchanged()}, full=True))
        assert _states(replica) == {"a": RecordState.SYNCED, "b": RecordState.REMOVED}
        assert replica.scope(_SCOPE).token == "L2"

    def test_pages_of_one_listing_carry_one_token(self, replica, store):
        with _open_session(replica) as session:
            session.ingest(_page(None, "L1", {}, full=True, truncated=True))
            page = _page(None, "L2", {}, full=True)
            _assert_refused(
                replica, store, lambda: session.ingest(page), StaleChangeSetError, "Page of listing"
            )

    def test_listing_against_the_current_token(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "L", {"a": Unchanged(), "b": _changed("fb2", "v2")}, full=True))
        assert replica.scope(_SCOPE).token == "L"
        assert replica.known_versions(_SCOPE) == {"a": "v1", "b": "v2"}

    def test_stale_page_lets_the_same_session_start_over(self, replica, store):
        _seeded(replica)
        with _open_session(replica) as session:
            session.ingest(_page("T1", "L1", {"a": Unchanged()}, full=True, truncated=True))
            other = _page("T1", "L-other", {}, full=True)
            _assert_refused(replica, store, lambda: session.ingest(other), StaleChangeSetError)
            assert session.state is SessionState.IDLE
            session.ingest(_page("T1", "L2", {"a": Unchanged()}, full=True))
        assert replica.scope(_SCOPE).token == "L2"
        assert _states(replica)["b"] is RecordState.REMOVED

    def test_interleaved_listings_refuse_the_older_one(self, make_store, clock):
        store = make_store()
        a, b = Replica(store, clock=clock), Replica(make_store(), clock=clock)
        with _open_session(a) as session_a:
            session_a.ingest(_page(None, "LA", {"x": _changed("fx")}, full=True, truncated=True))
            with _open_session(b) as session_b:
                session_b.ingest(
                    _page(None, "LB", {"y": _changed("fy")}, full=True, truncated=True)
                )
                last = _page(None, "LA", {}, full=True)
                _assert_refused(
                    a, store, lambda: session_a.ingest(last), StaleChangeSetError, "is listing"
                )
                session_b.ingest(_page(None, "LB", {"x": Unchanged()}, full=True))
        assert b.scope(_SCOPE).token == "LB"
        assert {r.object_id for r in b.records(_SCOPE) if r.state is RecordState.SYNCED} == {
            "x",
            "y",
        }


class TestSessions:
    def test_ingest_after_the_last_page_is_refused(self, replica, store):
        with _open_session(replica) as session:
            session.ingest(_page(None, "T1", {}, full=True))
            page = _page("T1", "T2", {})
            _assert_refused(
                replica,
                store,
                lambda: session.ingest(page),
                SessionError,
                "last page",
                before_a_transaction=True,
            )

    def test_listing_and_incremental_pages_do_not_mix(self, replica, store):
        _seeded(replica)
        with _open_session(replica) as session:
            session.ingest(_page("T1", "C1", {}, truncated=True))
            listing = _page("C1", "L", {}, full=True)
            _assert_refused(
                replica,
                store,
                lambda: session.ingest(listing),
                SessionError,
                "all listing pages",
                before_a_transaction=True,
            )

    def test_ended_session_refuses_ingests(self, replica, store):
        with _open_session(replica) as session:
            session.ingest(_page(None, "L", {}, full=True, truncated=True))
        assert session.state is SessionState.IDLE
        page = _page(None, "L", {}, full=True)
        _assert_refused(
            replica,
            store,
            lambda: session.ingest(page),
            SessionError,
            "has ended",
            before_a_transaction=True,
        )

    def test_one_open_session_per_scope(self, replica, store):
        with _open_session(replica) as session:
            assert session.scope_id == _SCOPE
            second = _open_session(replica)
            _assert_refused(
                replica,
                store,
                lambda: _enter(second),
                SessionError,
                "already open",
                before_a_transaction=True,
            )
        _enter(_open_session(replica))

    @pytest.mark.parametrize("method", ["reset", "drop_scope"])
    def test_reset_and_drop_refused_while_a_session_is_open(self, replica, store, method):
        _seeded(replica)
        with _open_session(replica):
            call = getattr(replica, method)
            _assert_refused(
                replica,
                store,
                lambda: call(_SCOPE),
                SessionError,
                "is open",
                before_a_transaction=True,
            )

    def test_media_type_mismatch(self, replica, store):
        _seeded(replica)
        other = _open_session(replica, media="application/json")
        _assert_refused(replica, store, lambda: _enter(other), ScopeMismatchError)
        _enter(_open_session(replica))

    def test_fingerprint_scheme_change_resets_and_clears_versions(self, replica, store):
        _seeded(replica)
        with _open_session(replica, scheme=_OTHER_SCHEME) as session:
            scope = replica.scope(_SCOPE)
            assert (scope.token, scope.fingerprint_scheme) == (None, _OTHER_SCHEME)
            assert {(r.version, r.fingerprint) for r in replica.records(_SCOPE)} == {(None, None)}
            assert replica.known_versions(_SCOPE) == {}
            page = _page(None, "T2", {})
            _assert_refused(
                replica,
                store,
                lambda: session.ingest(page),
                ListingRequiredError,
                "needs a full listing",
            )

    def test_listing_after_a_scheme_change_reports_only_new_content(self, replica):
        _seeded(replica)
        relisted = {
            "a": _changed("new-a", content=b"text of fa"),
            "b": _changed("new-b", content=b"b edited meanwhile"),
        }
        with _open_session(replica, scheme=_OTHER_SCHEME) as session:
            session.ingest(_page(None, "L", relisted, full=True))
        assert _feed(replica, 2) == [(FeedEntryKind.CHANGED, "b")]
        assert {r.object_id: r.fingerprint for r in replica.records(_SCOPE)} == {
            "a": "new-a",
            "b": "new-b",
        }

    def test_reset_keeps_records(self, replica):
        _seeded(replica)
        replica.reset(_SCOPE)
        assert replica.scope(_SCOPE).token is None
        assert _states(replica) == {"a": RecordState.SYNCED, "b": RecordState.SYNCED}
        assert replica.known_versions(_SCOPE) == {"a": "v1", "b": "v1"}


class TestScopes:
    def test_unknown_scope(self, replica, store):
        _seeded(replica)
        for call in (
            replica.scope,
            replica.records,
            replica.known_versions,
            replica.reset,
            replica.drop_scope,
            lambda scope: replica.find_record(scope, "a"),
            lambda scope: replica.find_by_logical_key(scope, "u"),
            lambda scope: _create(replica, "fn", scope=scope),
        ):
            _assert_refused(replica, store, lambda call=call: call("nope"), UnknownScopeError)

    def test_two_scopes_stay_independent(self, replica):
        _a_removed(replica)
        _sync(replica, _page(None, "O1", {"a": _changed("other-a")}, full=True, scope=_OTHER))
        assert [s.id for s in replica.scopes()] == [_SCOPE, _OTHER]
        assert replica.records(_OTHER)[0].state is RecordState.SYNCED
        assert replica.scope(_OTHER).token == "O1"
        replica.drop_scope(_SCOPE)
        assert [s.id for s in replica.scopes()] == [_OTHER]

    def test_drop_scope(self, replica, store):
        _edited(replica)
        created = _create(replica, "fn")
        with store.transaction():
            in_flight = store.insert_record(_local_record(RecordState.IN_FLIGHT, object_id="f"))
        pending, head = [_key(replica), created, in_flight], replica.feed_head()
        dropped = replica.drop_scope(_SCOPE)
        assert dropped.scope == _SCOPE
        assert [r.local_key for r in dropped.pending] == pending
        assert [r.object_id for r in dropped.conflicts] == ["b"]
        assert replica.scopes() == []
        assert _feed(replica, head) == [(FeedEntryKind.SCOPE_DROPPED, None)]


# What a record ends up as, in the shape _summary gives: state, edit kind,
# conflict kind.
_SYNCED = (RecordState.SYNCED, None, None)
_REMOVED = (RecordState.REMOVED, None, None)


def _pending(edit: EditKind) -> tuple[object, ...]:
    return (RecordState.PENDING, edit, None)


def _conflicted(edit: EditKind, kind: ConflictKind) -> tuple[object, ...]:
    return (RecordState.CONFLICT, edit, kind)


# The design's change classes as an ingest applies them: the local edit,
# what the server reported, then what the record ends up as and the feed
# entries the ingest wrote.
_CLASSIFIED = [
    (None, None, _SYNCED, []),
    (None, "fa", _SYNCED, []),
    (None, "fx", _SYNCED, [FeedEntryKind.CHANGED]),
    (None, "removed", _REMOVED, [FeedEntryKind.REMOVED]),
    ("change", None, _pending(EditKind.CHANGE), []),
    ("change", "fa", _pending(EditKind.CHANGE), []),
    ("change", "fl", _SYNCED, [FeedEntryKind.CHANGED]),
    (
        "change",
        "fx",
        _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    (
        "change",
        "removed",
        _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    ("delete", None, _pending(EditKind.DELETE), []),
    ("delete", "fa", _pending(EditKind.DELETE), []),
    (
        "delete",
        "fx",
        _conflicted(EditKind.DELETE, ConflictKind.DELETED_LOCALLY),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    ("delete", "removed", _REMOVED, [FeedEntryKind.REMOVED]),
    ("create", None, _pending(EditKind.CREATE), []),
    ("create", "removed", _pending(EditKind.CREATE), []),
    ("create", "fl", _SYNCED, [FeedEntryKind.CHANGED]),
    (
        "create",
        "fx",
        _conflicted(EditKind.CREATE, ConflictKind.BOTH_CREATED),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
]

# An open conflict meeting the server's next report: the local edit, the
# report that opened the conflict, the next one, then what the record ends
# up as and the feed entries the second ingest wrote.
_SETTLED = [
    ("change", "fx", "fl", _SYNCED, [FeedEntryKind.CONFLICT_RESOLVED]),
    ("change", "fx", "fa", _pending(EditKind.CHANGE), [FeedEntryKind.CONFLICT_RESOLVED]),
    ("change", "removed", "fa", _pending(EditKind.CHANGE), [FeedEntryKind.CONFLICT_RESOLVED]),
    ("delete", "fx", "removed", _REMOVED, [FeedEntryKind.CONFLICT_RESOLVED]),
    ("delete", "fx", "fa", _pending(EditKind.DELETE), [FeedEntryKind.CONFLICT_RESOLVED]),
    ("create", "fx", "removed", _pending(EditKind.CREATE), [FeedEntryKind.CONFLICT_RESOLVED]),
    ("create", "fx", "fl", _SYNCED, [FeedEntryKind.CONFLICT_RESOLVED]),
    (
        "change",
        "fx",
        "fy",
        _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED),
        [FeedEntryKind.CONFLICT_UPDATED],
    ),
    (
        "change",
        "fx",
        "removed",
        _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
        [FeedEntryKind.CONFLICT_UPDATED],
    ),
    ("change", "fx", "fx", _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED), []),
]

# How a push ended, recorded with settle(): the local edit, the outcome,
# then what the record ends up as and the feed entries settling wrote.
_OUTCOMES = [
    ("change", Applied(), _SYNCED, [FeedEntryKind.PUSHED]),
    ("create", Applied(), _SYNCED, [FeedEntryKind.PUSHED]),
    ("delete", Applied(), _REMOVED, [FeedEntryKind.PUSHED]),
    ("delete", Gone(), _REMOVED, [FeedEntryKind.PUSHED]),
    ("change", Rejected(), _pending(EditKind.CHANGE), [FeedEntryKind.PUSH_FAILED]),
    ("delete", Rejected(), _pending(EditKind.DELETE), [FeedEntryKind.PUSH_FAILED]),
    ("create", Rejected(), _pending(EditKind.CREATE), [FeedEntryKind.PUSH_FAILED]),
    (
        "change",
        Gone(),
        _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    (
        "create",
        LogicalKeyTaken("b"),
        _conflicted(EditKind.CREATE, ConflictKind.LOGICAL_KEY_TAKEN),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    (
        "create",
        LogicalKeyTaken(),
        _conflicted(EditKind.CREATE, ConflictKind.LOGICAL_KEY_TAKEN),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    (
        "change",
        LogicalKeyTaken("b"),
        _conflicted(EditKind.CHANGE, ConflictKind.LOGICAL_KEY_TAKEN),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    ("change", Unknown(), (RecordState.IN_FLIGHT, EditKind.CHANGE, None), []),
]

# A push whose outcome never arrived, met by the next complete ingest: the
# local edit, what the server reported, then what the record ends up as and
# the feed entries the ingest wrote.
_LEFT_IN_FLIGHT = [
    ("change", None, _pending(EditKind.CHANGE), [FeedEntryKind.PUSH_FAILED]),
    ("change", "fa", _pending(EditKind.CHANGE), [FeedEntryKind.PUSH_FAILED]),
    ("change", "fl", _SYNCED, [FeedEntryKind.PUSHED]),
    (
        "change",
        "fx",
        _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    (
        "change",
        "removed",
        _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    ("delete", None, _pending(EditKind.DELETE), [FeedEntryKind.PUSH_FAILED]),
    ("delete", "fa", _pending(EditKind.DELETE), [FeedEntryKind.PUSH_FAILED]),
    (
        "delete",
        "fx",
        _conflicted(EditKind.DELETE, ConflictKind.DELETED_LOCALLY),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
    ("delete", "removed", _REMOVED, [FeedEntryKind.PUSHED]),
    ("create", None, _pending(EditKind.CREATE), [FeedEntryKind.PUSH_FAILED]),
    ("create", "removed", _pending(EditKind.CREATE), [FeedEntryKind.PUSH_FAILED]),
    ("create", "fl", _SYNCED, [FeedEntryKind.PUSHED]),
    # Another object at the create's id isn't the create.
    (
        "create",
        "fx",
        _conflicted(EditKind.CREATE, ConflictKind.BOTH_CREATED),
        [FeedEntryKind.CONFLICT_OPENED],
    ),
]

# An outcome arriving after another process's ingest already settled the
# push: what that ingest saw (None for nothing, "edited" for nothing and
# then a new local edit, "claimed again" for nothing and then a claim of its
# own), the outcome, then what settle() returns, what the record ends up as
# and the feed entries settling wrote.
_SETTLED_LATE = [
    (None, Applied(), True, _SYNCED, [FeedEntryKind.PUSHED]),
    (None, Rejected(), True, _pending(EditKind.CHANGE), []),
    (None, Unknown(), True, _pending(EditKind.CHANGE), []),
    ("edited", Applied(), False, _pending(EditKind.CHANGE), []),
    ("fl", Applied(), False, _SYNCED, []),
    ("fx", Rejected(), False, _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED), []),
    ("claimed again", Rejected(), False, (RecordState.IN_FLIGHT, EditKind.CHANGE, None), []),
]

# Records settle() refuses because this session didn't claim them so.
_UNCLAIMED = ("not claimed", "another record")

# The rows of _SETTLED_LATE where the server reported nothing new.
_NOTHING_NEW = (None, "edited", "claimed again")

_MERGED = Merge(_content("fm"), EditFields("fm"))

# Each conflict resolved each way: the conflict's kind, the kind of edit a
# refused one was, the resolution, then what the record ends up as, or None
# once it's gone.
_RESOLVED = [
    (ConflictKind.BOTH_CHANGED, "create", KeepServer(), _SYNCED),
    (ConflictKind.BOTH_CHANGED, "create", KeepLocal(), _pending(EditKind.CHANGE)),
    (ConflictKind.BOTH_CHANGED, "create", _MERGED, _pending(EditKind.CHANGE)),
    (ConflictKind.BOTH_CHANGED, "create", Delete(), _pending(EditKind.DELETE)),
    (ConflictKind.REMOVED_REMOTELY, "create", KeepServer(), _REMOVED),
    (ConflictKind.REMOVED_REMOTELY, "create", KeepLocal(), _pending(EditKind.CREATE)),
    (ConflictKind.REMOVED_REMOTELY, "create", _MERGED, _pending(EditKind.CREATE)),
    (ConflictKind.REMOVED_REMOTELY, "create", Delete(), _REMOVED),
    (ConflictKind.DELETED_LOCALLY, "create", KeepServer(), _SYNCED),
    (ConflictKind.DELETED_LOCALLY, "create", KeepLocal(), _pending(EditKind.DELETE)),
    (ConflictKind.DELETED_LOCALLY, "create", _MERGED, _pending(EditKind.CHANGE)),
    (ConflictKind.DELETED_LOCALLY, "create", Delete(), _pending(EditKind.DELETE)),
    (ConflictKind.BOTH_CREATED, "create", KeepServer(), _SYNCED),
    (ConflictKind.BOTH_CREATED, "create", KeepLocal(), _pending(EditKind.CHANGE)),
    (ConflictKind.BOTH_CREATED, "create", _MERGED, _pending(EditKind.CHANGE)),
    (ConflictKind.BOTH_CREATED, "create", Delete(), _pending(EditKind.DELETE)),
    (ConflictKind.LOGICAL_KEY_TAKEN, "create", KeepServer(), None),
    (ConflictKind.LOGICAL_KEY_TAKEN, "create", _MERGED, _pending(EditKind.CREATE)),
    (ConflictKind.LOGICAL_KEY_TAKEN, "create", Delete(), None),
    (ConflictKind.LOGICAL_KEY_TAKEN, "create", UpdateHolder(), None),
    (ConflictKind.LOGICAL_KEY_TAKEN, "change", KeepServer(), _SYNCED),
    (ConflictKind.LOGICAL_KEY_TAKEN, "change", _MERGED, _pending(EditKind.CHANGE)),
    (ConflictKind.LOGICAL_KEY_TAKEN, "change", Delete(), _pending(EditKind.DELETE)),
    (ConflictKind.LOGICAL_KEY_TAKEN, "change", UpdateHolder(), _SYNCED),
]

# Local edits in a row: where the record starts (synced as fa, or a
# create), the edits, then what the record ends up as, or None once it's gone.
_EDITED = [
    ("synced", ["change fl"], _pending(EditKind.CHANGE)),
    ("synced", ["change fa"], _SYNCED),
    ("synced", ["change fl", "change fa"], _SYNCED),
    ("synced", ["change fl", "delete"], _pending(EditKind.DELETE)),
    ("synced", ["delete", "change fl"], _pending(EditKind.CHANGE)),
    ("synced", ["delete", "change fa"], _SYNCED),
    ("synced", ["change fl", "discard"], _SYNCED),
    ("synced", ["discard"], _SYNCED),
    ("create", [], _pending(EditKind.CREATE)),
    ("create", ["change fx"], _pending(EditKind.CREATE)),
    ("create", ["delete"], None),
    ("create", ["discard"], None),
    ("create", ["change fx", "delete"], None),
]

# Strings a store must give back character for character.
_TEXTS = [
    "emoji \U0001f600 and accents \u00e9\u00e0",
    "nul \x00 inside",
    "\ufeffbyte order mark",
    "trailing whitespace  \n\n",
    "a line\r\nand the next",
    "",
]

# Content a store must give back byte for byte, valid UTF-8 or not.
_CONTENTS = [
    b"BEGIN:VCALENDAR\r\nX-NOTE:a line\r\n folded\r\nEND:VCALENDAR\r\n",
    "emoji \U0001f600 and accents \u00e9\u00e0".encode(),
    b"nul \x00 inside",
    b"\xef\xbb\xbfbyte order mark",
    b"not UTF-8 \xff\xfe\xc3",
    b"",
]

# A string no store can keep: a lone surrogate.
_BAD_TEXT = "a\ud800b"


def _wrong(value: object) -> Any:
    """A value of the wrong type, passed where the type checker can't object."""
    return value


_NOT_BYTES = _wrong("text of fa")


class TestLocalEdits:
    @pytest.mark.parametrize("start, steps, expected", _EDITED)
    def test_edits_collapse(self, replica, start, steps, expected):
        key, _ = _seeded_edit(replica, "create" if start == "create" else None)
        head = replica.feed_head()
        for step in steps:
            _step(replica, key, step)
        record = replica.record(key)
        assert _summary(record) == expected
        if record is not None:
            assert record.content == (None if start == "create" else _content("fa"))
        assert replica.feed_head() == head

    def test_create_keeps_what_it_was_given(self, replica, clock):
        _seeded(replica)
        key = _create(
            replica,
            "fn",
            containers=frozenset({"x", "y"}),
            logical_key="u",
            object_id="n",
        )
        record = replica.record(key)
        assert (record.object_id, record.content, record.version) == ("n", None, None)
        assert record.edit == LocalEdit(
            EditKind.CREATE, _content("fn"), "fn", frozenset({"x", "y"}), "u", clock.now
        )

    def test_a_create_at_an_id_in_use(self, replica, store):
        _a_removed(replica)
        _assert_refused(
            replica,
            store,
            lambda: _create(replica, "fn", object_id="b"),
            DuplicateObjectError,
        )
        removed = replica.find_record(_SCOPE, "a")
        key = _create(replica, "fn", object_id="a")
        record = replica.record(key)
        assert key == removed.local_key
        assert _summary(record) == _pending(EditKind.CREATE)
        assert (record.content, record.version) == (None, None)
        assert record.last_entry == removed.last_entry
        assert replica.purge(replica.feed_head()) == 0
        _sync(replica, _page("T2", "T3", {"a": _changed("fn", "v3")}))
        assert _summary(replica.record(key)) == _SYNCED

    @pytest.mark.parametrize("step", ["change fy", "delete", "discard"])
    def test_records_that_take_no_edit(self, replica, store, step):
        _a_removed(replica)
        _in_conflict(replica, "b", "T2", "T3")
        with store.transaction():
            in_flight = store.insert_record(_local_record(RecordState.IN_FLIGHT, object_id="f"))
        refused = {
            _key(replica, "a"): RemovedRecordError,
            _key(replica, "b"): OpenConflictError,
            in_flight: PushInFlightError,
            999: UnknownRecordError,
        }
        for key, error in refused.items():
            _assert_refused(replica, store, lambda key=key: _step(replica, key, step), error)

    def test_an_edit_from_one_handle_meets_an_ingest_from_another(self, make_store, clock):
        editor = _seeded(Replica(make_store(), clock=clock))
        syncer = Replica(make_store(), clock=clock)
        _change(editor, _key(editor), "fl")
        _sync(syncer, _page("T1", "T2", {"a": _changed("fx", "v2")}))
        assert _summary(editor.find_record(_SCOPE, "a")) == _conflicted(
            EditKind.CHANGE, ConflictKind.BOTH_CHANGED
        )


class TestConflicts:
    @pytest.mark.parametrize("local, remote, expected, feed", _CLASSIFIED)
    def test_ingest_classifies_against_the_local_edit(self, replica, local, remote, expected, feed):
        key, object_id = _seeded_edit(replica, local)
        head = replica.feed_head()
        _sync(replica, _page("T1", "T2", {} if remote is None else {object_id: _remote(remote)}))
        _assert_ended(replica, key, head, expected, feed)

    @pytest.mark.parametrize("local, opening, following, expected, feed", _SETTLED)
    def test_an_open_conflict_meets_the_next_report(
        self, replica, local, opening, following, expected, feed
    ):
        key, object_id = _seeded_edit(replica, local)
        _sync(replica, _page("T1", "T2", {object_id: _remote(opening)}))
        opened_at = replica.record(key).conflict.opened_at
        head = replica.feed_head()
        _sync(replica, _page("T2", "T3", {object_id: _remote(following, "v3")}))
        record = _assert_ended(replica, key, head, expected, feed)
        if record is not None and record.conflict is not None:
            assert record.conflict.opened_at == opened_at
            assert record.conflict.remote_version == (None if following == "removed" else "v3")

    @pytest.mark.parametrize(
        "following, expected, feed",
        [
            (
                "fx",
                _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED),
                [FeedEntryKind.CONFLICT_UPDATED],
            ),
            (
                "removed",
                _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
                [FeedEntryKind.CONFLICT_UPDATED],
            ),
            ("fl", _SYNCED, [FeedEntryKind.CONFLICT_RESOLVED]),
        ],
    )
    def test_a_refused_change_meets_the_next_report(self, replica, following, expected, feed):
        # The refusal holds no copy of the server's object, so any report is news.
        key = _in_conflict(_seeded(replica), kind=ConflictKind.LOGICAL_KEY_TAKEN, refused="change")
        # The server still holds the synced copy, so a listing needn't fetch it.
        assert replica.known_versions(_SCOPE)["a"] == replica.record(key).version
        head = replica.feed_head()
        _sync(replica, _page("T2", "T3", {"a": _remote(following, "v3")}))
        _assert_ended(replica, key, head, expected, feed)

    @pytest.mark.parametrize("refused", [EditKind.CREATE, EditKind.CHANGE])
    @pytest.mark.parametrize(
        "held, report, lifted, feed",
        [
            (
                True,
                {"b": Removed()},
                True,
                [FeedEntryKind.REMOVED, FeedEntryKind.CONFLICT_RESOLVED],
            ),
            (
                True,
                {"b": _changed("fb", "v3", logical_key="w")},
                True,
                [FeedEntryKind.CHANGED, FeedEntryKind.CONFLICT_RESOLVED],
            ),
            # A listing that leaves the holder out removes it.
            (
                True,
                {"a": Unchanged()},
                True,
                [FeedEntryKind.REMOVED, FeedEntryKind.CONFLICT_RESOLVED],
            ),
            (True, {"b": _changed("fx", "v3", logical_key="u")}, False, [FeedEntryKind.CHANGED]),
            (
                True,
                {"b": Removed(), "c": _changed("fc", logical_key="u")},
                False,
                [FeedEntryKind.REMOVED, FeedEntryKind.CHANGED],
            ),
            # An object outside the scope holds the key, so nothing here frees it.
            (False, {"b": Removed()}, False, [FeedEntryKind.REMOVED]),
        ],
        ids=[
            "holder-removed",
            "holder-rekeyed",
            "holder-unlisted",
            "holder-kept",
            "key-taken-again",
            "holder-elsewhere",
        ],
    )
    def test_a_refusal_ends_when_the_server_frees_its_key(
        self, replica, refused, held, report, lifted, feed
    ):
        holding = {"b": _changed("fb", "v2", logical_key="u")} if held else {}
        _sync(_seeded(replica), _page("T1", "T2", holding))
        key = _refused_edit(replica, refused)
        head = replica.feed_head()
        listing = any(isinstance(change, Unchanged) for change in report.values())
        _sync(replica, _page("T3", "T4", report, full=listing))
        expected = (
            _pending(refused) if lifted else _conflicted(refused, ConflictKind.LOGICAL_KEY_TAKEN)
        )
        _assert_ended(replica, key, head, expected, feed)

    @pytest.mark.parametrize("refused", [EditKind.CREATE, EditKind.CHANGE])
    def test_a_refusal_ends_when_your_push_frees_its_key(self, replica, refused):
        _sync(_seeded(replica), _page("T1", "T2", {"b": _changed("fb", "v2", logical_key="u")}))
        key = _refused_edit(replica, refused)
        holder = _key(replica, "b")
        replica.delete(holder)
        head = replica.feed_head()
        _push(replica, holder, Applied(), "T3", "T4")
        _assert_ended(
            replica,
            key,
            head,
            _pending(refused),
            [FeedEntryKind.PUSHED, FeedEntryKind.CONFLICT_RESOLVED],
        )

    def test_a_refused_change_is_not_the_holder_of_its_own_key(self, replica):
        # a's synced copy already has u, so the server keeps it under u; only b blocks it.
        holding = {k: _changed(f"f{k}", "v2", logical_key="u") for k in "ab"}
        _sync(_seeded(replica), _page("T1", "T2", holding))
        key = _refused_edit(replica, EditKind.CHANGE)
        head = replica.feed_head()
        _sync(replica, _page("T3", "T4", {"b": Removed()}))
        _assert_ended(
            replica,
            key,
            head,
            _pending(EditKind.CHANGE),
            [FeedEntryKind.REMOVED, FeedEntryKind.CONFLICT_RESOLVED],
        )

    @pytest.mark.parametrize("refused", [EditKind.CREATE, EditKind.CHANGE])
    def test_a_holder_in_conflict_holds_the_key_of_the_server_side(self, replica, refused):
        _sync(_seeded(replica), _page("T1", "T2", {"b": _changed("fb", "v2", logical_key="u")}))
        key = _refused_edit(replica, refused)
        # b's synced copy keeps u, but the server's copy in its conflict moved to w.
        _change(replica, _key(replica, "b"), "fl")
        head = replica.feed_head()
        _sync(replica, _page("T3", "T4", {"b": _changed("fx", "v3", logical_key="w")}))
        _assert_ended(
            replica,
            key,
            head,
            _pending(refused),
            [FeedEntryKind.CONFLICT_OPENED, FeedEntryKind.CONFLICT_RESOLVED],
        )

    def test_a_version_only_change_moves_the_version_under_an_edit(self, replica):
        key, _ = _seeded_edit(replica, "change")
        _sync(replica, _page("T1", "T2", {"a": _changed("fa", "v2", content=b"rewritten")}))
        record = replica.record(key)
        assert (record.state, record.version, record.content) == (
            RecordState.PENDING,
            "v2",
            b"rewritten",
        )
        assert record.edit.content == _content("fl")

    @pytest.mark.parametrize(
        "opening, kind, known",
        [("fx", ConflictKind.BOTH_CHANGED, "v2"), ("removed", ConflictKind.REMOVED_REMOTELY, None)],
    )
    def test_an_open_conflict_waits_for_news(self, replica, opening, kind, known):
        key, _ = _seeded_edit(replica, "change")
        _sync(replica, _page("T1", "T2", {"a": _remote(opening)}))
        # The server's version of the conflicted copy, none once it removed it.
        assert replica.known_versions(_SCOPE).get("a") == known
        head = replica.feed_head()
        # Nothing new about a: left out of a change set, then unchanged in a
        # listing, or not listed at all once the server removed it.
        _sync(replica, _page("T2", "T3", {}))
        listing = {"b": Unchanged()} if known is None else {"a": Unchanged(), "b": Unchanged()}
        _sync(replica, _page("T3", "L", listing, full=True))
        assert _summary(replica.record(key)) == _conflicted(EditKind.CHANGE, kind)
        assert _feed(replica, head) == []

    def test_reset_keeps_edits_and_conflicts(self, replica):
        _edited(replica)
        before = replica.records(_SCOPE)
        replica.reset(_SCOPE)
        assert replica.records(_SCOPE) == before

    def test_a_scheme_change_makes_edits_compare_by_content(self, replica):
        _edited(replica)
        with _open_session(replica, scheme=_OTHER_SCHEME):
            pass
        a, b = replica.records(_SCOPE)
        assert (a.version, a.fingerprint, a.edit.fingerprint) == (None, None, None)
        assert (b.conflict.remote_version, b.conflict.remote_fingerprint) == (None, None)
        assert replica.known_versions(_SCOPE) == {}
        head = replica.feed_head()
        listing = {
            "a": _changed("new-fa", content=_content("fa")),
            "b": _changed("new-fx", "v2", content=_content("fx")),
        }
        with _open_session(replica, scheme=_OTHER_SCHEME) as session:
            session.ingest(_page(None, "T3", listing, full=True))
        a, b = replica.records(_SCOPE)
        assert _summary(a) == _pending(EditKind.CHANGE)
        assert _summary(b) == _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED)
        assert (b.conflict.remote_version, b.conflict.remote_fingerprint) == ("v2", "new-fx")
        assert _feed(replica, head) == []


def _first_push_rejected(replica: Replica, guard_covers_scope: bool) -> Any:
    """The docs' example adapter, its first push rejected and every later one applied."""
    memory = _examples.load("memory_adapter")
    # Loaded from its file, so a type checker can't see its classes.
    example: Any = memory.MemoryAdapter

    class FirstPushRejected(example):
        def __init__(self) -> None:
            server = memory.MemoryServer()
            server.put("a", b"a", if_version=None)
            super().__init__(replica, server, _SCOPE)
            self._guard_covers_scope = guard_covers_scope
            self.sent: list[int] = []

        def _send(self, claimed: Record) -> Steps[PushOutcome]:
            self.sent.append(claimed.local_key)
            if len(self.sent) == 1:
                yield from ()
                return Rejected()
            return (yield from super()._send(claimed))

    return FirstPushRejected()


# Each protocol's scenario module, and each recording of each.
_PROTOCOLS = (_caldav_scenarios, _jmap_scenarios)
_RECORDED = [
    pytest.param(module, server, scenario, id=f"{module.PROTOCOL}-{server}-{scenario}")
    for module in _PROTOCOLS
    for server, scenario in module.RECORDINGS
]


class TestRecordings:
    @pytest.mark.parametrize("scenarios, server, scenario", _RECORDED)
    def test_every_recording_replays(self, scenarios, server, scenario, store, asynchronous):
        tape = Tape.load(scenarios.PROTOCOL, f"{server}-{scenario}")
        run(
            scenarios,
            scenarios.SERVERS[server],
            scenario,
            tape,
            store=store,
            asynchronous=asynchronous,
        )
        assert tape.finished

    @pytest.mark.parametrize("scenarios, server, scenario", _RECORDED)
    def test_sync_and_async_adapters_leave_the_same_store(self, scenarios, server, scenario):
        def replayed(asynchronous: bool) -> object:
            store = MemoryStore()
            tape = Tape.load(scenarios.PROTOCOL, f"{server}-{scenario}")
            run(
                scenarios,
                scenarios.SERVERS[server],
                scenario,
                tape,
                store=store,
                asynchronous=asynchronous,
            )
            replica = Replica(store)
            scopes = replica.scopes()
            held = (scopes, [replica.records(s.id) for s in scopes], replica.read_feed(0))
            return _timeless(held)

        assert replayed(asynchronous=True) == replayed(asynchronous=False)

    @pytest.mark.parametrize("scenarios", _PROTOCOLS, ids=lambda module: module.PROTOCOL)
    def test_every_scenario_is_recorded(self, scenarios):
        assert {scenario for _, scenario in scenarios.RECORDINGS} == set(scenarios.SCENARIOS)


class TestPushes:
    @pytest.mark.parametrize(
        "flow, guarded_by_version, unique_logical_key, guard_covers_scope",
        [
            # A push carries the object's ETag; one calendar may hold two
            # objects with one UID; a refusal is about that object alone.
            pytest.param(_CalDAVFlow, True, False, False, id="caldav"),
            # A push carries the account's state; one account holds one event
            # per uid and recurrence id; a refusal covers every event.
            pytest.param(_JMAPFlow, False, True, True, id="jmap"),
        ],
    )
    def test_each_adapter_declares_what_its_server_checks(
        self, flow, guarded_by_version, unique_logical_key, guard_covers_scope
    ):
        declared = (flow._guarded_by_version, flow._unique_logical_key, flow._guard_covers_scope)
        assert declared == (guarded_by_version, unique_logical_key, guard_covers_scope)

    @_needs_examples
    @pytest.mark.parametrize("covers_scope", [True, False], ids=["scope", "object"])
    def test_a_rejected_push_stops_the_rest_where_the_check_covers_the_scope(
        self, replica, covers_scope
    ):
        flow = _first_push_rejected(replica, covers_scope)
        flow.sync()
        changed = _key(replica)
        flow.change(changed, b"fl")
        created = flow.create(b"fn")
        flow.sync()
        # Where the check covers the scope, the create would be rejected too, so it waits unsent.
        assert flow.sent == ([changed] if covers_scope else [changed, created])
        later = replica.record(created)
        assert later is not None
        assert later.state is (RecordState.PENDING if covers_scope else RecordState.SYNCED)

    def test_to_push_lists_pending_edits_by_local_key(self, replica):
        _edited(replica)
        created = _create(replica, "fn")
        with _pushing(replica, "T2", "T3") as session:
            assert [r.local_key for r in session.to_push()] == [_key(replica), created]

    def test_a_guarded_push_waits_for_a_version(self, replica):
        _seeded(replica)
        key = _key(replica)
        _change(replica, key, "fl")
        _push(replica, key, Applied())
        _change(replica, key, "fy")
        created = _create(replica, "fn")
        with _pushing(replica, "T2", "T3") as session:
            assert [r.local_key for r in session.to_push()] == [key, created]
        # The applied push left no version, so a guarded push of the next
        # edit would go out unguarded; it waits for an ingest that has one.
        with _pushing(replica, "T3", "T4", guarded_by_version=True) as session:
            assert [r.local_key for r in session.to_push()] == [created]
            assert session.claim(key) is None
        _sync(replica, _page("T4", "T5", {"a": _changed("fl", "v5")}))
        with _pushing(replica, "T5", "T6", guarded_by_version=True) as session:
            assert [r.local_key for r in session.to_push()] == [key, created]
            assert _claim(session, key).version == "v5"

    @pytest.mark.parametrize("local, outcome, expected, feed", _OUTCOMES)
    def test_settle_records_how_a_push_ended(self, replica, local, outcome, expected, feed):
        key, _ = _seeded_edit(replica, local)
        head = replica.feed_head()
        _push(replica, key, outcome)
        record = _assert_ended(replica, key, head, expected, feed)
        assert record is not None
        if expected == _SYNCED:
            # The server may store the edit rewritten, so it gives no version to keep.
            assert (record.version, record.fingerprint, record.content) == (
                None,
                "fl",
                _content("fl"),
            )
        if isinstance(outcome, LogicalKeyTaken):
            assert record.conflict is not None and record.conflict.holder == outcome.holder

    @pytest.mark.parametrize(
        "local, outcome, message",
        [
            ("create", Gone(), "can't be gone"),
            ("delete", LogicalKeyTaken("b"), "^A delete can't be refused for its logical key$"),
            ("delete", Applied("x"), "no object id"),
            ("create without an id", Applied(), "needs the object id"),
            ("not claimed", Applied(), "takes a record"),
            ("another record", Applied(), "takes a record"),
        ],
    )
    def test_an_outcome_no_push_can_have_is_refused(self, replica, store, local, outcome, message):
        """Including a record this session didn't claim, or not as it claimed it."""
        _seeded(replica)
        if local == "create without an id":
            key = _create(replica, "fl")
        else:
            key, _ = _edit_locally(replica, "change" if local in _UNCLAIMED else local)
        with _pushing(replica) as session:
            claimed = _claim(session, key)
            if local == "not claimed":
                claimed = replace(claimed, state=RecordState.PENDING)
            if local == "another record":
                claimed = replace(claimed, local_key=_key(replica, "b"))
            _assert_refused(
                replica,
                store,
                lambda: session.settle(claimed, outcome),
                ValueError,
                message,
                before_a_transaction=True,
            )

    def test_pushing_follows_the_session_order(self, replica, store):
        key, _ = _seeded_edit(replica, "change")

        def refused(call: Callable[[], object], match: str) -> None:
            _assert_refused(replica, store, call, SessionError, match, before_a_transaction=True)

        with _open_session(replica) as session:
            for page in (_page("T1", "C", {}, truncated=True), _page("C", "T2", {})):
                refused(session.to_push, "after it ingested its last page")
                refused(lambda: session.claim(key), "after it ingested its last page")
                session.ingest(page)
            claimed = _claim(session, key)
            refused(lambda: session.ingest(_page("T2", "T3", {})), "already pushed")
        refused(session.to_push, "has ended")
        refused(lambda: session.claim(key), "has ended")
        refused(lambda: session.settle(claimed, Applied()), "has ended")

    def test_claim_passes_over_what_has_nothing_to_push(self, replica):
        _edited(replica)
        a, b = _key(replica, "a"), _key(replica, "b")
        with _pushing(replica, "T2", "T3") as session:
            assert session.state is SessionState.INGESTED
            assert _claim(session, a).state is RecordState.IN_FLIGHT
            assert session.state is SessionState.PUSHING
            assert [session.claim(key) for key in (a, b, 999)] == [None, None, None]

    def test_claim_refuses_what_it_cant_push(self, replica, store):
        key, _ = _seeded_edit(replica, "change")
        _sync(replica, _page(None, "O1", {}, full=True, scope=_OTHER))
        elsewhere = _create(replica, "fn", scope=_OTHER)
        with _pushing(replica) as session:
            for call, error, match in [
                (lambda: session.claim(elsewhere), OtherScopeError, "belongs to scope"),
                (
                    lambda: session.claim(key, object_id="x"),
                    ObjectIdAssignedError,
                    "already has object id",
                ),
            ]:
                _assert_refused(replica, store, call, error, match)
            _assert_refused(
                replica,
                store,
                lambda: session.claim(key, object_id=_BAD_TEXT),
                LoneSurrogateError,
                "^object_id ",
                before_a_transaction=True,
            )

    @pytest.mark.parametrize("at_claim", [True, False], ids=["claimed", "applied"])
    @pytest.mark.parametrize("object_id", ["x", "a", "b"])
    def test_a_create_takes_the_id_it_goes_out_under(self, replica, store, at_claim, object_id):
        _a_removed(replica)
        removed = replica.find_record(_SCOPE, "a")
        key = _create(replica, "fn")
        with _pushing(replica, "T2", "T3") as session:
            if at_claim:
                give = partial(session.claim, key, object_id=object_id)
            else:
                give = partial(session.settle, _claim(session, key), Applied(object_id))
            if object_id == "b":
                _assert_refused(replica, store, give, DuplicateObjectError)
                return
            give()
        assert replica.find_record(_SCOPE, object_id).local_key == key
        # A removed record holding the id gives it up, and stays until it's purged.
        kept = replace(removed, object_id=None) if object_id == "a" else removed
        assert replica.record(removed.local_key) == kept
        assert replica.purge(replica.feed_head()) == 1
        # What was sent, at that id, is the create, whether it's in flight or applied.
        _sync(replica, _page("T3", "T4", {object_id: _changed("fn", "v4")}))
        assert _summary(replica.record(key)) == _SYNCED

    def test_each_push_is_claimed_and_settled_once(self, make_store, clock):
        first = _seeded(Replica(make_store(), clock=clock))
        keys = [_edit_locally(first, "change", object_id)[0] for object_id in "ab"]
        keys += [_create(first, f"n{i}", object_id=f"n{i}") for i in range(6)]
        head = first.feed_head()
        handles = [Replica(make_store(), clock=clock) for _ in range(8)]

        def push_all(session: Session) -> list[int]:
            pushed = []
            for key in keys:
                claimed = session.claim(key)
                if claimed is not None:
                    assert session.settle(claimed, Applied())
                    pushed.append(key)
            return pushed

        with ExitStack() as stack:
            sessions = [stack.enter_context(_open_session(handle)) for handle in handles]
            # Every handle ingests first, since an ingest settles pushes in flight.
            for n, session in enumerate(sessions, 1):
                session.ingest(_page(f"T{n}", f"T{n + 1}", {}))
            with ThreadPoolExecutor(len(sessions)) as pool:
                pushed = [
                    key for keys_pushed in pool.map(push_all, sessions) for key in keys_pushed
                ]
        assert sorted(pushed) == sorted(keys)
        assert {_summary(first.record(key)) for key in keys} == {_SYNCED}
        entries = first.read_feed(head)
        assert Counter(e.local_key for e in entries) == Counter(keys)
        assert {e.kind for e in entries} == {FeedEntryKind.PUSHED}

    @pytest.mark.parametrize("following, outcome, settled, expected, feed", _SETTLED_LATE)
    def test_a_late_outcome_counts_only_for_the_same_edit(
        self, make_store, clock, following, outcome, settled, expected, feed
    ):
        pusher = _seeded(Replica(make_store(), clock=clock))
        syncer = Replica(make_store(), clock=clock)
        key, _ = _edit_locally(pusher, "change")
        with _pushing(pusher) as session:
            claimed = _claim(session, key)
            # Another process syncs while the server's answer is on its way.
            reported = {} if following in _NOTHING_NEW else {"a": _remote(following, "v3")}
            _sync(syncer, _page("T2", "T3", reported))
            if following == "edited":
                _change(syncer, key, "fy")
            if following == "claimed again":
                _push(syncer, key, None, "T3", "T4")
            head = pusher.feed_head()
            assert session.settle(claimed, outcome) is settled
        assert _summary(pusher.record(key)) == expected
        assert [kind for kind, _ in _feed(pusher, head)] == feed

    @pytest.mark.parametrize("local, remote, expected, feed", _LEFT_IN_FLIGHT)
    def test_the_next_complete_ingest_settles_a_push_left_in_flight(
        self, replica, local, remote, expected, feed
    ):
        key, object_id = _seeded_edit(replica, local)
        _push(replica, key, None)
        head = replica.feed_head()
        reported = {} if remote is None else {object_id: _remote(remote, "v3")}
        # A unique key changes nothing for a push that has an id.
        with _open_session(replica, unique_logical_key=True) as session:
            session.ingest(_page("T2", "C", {}, truncated=True))
            assert _summary(replica.record(key)) == (RecordState.IN_FLIGHT, EditKind(local), None)
            session.ingest(_page("C", "T3", reported))
        _assert_ended(replica, key, head, expected, feed)

    def test_a_listing_settles_pushes_it_never_mentions(self, replica):
        _seeded(replica)
        keys = {
            "change": _edit_locally(replica, "change")[0],
            "delete": _edit_locally(replica, "delete", "b")[0],
            "create": _edit_locally(replica, "create")[0],
            "create without an id": _create(replica, "fm"),
        }
        with _pushing(replica) as session:
            for key in keys.values():
                session.claim(key)
        head = replica.feed_head()
        _sync(replica, _page("T2", "L", {}, full=True))
        assert {name: _summary(replica.record(key)) for name, key in keys.items()} == {
            "change": _conflicted(EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY),
            "delete": _REMOVED,
            "create": _pending(EditKind.CREATE),
            "create without an id": _pending(EditKind.CREATE),
        }
        assert [kind for kind, _ in _feed(replica, head)] == [
            FeedEntryKind.CONFLICT_OPENED,
            FeedEntryKind.PUSHED,
            FeedEntryKind.PUSH_FAILED,
            FeedEntryKind.PUSH_FAILED,
        ]

    @pytest.mark.parametrize(
        "unique, creates, fingerprint",
        [(True, 1, "fl"), (True, 1, "fx"), (False, 1, "fl"), (True, 2, "fl")],
    )
    def test_a_create_without_an_id_is_known_by_a_unique_key(
        self, replica, unique, creates, fingerprint
    ):
        _seeded(replica)
        keys = [_create(replica, "fl", logical_key="u") for _ in range(creates)]
        with _pushing(replica) as session:
            for key in keys:
                session.claim(key)
        head = replica.feed_head()
        with _open_session(replica, unique_logical_key=unique) as session:
            session.ingest(_page("T2", "T3", {"new": _changed(fingerprint, "v3", logical_key="u")}))
        new = replica.find_record(_SCOPE, "new")
        feed = [kind for kind, _ in _feed(replica, head)]
        # Only a key that one create holds, in a scope where keys are unique,
        # tells which create the new object is, and only if it holds what was
        # sent; otherwise it's someone else's object, and the create goes out again.
        if unique and creates == 1 and fingerprint == "fl":
            assert (new.local_key, _summary(new)) == (keys[0], _SYNCED)
            assert feed == [FeedEntryKind.PUSHED]
        else:
            assert new.local_key not in keys
            assert [_summary(replica.record(key)) for key in keys] == [
                _pending(EditKind.CREATE)
            ] * creates
            assert feed == [FeedEntryKind.CHANGED] + [FeedEntryKind.PUSH_FAILED] * creates

    @pytest.mark.parametrize(
        "changes, expected",
        [
            ({}, _pending(EditKind.CHANGE)),
            ({"b": _changed("fx", "v3")}, _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED)),
        ],
    )
    def test_a_scope_wide_rejection_is_pushed_again(self, replica, changes, expected):
        _seeded(replica)
        keys = [_edit_locally(replica, "change", object_id)[0] for object_id in "ab"]
        with _pushing(replica) as session:
            for key in keys:
                session.settle(_claim(session, key), Rejected())
        # Only an object the server changed since is a conflict; the rest go out again.
        with _open_session(replica) as session:
            session.ingest(_page("T2", "T3", changes))
            to_push = [r.local_key for r in session.to_push()]
        assert [_summary(replica.record(key)) for key in keys] == [
            _pending(EditKind.CHANGE),
            expected,
        ]
        assert to_push == [key for key in keys if replica.record(key).state is RecordState.PENDING]


class TestResolutions:
    @pytest.mark.parametrize("kind, refused, resolution, expected", _RESOLVED)
    def test_each_resolution_of_each_conflict(self, replica, kind, refused, resolution, expected):
        key = _in_conflict(_seeded(replica), kind=kind, refused=refused)
        before = replica.record(key)
        conflict = before.conflict
        # The server's copy: what its side of the conflict holds, or, for a
        # change it refused, the synced copy it kept.
        refused_change = kind is ConflictKind.LOGICAL_KEY_TAKEN and refused == "change"
        server = (
            (before.version, before.content)
            if refused_change
            else (conflict.remote_version, conflict.remote_content)
        )
        head = replica.feed_head()
        replica.resolve(replica.record(key), resolution)
        record = _assert_ended(replica, key, head, expected, [FeedEntryKind.CONFLICT_RESOLVED])
        if record is not None and record.state is not RecordState.REMOVED:
            # Whatever is pushed next goes out against the server's current copy.
            assert (record.version, record.content) == server
        if isinstance(resolution, Merge):
            assert record is not None
            assert (record.edit and record.edit.content) == resolution.content
        if isinstance(resolution, UpdateHolder):
            # The holder, b, takes the refused edit's content as a change.
            holder = replica.find_record(_SCOPE, "b")
            assert holder.edit.content == before.edit.content

    @pytest.mark.parametrize(
        "kind, resolution, message",
        [
            *((kind, UpdateHolder(), "^Only an edit refused") for kind in _OPENED_BY),
            (ConflictKind.LOGICAL_KEY_TAKEN, KeepLocal(), "can't be pushed again unchanged$"),
            (None, KeepServer(), "takes a record in conflict$"),
        ],
    )
    def test_a_resolution_that_does_not_fit_is_refused(
        self, replica, store, kind, resolution, message
    ):
        _seeded(replica)
        key = _key(replica) if kind is None else _in_conflict(replica, kind=kind)
        record = replica.record(key)
        _assert_refused(
            replica,
            store,
            lambda: replica.resolve(record, resolution),
            ValueError,
            message,
            before_a_transaction=True,
        )

    def test_a_merge_the_server_already_holds_is_synced(self, replica):
        key = _in_conflict(_seeded(replica))
        replica.resolve(replica.record(key), Merge(_content("fx"), EditFields("fx")))
        assert _summary(replica.record(key)) == _SYNCED

    @pytest.mark.parametrize("following", ["fx", "fy", "removed", "fl"])
    def test_a_conflict_that_moved_on_is_stale(self, replica, store, following):
        key = _in_conflict(_seeded(replica))
        read = replica.record(key)
        _sync(replica, _page("T2", "T3", {"a": _remote(following, "v3")}))
        if following == "fx":
            # Only the version moved, and the edit goes out against the new one.
            replica.resolve(read, KeepLocal())
            record = replica.record(key)
            assert (record.state, record.version) == (RecordState.PENDING, "v3")
        else:
            _assert_refused(
                replica, store, lambda: replica.resolve(read, KeepLocal()), StaleConflictError
            )

    @pytest.mark.parametrize(
        "holder, keyed, logical_key",
        [
            ("b", [], "u"),
            ("b", [], None),
            (None, ["b"], "u"),
            ("z", ["b"], "u"),
            (None, [], "u"),
            (None, ["a", "b"], "u"),
            # The create had no key, so only an object the server named can hold it.
            (None, [], None),
        ],
    )
    def test_a_refused_create_goes_to_the_key_holder(
        self, replica, store, holder, keyed, logical_key
    ):
        _seeded(replica)
        key = _refused_create(replica, holder, logical_key=logical_key)
        if logical_key is None:
            # Without a key, nothing but the holder the server named is left to pick.
            changes: dict[str, Change] = {o: Removed() for o in "ab" if o != holder}
            unsent = None
        else:
            # A create not sent yet holds the key too, but the server doesn't have it.
            changes = {}
            unsent = _create(replica, "fo", logical_key=logical_key)
        changes |= {o: _changed(f"f{o}", "v2", logical_key="u") for o in keyed}
        _sync(replica, _page("T2", "T3", changes))
        refused = replica.record(key)
        if holder is None and len(keyed) != 1:
            _assert_refused(
                replica, store, lambda: replica.resolve(refused, UpdateHolder()), UnknownHolderError
            )
            return
        replica.resolve(refused, UpdateHolder())
        assert replica.record(key) is None
        if unsent is not None:
            assert replica.record(unsent).edit.content == _content("fo")
        b = replica.find_record(_SCOPE, "b")
        assert _summary(b) == _pending(EditKind.CHANGE)
        assert (b.edit.content, b.edit.logical_key) == (_content("fl"), logical_key)

    @pytest.mark.parametrize(
        "holding, error",
        [
            ("conflict", OpenConflictError),
            ("in flight", PushInFlightError),
            ("an edit", PendingEditError),
        ],
    )
    def test_a_holder_that_takes_no_edit(self, replica, store, holding, error):
        key = _refused_create(_seeded(replica), "b")
        if holding == "conflict":
            _in_conflict(replica, "b", "T2", "T3")
        elif holding == "in flight":
            _push(replica, _edit_locally(replica, "change", "b")[0], None, "T2", "T3")
        else:
            _edit_locally(replica, "change", "b")
        refused = replica.record(key)
        _assert_refused(replica, store, lambda: replica.resolve(refused, UpdateHolder()), error)

    def test_resolve_conflicts_resolves_each_answer(self, replica):
        a, b = _two_conflicts(replica)
        # A conflict in another scope comes after them.
        _sync(replica, _page(None, "O1", {"a": _changed("fa")}, full=True, scope=_OTHER))
        other = replica.find_record(_OTHER, "a").local_key
        _change(replica, other, "fl")
        _sync(replica, _page("O1", "O2", {"a": _changed("fx", "v2")}, scope=_OTHER))
        seen = []

        def resolver(record: Record) -> KeepServer | None:
            seen.append(record.local_key)
            return None if record.local_key == a else KeepServer()

        assert replica.resolve_conflicts(resolver) == ResolvedConflicts((b, other), (a,))
        assert seen == [a, b, other]
        assert [_summary(replica.record(key)) for key in (a, b, other)] == [
            _conflicted(EditKind.CHANGE, ConflictKind.BOTH_CHANGED),
            _SYNCED,
            _SYNCED,
        ]
        assert replica.resolve_conflicts(lambda record: None) == ResolvedConflicts(left_open=(a,))

    def test_resolve_conflicts_skips_a_conflict_that_moved_on(self, replica):
        a, b = _two_conflicts(replica)

        def resolver(record: Record) -> KeepServer:
            if record.local_key == a:
                # A sync brings news about b while the application decides about a.
                _sync(replica, _page("T3", "T4", {"b": _changed("fy", "v4")}))
            return KeepServer()

        assert replica.resolve_conflicts(resolver) == ResolvedConflicts((a,), changed=(b,))
        assert replica.record(b).state is RecordState.CONFLICT
        assert replica.resolve_conflicts(lambda record: KeepServer()) == ResolvedConflicts((b,))
        assert replica.record(b).content == _content("fy")

    def test_an_error_from_the_resolver_keeps_earlier_resolutions(self, replica):
        a, b = _two_conflicts(replica)

        def resolver(record: Record) -> KeepServer:
            if record.local_key == b:
                raise RuntimeError("the application failed")
            return KeepServer()

        with pytest.raises(RuntimeError, match="the application failed"):
            replica.resolve_conflicts(resolver)
        assert [replica.record(key).state for key in (a, b)] == [
            RecordState.SYNCED,
            RecordState.CONFLICT,
        ]


class TestLocalCopy:
    def test_find_record(self, replica):
        _a_removed(replica)
        assert replica.find_record(_SCOPE, "a").state is RecordState.REMOVED
        assert replica.find_record(_SCOPE, "b").state is RecordState.SYNCED
        assert replica.find_record(_SCOPE, "z") is None

    def test_find_by_logical_key(self, replica):
        objects = {"a": _changed("fa", logical_key="u1"), "b": _changed("fb", logical_key="u2")}
        _sync(replica, _page(None, "T1", objects, full=True))
        created = _create(replica, "fn", logical_key="u1")
        _sync(replica, _page("T1", "T2", {"a": Removed()}))
        found = replica.find_by_logical_key(_SCOPE, "u1")
        assert [r.local_key for r in found] == [_key(replica), created]
        assert replica.find_by_logical_key(_SCOPE, "u3") == []

    @pytest.mark.parametrize("content, text", list(zip(_CONTENTS, _TEXTS, strict=True)))
    def test_content_and_strings_round_trip_exactly(self, replica, content, text):
        _sync(replica, _page(None, "T1", {"a": _changed("fa", content=content)}, full=True))
        created = replica.create(_SCOPE, content, fingerprint="fn")
        _change(replica, _key(replica), "fl", containers=frozenset({text}), logical_key=text)
        record = replica.find_record(_SCOPE, "a")
        assert record.content == content
        assert (record.edit.containers, record.edit.logical_key) == (frozenset({text}), text)
        assert replica.record(created).edit.content == content

    def test_wrong_types_and_text_are_refused(self, replica, store, db_path):
        # The values an adapter or the application builds check themselves.
        built: list[tuple[Callable[[], object], type[Exception], str]] = [
            (
                lambda: Changed("v1", _BAD_TEXT, b"c"),
                LoneSurrogateError,
                "^fingerprint holds a lone surrogate at 1",
            ),
            (lambda: Changed("v1", "fa", _NOT_BYTES), TypeError, "^content must be bytes, not str"),
            (
                lambda: Changed(_wrong(1), "fa", b"c"),
                TypeError,
                "^version must be str or None, not int",
            ),
            (
                lambda: Changed("v1", _wrong(None), b"c"),
                TypeError,
                "^fingerprint must be str, not NoneType",
            ),
            (
                lambda: Changed("v1", "fa", b"c", _wrong("cal")),
                TypeError,
                "^containers must be frozenset of str, not str",
            ),
            (
                lambda: Changed("v1", "fa", b"c", frozenset({_wrong(1)})),
                TypeError,
                "^containers must be frozenset of str, but holds int",
            ),
            (
                lambda: ChangeSet(_wrong(1), None, "T1", {}),
                TypeError,
                "^scope must be str, not int",
            ),
            (
                lambda: ChangeSet(_SCOPE, None, "T1", _wrong([])),
                TypeError,
                "^changes must be mapping of str to Changed, Removed or Unchanged, not list",
            ),
            (lambda: _page(None, "T1", {_BAD_TEXT: Removed()}), LoneSurrogateError, "^object_ids "),
            (
                lambda: _page("T1", "T2", {"a": Unchanged()}),
                ValueError,
                "^Unchanged object 'a' outside a full listing$",
            ),
            (
                lambda: _page(None, "T1", {"a": _wrong("removed")}),
                TypeError,
                r"^changes\['a'\] must be Changed, Removed or Unchanged, not str",
            ),
            (lambda: Applied(_BAD_TEXT), LoneSurrogateError, "^object_id "),
            (lambda: Applied(_wrong(1)), TypeError, "^object_id must be str or None, not int"),
            (lambda: LogicalKeyTaken(_BAD_TEXT), LoneSurrogateError, "^holder "),
            (lambda: Merge(b"c", EditFields(_BAD_TEXT)), LoneSurrogateError, "^fingerprint "),
            (lambda: EditFields(_wrong(1)), TypeError, "^fingerprint must be str, not int"),
            (lambda: Merge(_NOT_BYTES, EditFields("f")), TypeError, "^content "),
            (
                lambda: SQLiteStore(db_path, timeout=_wrong("5")),
                TypeError,
                "^timeout must be float",
            ),
            (lambda: Replica(store, clock=_wrong(5)), TypeError, "^clock must be callable or None"),
        ]
        for build, error, match in built:
            with pytest.raises(error, match=match):
                build()
        # Calls on the replica and the store refuse them on every store, before a transaction.
        _seeded(replica)
        key = _key(replica)
        synced = replica.record(key)
        refused: list[tuple[Callable[[], object], type[Exception], str]] = [
            (lambda: _enter(_open_session(replica, _BAD_TEXT)), LoneSurrogateError, "^scope_id "),
            (lambda: _enter(_open_session(replica, _wrong(1))), TypeError, "^scope_id must be str"),
            (
                lambda: replica.create(
                    _SCOPE, b"c", fingerprint="f", containers=frozenset({_BAD_TEXT})
                ),
                LoneSurrogateError,
                "^containers ",
            ),
            (
                lambda: _change(replica, key, "fl", logical_key=_BAD_TEXT),
                LoneSurrogateError,
                "^logical_key ",
            ),
            (lambda: replica.create(_SCOPE, _NOT_BYTES, fingerprint="f"), TypeError, "^content "),
            (lambda: replica.change(key, _NOT_BYTES, fingerprint="f"), TypeError, "^content "),
            (lambda: replica.record(_wrong("1")), TypeError, "^local_key must be int, not str"),
            (lambda: replica.record(_wrong(True)), TypeError, "^local_key must be int, not bool"),
            (lambda: replica.read_feed(_wrong("0")), TypeError, "^after must be int, not str"),
            (
                lambda: store.delete_record(_wrong("1")),
                TypeError,
                "^local_key must be int, not str",
            ),
            (
                lambda: store.insert_record(replace(synced, containers=_wrong("cal"))),
                TypeError,
                "^record.containers must be frozenset of str, not str",
            ),
            (
                lambda: store.update_record(
                    replace(
                        synced,
                        state=RecordState.PENDING,
                        edit=replace(_EDIT, containers=frozenset({_wrong(1)})),
                    )
                ),
                TypeError,
                "^record.edit.containers must be frozenset of str, but holds int",
            ),
            (
                lambda: store.put_scope(replace(_new_scope(), token=_wrong(5))),
                TypeError,
                "^scope.token must be str or None, not int",
            ),
            (lambda: _enter(store.transaction(write=_wrong(1))), TypeError, "^write must be bool"),
        ]
        for call, error, match in refused:
            _assert_refused(replica, store, call, error, match, before_a_transaction=True)


class TestFeed:
    def test_entries_and_head(self, replica):
        assert replica.feed_head() == 0
        assert replica.read_feed() == []
        _seeded(replica)
        entries = replica.read_feed()
        assert [e.number for e in entries] == [1, 2]
        assert entries[0].kind is FeedEntryKind.CHANGED and entries[0].scope == _SCOPE
        assert entries[0].local_key == replica.records(_SCOPE)[0].local_key
        assert replica.feed_head() == 2

    def test_paging(self, replica):
        _sync(replica, _page(None, "T1", {f"o{i}": _changed(f"f{i}") for i in range(5)}, full=True))
        assert [e.number for e in replica.read_feed(0, limit=2)] == [1, 2]
        assert [e.number for e in replica.read_feed(2, limit=2)] == [3, 4]
        assert [e.number for e in replica.read_feed(5)] == []

    # A limit out of range is refused before the feed is read; a cursor needs the feed.
    @pytest.mark.parametrize(
        "after, limit, error",
        [
            (-1, 10, FeedRangeError),
            (3, 10, FeedRangeError),
            (0, 0, ValueError),
            (0, FEED_PAGE_SIZE + 1, ValueError),
        ],
    )
    def test_bad_read_arguments(self, replica, store, after, limit, error):
        _seeded(replica)
        _assert_refused(
            replica,
            store,
            lambda: replica.read_feed(after, limit),
            error,
            before_a_transaction=error is ValueError,
        )

    def test_trim_and_trimmed_cursor(self, replica, store):
        _a_removed(replica)
        replica.trim_feed(2)
        assert [e.number for e in replica.read_feed(2)] == [3]
        _assert_refused(replica, store, lambda: replica.read_feed(1), TrimmedFeedError)
        replica.trim_feed(1)
        _assert_refused(replica, store, lambda: replica.read_feed(0), TrimmedFeedError)

    @pytest.mark.parametrize("method", ["trim_feed", "purge"])
    @pytest.mark.parametrize("through", [-1, 3])
    def test_bad_through(self, replica, store, method, through):
        _seeded(replica)
        call = getattr(replica, method)
        _assert_refused(replica, store, lambda: call(through), FeedRangeError)

    def test_purge(self, replica):
        _a_removed(replica)
        _sync(replica, _page("T2", "T3", {"b": Removed()}))
        assert replica.purge(3) == 1
        assert _states(replica) == {"b": RecordState.REMOVED}
        assert _feed(replica, 2) == [(FeedEntryKind.REMOVED, "a"), (FeedEntryKind.REMOVED, "b")]
        assert replica.purge(4) == 1
        assert replica.records(_SCOPE) == []

    def test_local_keys_are_never_reused(self, replica):
        _seeded(replica)
        keys = {r.local_key for r in replica.records(_SCOPE)}
        _sync(replica, _page("T1", "T2", {"a": Removed(), "b": Removed()}))
        replica.purge(replica.feed_head())
        _sync(replica, _page("T2", "T3", {"a": _changed("fa")}))
        assert replica.records(_SCOPE)[0].local_key not in keys

    def test_default_clock_is_utc_now(self, store):
        _seeded(Replica(store))
        at = Replica(store).read_feed()[0].at
        assert at.tzinfo is UTC
        assert abs(datetime.now(UTC) - at) < timedelta(minutes=1)


class TestStores:
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

    @pytest.mark.parametrize("call", _READS.values(), ids=_READS)
    def test_reads_run_in_a_read_transaction(self, store, call):
        with store.transaction(write=False):
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

    @pytest.mark.parametrize(
        "open_store, beside",
        [
            (lambda path: MemoryStore(), True),
            (lambda path: SQLiteStore(path), True),
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

    def test_close_waits_for_open_transactions(self, store):
        with ThreadPoolExecutor(1) as pool, _held_open(store, write=False):
            closing = pool.submit(store.close)
            with pytest.raises(TimeoutError):
                closing.result(_SHORT_TIMEOUT)
        closing.result()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction(write=False))

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

    def test_a_closed_store_refuses_transactions(self, store):
        store.close()
        store.close()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction(write=False))

    def test_closing_inside_a_transaction_is_refused(self, store):
        with _write_with_scope(store):
            with pytest.raises(TransactionError, match="after its transaction"):
                store.close()
        assert _scope_ids(store) == [_SCOPE]

    def test_object_ids_are_unique_per_scope(self, replica, store):
        _seeded(replica)
        a, b = replica.records(_SCOPE)
        with store.transaction(), pytest.raises(DuplicateObjectError):
            store.insert_record(_local_record(RecordState.SYNCED, object_id="a"))
        with store.transaction(), pytest.raises(DuplicateObjectError):
            store.update_record(_local_record(b.state, object_id="a", local_key=b.local_key))

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
    def test_scope_records_by_state(self, replica, store, states, expected):
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

    def test_inserting_into_a_missing_scope_raises(self, store):
        with store.transaction(), pytest.raises(UnknownScopeError):
            store.insert_record(_local_record(RecordState.SYNCED))

    def test_updating_a_missing_record_raises(self, store):
        with store.transaction(), pytest.raises(UnknownRecordError):
            store.update_record(_local_record(RecordState.SYNCED, local_key=999))

    @pytest.mark.parametrize("write", [True, False], ids=["write", "read"])
    @pytest.mark.parametrize("step", ["begin", "commit", "scopes"])
    def test_a_failure_inside_the_store_is_unexpected_and_keeps_nothing(
        self, store, monkeypatch, step, write
    ):
        bug = RuntimeError(f"{step} has a bug")
        _break(store, monkeypatch, step, bug)
        with pytest.raises(UnexpectedStoreError, match=re.escape(ISSUES_URL)) as raised:
            with _write_with_scope(store) if write else store.transaction(write=False):
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

    def test_store_as_context_manager(self, make_store):
        with make_store() as store:
            assert _scope_ids(store) == []

    def test_a_memory_store_lets_go_of_old_data(self):
        store = MemoryStore()
        with ThreadPoolExecutor(1) as pool:
            # The thread stays alive, idle, after its read.
            pool.submit(_scope_ids, store).result()
            old = weakref.ref(cast(_MemoryStorage, store._storage)._committed)
            _add_scope(store)
            gc.collect()
            assert old() is None

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
        written = self._path.with_suffix(".new")
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


class TestSQLiteStore:
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

    def test_other_sqlite_errors_become_store_error(self, db_path):
        with SQLiteStore(db_path) as store:
            _edit_file(db_path, f"DROP TABLE {_FEED}", ())
            with store.transaction(write=False), pytest.raises(StoreError, match=_FEED):
                store.entries(0, 1)

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

    def test_state_survives_reopening(self, db_path, clock):
        with SQLiteStore(db_path) as store:
            replica = _edited(Replica(store, clock=clock))
            created = _create(
                replica,
                "fn",
                containers=frozenset({"x", "y"}),
                logical_key="u",
            )
            # The edit and server side that store no content, a delete and a
            # removal, and a record the server removed.
            changes = {"c": _changed("fc"), "d": _changed("fd"), "b": Removed()}
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

    def test_interrupted_ingest_leaves_the_token_after_reopening(self, db_path, clock):
        with SQLiteStore(db_path) as store:
            replica = _seeded(Replica(store, clock=clock))
            with pytest.raises(RuntimeError):
                with _open_session(replica) as session:
                    session.ingest(_page("T1", "L", {"a": Unchanged()}, full=True, truncated=True))
                    raise RuntimeError("process stops")
        with SQLiteStore(db_path) as store:
            replica = Replica(store, clock=clock)
            assert replica.scope(_SCOPE).token == "T1"
            assert _states(replica) == {"a": RecordState.SYNCED, "b": RecordState.SYNCED}

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

    def test_schema_created_by_another_process_first(self, db_path, monkeypatch):
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

    def test_newer_schema_is_refused(self, db_path):
        SQLiteStore(db_path).close()
        _edit_file(db_path, f"UPDATE {_META} SET schema_version = ?", (SCHEMA_VERSION + 1,))
        with pytest.raises(StoreVersionError):
            SQLiteStore(db_path)

    def test_rollback_after_the_transaction_already_ended(self, db_path):
        with SQLiteStore(db_path) as store:
            with pytest.raises(OSError), store.transaction():
                cast(_SQLiteTransaction, _current(store))._conn.execute("COMMIT")
                raise OSError("after an implicit end")
