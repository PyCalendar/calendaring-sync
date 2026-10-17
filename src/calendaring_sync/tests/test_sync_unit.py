# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for the calendaring_sync package.

Rule: zero network calls, zero Docker dependency, all tests are fast.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import calendaring_sync
from calendaring_sync import (
    Change,
    Changed,
    ChangeSet,
    DuplicateObjectError,
    FeedEntryKind,
    MemoryStore,
    Record,
    RecordState,
    Removed,
    Replica,
    Scope,
    ScopeMismatchError,
    Session,
    SessionError,
    SessionState,
    SQLiteStore,
    StaleChangeSetError,
    StateStore,
    StoreBusyError,
    StoreError,
    StoreVersionError,
    TransactionError,
    TransactionPermissionError,
    TransactionRequiredError,
    TrimmedFeedError,
    Unchanged,
    UnexpectedStoreError,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.constants import FEED_PAGE_SIZE, ISSUES_URL, SCHEMA_VERSION
from calendaring_sync.store.sqlite import _FEED, _META, _RECORDS

_SCOPE = "cal"
_OTHER = "other"
_DB_FILE = "s.db"
_SCHEME = "scheme-1"
_OTHER_SCHEME = "scheme-2"
_MEDIA = "text/plain"
_START = datetime(2026, 10, 1, tzinfo=UTC)
# How long a test store waits for another, where the wait is meant to run out.
_SHORT_TIMEOUT = 0.1


class _Clock:
    """A clock that moves one second each time it's read."""

    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture(params=[MemoryStore, SQLiteStore], ids=lambda cls: cls.__name__)
def make_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[Callable[[], StateStore]]:
    """Open handles on one store, as separate processes would: one shared
    ``MemoryStore``, or a new connection to one SQLite file each time."""
    opened: list[StateStore] = []
    memory = MemoryStore()

    def make() -> StateStore:
        store: StateStore = (
            memory if request.param is MemoryStore else SQLiteStore(tmp_path / _DB_FILE)
        )
        opened.append(store)
        return store

    yield make
    for store in opened:
        store.close()


@pytest.fixture
def store(make_store: Callable[[], StateStore]) -> StateStore:
    return make_store()


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
    content: str | None = None,
    containers: frozenset[str] = frozenset(),
    logical_key: str | None = None,
) -> Changed:
    return Changed(
        version, fingerprint, content or f"text of {fingerprint}", containers, logical_key
    )


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
    replica: Replica, scope: str = _SCOPE, *, scheme: str = _SCHEME, media: str = _MEDIA
) -> AbstractContextManager[Session]:
    return replica.session(scope, fingerprint_scheme=scheme, media_type=media)


def _enter(context: AbstractContextManager[object]) -> None:
    """Enter and leave a context, for tests about what happens on entry."""
    with ExitStack() as stack:
        stack.enter_context(context)


def _sync(replica: Replica, *pages: ChangeSet) -> SessionState:
    with _open_session(replica) as session:
        for change_set in pages:
            session.ingest(change_set)
        return session.state


def _states(replica: Replica) -> dict[str | None, RecordState]:
    return {r.object_id: r.state for r in replica.records(_SCOPE)}


def _feed(replica: Replica, after: int = 0) -> list[tuple[FeedEntryKind, str | None]]:
    return [(e.kind, e.object_id) for e in replica.read_feed(after)]


def _snapshot(replica: Replica) -> tuple[object, ...]:
    """Everything a replica holds, to compare before and after."""
    return (replica.scopes(), replica.records(_SCOPE), replica.read_feed(), replica.feed_head())


def _new_scope() -> Scope:
    return Scope(_SCOPE, None, _SCHEME, _MEDIA)


@contextmanager
def _busy_pair(tmp_path: Path) -> Iterator[tuple[SQLiteStore, SQLiteStore]]:
    """Two stores on one file; the second gives up quickly when the first holds it."""
    path = tmp_path / _DB_FILE
    with SQLiteStore(path) as holder, SQLiteStore(path, timeout=_SHORT_TIMEOUT) as waiter:
        yield holder, waiter


def _edit_file(path: Path, sql: str, params: tuple[object, ...]) -> None:
    """Change a store's file directly, as a damaged or newer file would look."""
    conn = sqlite3.connect(path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _local_record(state: RecordState, object_id: str | None = None, local_key: int = 0) -> Record:
    """A record written straight to a store, as later local edits will be."""
    return Record(local_key, _SCOPE, object_id, state, None, "f", "c", frozenset(), None, None, 0)


# Every store operation that only reads, called so it works on an empty store.
_READS: dict[str, Callable[[StateStore], object]] = {
    "get_scope": lambda store: store.get_scope(_SCOPE),
    "scopes": lambda store: store.scopes(),
    "get_record": lambda store: store.get_record(1),
    "find_record": lambda store: store.find_record(_SCOPE, "a"),
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


def _a_removed(replica: Replica) -> Replica:
    """A seeded scope at token T2, where a was removed."""
    _sync(_seeded(replica), _page("T1", "T2", {"a": Removed()}))
    return replica


class TestPackage:
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
            "text of fa",
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
            _page("T1", "T2", {"a": _changed("fa", "v2", content="rewritten by the server")}),
        )
        after = replica.record(before.local_key)
        assert after is not None
        assert (after.version, after.content, after.last_entry) == (
            "v2",
            "rewritten by the server",
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

    def test_content_round_trips_exactly(self, replica):
        text = "BEGIN:X\r\nSUMMARY:café ☃\r\n line folded\r\nEND:X\r\n"
        _sync(replica, _page(None, "T1", {"a": _changed("fa", content=text)}, full=True))
        assert replica.records(_SCOPE)[0].content == text

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

    def test_stale_change_set_is_refused(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "T2", {}))
        with pytest.raises(StaleChangeSetError):
            _sync(replica, _page("T1", "T3", {"a": _changed("x")}))
        assert replica.scope(_SCOPE).token == "T2"

    def test_second_handle_gets_the_stale_error(self, make_store, clock):
        first = _seeded(Replica(make_store(), clock=clock))
        second = Replica(make_store(), clock=clock)
        _sync(first, _page("T1", "T2", {"a": _changed("fa2")}))
        with pytest.raises(StaleChangeSetError):
            _sync(second, _page("T1", "T2b", {"b": _changed("fb2")}))
        assert second.scope(_SCOPE).token == "T2"

    def test_incremental_without_token_is_refused(self, replica):
        with pytest.raises(ValueError, match="needs a full listing"):
            _sync(replica, _page(None, "T1", {"a": _changed("fa")}))

    def test_unchanged_outside_listing_is_refused(self, replica):
        _seeded(replica)
        with pytest.raises(ValueError, match="outside a full listing"):
            _sync(replica, _page("T1", "T2", {"a": Unchanged()}))

    @pytest.mark.parametrize("object_id", ["zz", "a"])
    def test_unchanged_needs_a_live_record_with_a_version(self, replica, object_id):
        _a_removed(replica)
        with pytest.raises(ValueError, match="no version"):
            _sync(replica, _page("T2", "L", {object_id: Unchanged()}, full=True))

    def test_change_set_for_another_scope_is_refused(self, replica):
        with pytest.raises(ValueError, match="in a session for"):
            with _open_session(replica) as session:
                session.ingest(_page(None, "T1", {}, full=True, scope=_OTHER))

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
        before = _snapshot(replica)
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
        assert _snapshot(replica) == before


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

    def test_listing_leaves_records_without_object_id(self, replica, store):
        _seeded(replica)
        with store.transaction():
            store.insert_record(_local_record(RecordState.PENDING))
            store.insert_record(_local_record(RecordState.PENDING))
        _sync(replica, _page("T1", "L", {"a": Unchanged()}, full=True))
        assert [r.state for r in replica.records(_SCOPE) if r.object_id is None] == [
            RecordState.PENDING
        ] * 2
        assert _states(replica)["b"] is RecordState.REMOVED

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

    def test_pages_of_one_listing_carry_one_token(self, replica):
        with pytest.raises(StaleChangeSetError, match="Page of listing"):
            _sync(
                replica,
                _page(None, "L1", {}, full=True, truncated=True),
                _page(None, "L2", {}, full=True),
            )

    def test_listing_against_the_current_token(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "L", {"a": Unchanged(), "b": _changed("fb2", "v2")}, full=True))
        assert replica.scope(_SCOPE).token == "L"
        assert replica.known_versions(_SCOPE) == {"a": "v1", "b": "v2"}

    def test_stale_page_lets_the_same_session_start_over(self, replica):
        _seeded(replica)
        with _open_session(replica) as session:
            session.ingest(_page("T1", "L1", {"a": Unchanged()}, full=True, truncated=True))
            with pytest.raises(StaleChangeSetError):
                session.ingest(_page("T1", "L-other", {}, full=True))
            assert session.state is SessionState.IDLE
            session.ingest(_page("T1", "L2", {"a": Unchanged()}, full=True))
        assert replica.scope(_SCOPE).token == "L2"
        assert _states(replica)["b"] is RecordState.REMOVED

    def test_interleaved_listings_refuse_the_older_one(self, make_store, clock):
        a, b = Replica(make_store(), clock=clock), Replica(make_store(), clock=clock)
        with _open_session(a) as session_a:
            session_a.ingest(_page(None, "LA", {"x": _changed("fx")}, full=True, truncated=True))
            with _open_session(b) as session_b:
                session_b.ingest(
                    _page(None, "LB", {"y": _changed("fy")}, full=True, truncated=True)
                )
                with pytest.raises(StaleChangeSetError, match="is listing"):
                    session_a.ingest(_page(None, "LA", {}, full=True))
                session_b.ingest(_page(None, "LB", {"x": Unchanged()}, full=True))
        assert b.scope(_SCOPE).token == "LB"
        assert {r.object_id for r in b.records(_SCOPE) if r.state is RecordState.SYNCED} == {
            "x",
            "y",
        }


class TestSessions:
    def test_ingest_after_the_last_page_is_refused(self, replica):
        with pytest.raises(SessionError, match="last page"):
            _sync(replica, _page(None, "T1", {}, full=True), _page("T1", "T2", {}))

    def test_listing_and_incremental_pages_do_not_mix(self, replica):
        _seeded(replica)
        with pytest.raises(SessionError, match="all listing pages"):
            _sync(replica, _page("T1", "C1", {}, truncated=True), _page("C1", "L", {}, full=True))

    def test_ended_session_refuses_ingests(self, replica):
        with _open_session(replica) as session:
            session.ingest(_page(None, "L", {}, full=True, truncated=True))
        assert session.state is SessionState.IDLE
        with pytest.raises(SessionError, match="has ended"):
            session.ingest(_page(None, "L", {}, full=True))

    def test_one_open_session_per_scope(self, replica):
        with _open_session(replica) as session:
            assert session.scope_id == _SCOPE
            with pytest.raises(SessionError, match="already open"):
                _enter(_open_session(replica))
        _enter(_open_session(replica))

    @pytest.mark.parametrize("method", ["reset", "drop_scope"])
    def test_reset_and_drop_refused_while_a_session_is_open(self, replica, method):
        _seeded(replica)
        with _open_session(replica):
            with pytest.raises(SessionError, match="is open"):
                getattr(replica, method)(_SCOPE)

    def test_media_type_mismatch(self, replica):
        _seeded(replica)
        with pytest.raises(ScopeMismatchError):
            _enter(_open_session(replica, media="application/json"))
        _enter(_open_session(replica))

    def test_fingerprint_scheme_change_resets_and_clears_versions(self, replica):
        _seeded(replica)
        with _open_session(replica, scheme=_OTHER_SCHEME) as session:
            scope = replica.scope(_SCOPE)
            assert (scope.token, scope.fingerprint_scheme) == (None, _OTHER_SCHEME)
            assert {(r.version, r.fingerprint) for r in replica.records(_SCOPE)} == {(None, None)}
            assert replica.known_versions(_SCOPE) == {}
            with pytest.raises(ValueError, match="needs a full listing"):
                session.ingest(_page(None, "T2", {}))

    def test_listing_after_a_scheme_change_reports_only_new_text(self, replica):
        _seeded(replica)
        relisted = {
            "a": _changed("new-a", content="text of fa"),
            "b": _changed("new-b", content="b edited meanwhile"),
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
    def test_unknown_scope(self, replica):
        for call in (
            replica.scope,
            replica.records,
            replica.known_versions,
            replica.reset,
            replica.drop_scope,
        ):
            with pytest.raises(UnknownScopeError):
                call("nope")

    def test_two_scopes_stay_independent(self, replica):
        _a_removed(replica)
        with _open_session(replica, _OTHER) as session:
            session.ingest(_page(None, "O1", {"a": _changed("other-a")}, full=True, scope=_OTHER))
        assert [s.id for s in replica.scopes()] == [_SCOPE, _OTHER]
        assert replica.records(_OTHER)[0].state is RecordState.SYNCED
        assert replica.scope(_OTHER).token == "O1"
        replica.drop_scope(_SCOPE)
        assert [s.id for s in replica.scopes()] == [_OTHER]

    def test_drop_scope(self, replica, store):
        _seeded(replica)
        with store.transaction():
            for state in (RecordState.PENDING, RecordState.IN_FLIGHT, RecordState.CONFLICT):
                store.insert_record(_local_record(state))
        dropped = replica.drop_scope(_SCOPE)
        assert dropped.scope == _SCOPE
        assert [r.state for r in dropped.pending] == [RecordState.PENDING, RecordState.IN_FLIGHT]
        assert [r.state for r in dropped.conflicts] == [RecordState.CONFLICT]
        assert replica.scopes() == []
        assert _feed(replica, 2) == [(FeedEntryKind.SCOPE_DROPPED, None)]


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

    @pytest.mark.parametrize("after, limit", [(-1, 10), (3, 10), (0, 0), (0, FEED_PAGE_SIZE + 1)])
    def test_bad_read_arguments(self, replica, after, limit):
        _seeded(replica)
        with pytest.raises(ValueError):
            replica.read_feed(after, limit)

    def test_trim_and_trimmed_cursor(self, replica):
        _a_removed(replica)
        replica.trim_feed(2)
        assert [e.number for e in replica.read_feed(2)] == [3]
        with pytest.raises(TrimmedFeedError):
            replica.read_feed(1)
        replica.trim_feed(1)
        with pytest.raises(TrimmedFeedError):
            replica.read_feed(0)

    @pytest.mark.parametrize("method", ["trim_feed", "purge"])
    @pytest.mark.parametrize("through", [-1, 3])
    def test_bad_through(self, replica, method, through):
        _seeded(replica)
        with pytest.raises(ValueError):
            getattr(replica, method)(through)

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
        assert public - {"transaction", "close"} == _READS.keys() | _WRITES.keys()

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

    def test_transactions_do_not_nest(self, store):
        with store.transaction(), pytest.raises(TransactionError, match="already open"):
            _enter(store.transaction())

    def test_threads_share_one_store(self, replica):
        def write(n: int) -> None:
            scope = f"scope-{n}"
            objects = {f"o{i}": _changed(f"f{n}-{i}") for i in range(20)}
            with _open_session(replica, scope) as session:
                session.ingest(_page(None, "T1", objects, full=True, scope=scope))
            if n % 2:
                replica.drop_scope(scope)

        def read(_: int) -> None:
            for scope in replica.scopes():
                try:
                    replica.records(scope.id)
                except UnknownScopeError:
                    pass  # dropped by a writer between the two calls

        def work(n: int) -> None:
            write(n)
            read(n)

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(work, range(16)))
        assert [s.id for s in replica.scopes()] == sorted(f"scope-{n}" for n in range(0, 16, 2))
        assert all(len(replica.records(s.id)) == 20 for s in replica.scopes())
        assert [e.number for e in replica.read_feed()] == list(range(1, replica.feed_head() + 1))

    def test_a_closed_store_refuses_transactions(self, store):
        store.close()
        store.close()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction(write=False))

    def test_closing_inside_a_transaction_is_refused(self, store):
        with store.transaction():
            store.put_scope(_new_scope())
            with pytest.raises(TransactionError, match="after its transaction"):
                store.close()
        with store.transaction(write=False):
            assert [s.id for s in store.scopes()] == [_SCOPE]

    def test_object_ids_are_unique_per_scope(self, replica, store):
        _seeded(replica)
        a, b = replica.records(_SCOPE)
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

    @pytest.mark.parametrize("hook", ["_begin", "_commit", "_scopes"])
    def test_a_failure_inside_the_store_is_unexpected_and_keeps_nothing(
        self, store, monkeypatch, hook
    ):
        bug = RuntimeError(f"{hook} has a bug")

        def fail(*args: object, **kwargs: object) -> None:
            raise bug

        monkeypatch.setattr(store, hook, fail)
        with pytest.raises(UnexpectedStoreError, match=re.escape(ISSUES_URL)) as raised:
            with store.transaction():
                store.put_scope(_new_scope())
                store.scopes()
        assert raised.value.__cause__ is bug
        assert not isinstance(raised.value, StoreError)
        monkeypatch.undo()
        with store.transaction(write=False):
            assert store.scopes() == []

    def test_interrupts_pass_through_unchanged(self, store, monkeypatch):
        def interrupt() -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(store, "_scopes", interrupt)
        with pytest.raises(KeyboardInterrupt), store.transaction(write=False):
            store.scopes()
        with store.transaction(write=False):
            assert store.feed_bounds() == (0, 0)

    def test_store_as_context_manager(self, make_store):
        with make_store() as store:
            with store.transaction(write=False):
                assert store.scopes() == []


class TestSQLiteStore:
    def test_busy_file_raises_store_busy_error_and_stays_usable(self, tmp_path):
        with _busy_pair(tmp_path) as (holder, waiter):
            with holder.transaction(), pytest.raises(StoreBusyError):
                _enter(waiter.transaction())
            with waiter.transaction():
                waiter.put_scope(_new_scope())
            with holder.transaction(write=False):
                assert [s.id for s in holder.scopes()] == [_SCOPE]

    def test_ingest_while_busy_changes_nothing_and_can_be_retried(self, tmp_path, clock):
        with _busy_pair(tmp_path) as (holder, waiter):
            replica = Replica(waiter, clock=clock)
            first = _page(None, "T1", {"a": _changed("fa")}, full=True)
            with _open_session(replica) as session:
                with holder.transaction(), pytest.raises(StoreBusyError):
                    session.ingest(first)
                assert (session.state, replica.records(_SCOPE)) == (SessionState.IDLE, [])
                session.ingest(first)
            assert replica.scope(_SCOPE).token == "T1"

    def test_unreadable_files_raise_store_error(self, tmp_path):
        not_a_store = tmp_path / "junk.db"
        not_a_store.write_bytes(b"not a database" * 100)
        for path in (not_a_store, tmp_path / "missing" / _DB_FILE):
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
        ],
    )
    def test_damaged_values_raise_store_error(self, tmp_path, clock, column, damage):
        path = tmp_path / _DB_FILE
        with SQLiteStore(path) as store:
            _seeded(Replica(store, clock=clock))
        _edit_file(path, f"UPDATE {_RECORDS} SET {column} = ?", (damage,))
        with SQLiteStore(path) as store, pytest.raises(StoreError, match=column):
            Replica(store, clock=clock).records(_SCOPE)

    def test_other_sqlite_errors_become_store_error(self, tmp_path):
        path = tmp_path / _DB_FILE
        with SQLiteStore(path) as store:
            _edit_file(path, f"DROP TABLE {_FEED}", ())
            with store.transaction(write=False), pytest.raises(StoreError, match=_FEED):
                store.entries(0, 1)

    def test_a_lost_meta_row_raises_store_error(self, tmp_path, clock):
        path = tmp_path / _DB_FILE
        with SQLiteStore(path) as store:
            replica = Replica(store, clock=clock)
            _edit_file(path, f"DELETE FROM {_META}", ())
            with pytest.raises(StoreError, match="lost its row"):
                replica.feed_head()
            with pytest.raises(StoreError, match="lost its row"):
                _seeded(replica)
        with pytest.raises(StoreError, match="lost its row"):
            SQLiteStore(path)

    def test_a_full_disk_raises_store_error_and_keeps_nothing(self, tmp_path, clock):
        with SQLiteStore(tmp_path / _DB_FILE) as store:
            replica = _seeded(Replica(store, clock=clock))
            before = _snapshot(replica)
            # SQLite reports a file at its page limit as a full disk.
            pages = store._conn.execute("PRAGMA page_count").fetchone()[0]
            store._conn.execute(f"PRAGMA max_page_count = {pages}")
            many = {f"o{n}": _changed(f"f{n}", content="x" * 10_000) for n in range(20)}
            with pytest.raises(StoreError, match="full"):
                _sync(replica, _page("T1", "T2", many))
            assert _snapshot(replica) == before

    def test_a_failed_close_raises_store_error_and_still_closes(self, tmp_path):
        class FailingClose(SQLiteStore):
            def _close(self) -> None:
                super()._close()
                raise sqlite3.OperationalError("disk I/O error")

        store = FailingClose(tmp_path / _DB_FILE)
        with pytest.raises(StoreError, match="disk I/O"):
            store.close()
        store.close()
        with pytest.raises(StoreError, match="closed"):
            _enter(store.transaction())

    def test_state_survives_reopening(self, tmp_path, clock):
        path = tmp_path / _DB_FILE
        with SQLiteStore(path) as store:
            replica = _a_removed(Replica(store, clock=clock))
            expected = _snapshot(replica)
        with SQLiteStore(path) as store:
            assert _snapshot(Replica(store, clock=clock)) == expected

    def test_interrupted_ingest_leaves_the_token_after_reopening(self, tmp_path, clock):
        path = tmp_path / _DB_FILE
        with SQLiteStore(path) as store:
            replica = _seeded(Replica(store, clock=clock))
            with pytest.raises(RuntimeError):
                with _open_session(replica) as session:
                    session.ingest(_page("T1", "L", {"a": Unchanged()}, full=True, truncated=True))
                    raise RuntimeError("process stops")
        with SQLiteStore(path) as store:
            replica = Replica(store, clock=clock)
            assert replica.scope(_SCOPE).token == "T1"
            assert _states(replica) == {"a": RecordState.SYNCED, "b": RecordState.SYNCED}

    def test_schema_created_by_another_process_first(self, tmp_path, monkeypatch):
        path = tmp_path / _DB_FILE
        SQLiteStore(path).close()
        real = SQLiteStore._version
        looks: list[int] = []

        def version(store: SQLiteStore) -> int:
            # The first look sees an empty file; under the write lock the schema is there.
            looks.append(0)
            return 0 if len(looks) == 1 else real(store)

        monkeypatch.setattr(SQLiteStore, "_version", version)
        with SQLiteStore(path) as store, store.transaction(write=False):
            assert store.scopes() == []
        assert len(looks) == 3

    def test_newer_schema_is_refused(self, tmp_path):
        path = tmp_path / _DB_FILE
        SQLiteStore(path).close()
        _edit_file(path, f"UPDATE {_META} SET schema_version = ?", (SCHEMA_VERSION + 1,))
        with pytest.raises(StoreVersionError):
            SQLiteStore(path)

    def test_rollback_after_the_transaction_already_ended(self, tmp_path):
        with SQLiteStore(tmp_path / _DB_FILE) as store:
            with pytest.raises(OSError), store.transaction():
                store._conn.execute("COMMIT")
                raise OSError("after an implicit end")
