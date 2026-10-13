# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for the calendaring_sync package.

Rule: zero network calls, zero Docker dependency, all tests are fast.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import calendaring_sync
from calendaring_sync import (
    Change,
    Changed,
    ChangeSet,
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
    StoreVersionError,
    TrimmedFeedError,
    Unchanged,
    UnknownScopeError,
)
from calendaring_sync.constants import FEED_PAGE_SIZE, SCHEMA_VERSION
from calendaring_sync.store.sqlite import _META

_SCOPE = "cal"
_OTHER = "other"
_DB_FILE = "s.db"
_SCHEME = "scheme-1"
_MEDIA = "text/plain"
_START = datetime(2026, 10, 1, tzinfo=UTC)


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


def _local_record(state: RecordState, object_id: str | None = None, local_key: int = 0) -> Record:
    """A record written straight to a store, as later local edits will be."""
    return Record(local_key, _SCOPE, object_id, state, None, "f", "c", frozenset(), None, None, 0)


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
        with _open_session(replica, scheme="scheme-2") as session:
            scope = replica.scope(_SCOPE)
            assert (scope.token, scope.fingerprint_scheme) == (None, "scheme-2")
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
        with _open_session(replica, scheme="scheme-2") as session:
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
    def test_operations_outside_a_transaction_raise(self, store):
        with pytest.raises(RuntimeError, match="inside store.transaction"):
            store.scopes()

    def test_transactions_do_not_nest(self, store):
        with store.transaction(), pytest.raises(RuntimeError, match="already open"):
            _enter(store.transaction())

    def test_object_ids_are_unique_per_scope(self, replica, store):
        _seeded(replica)
        a, b = replica.records(_SCOPE)
        with store.transaction(), pytest.raises(ValueError):
            store.insert_record(_local_record(RecordState.SYNCED, object_id="a"))
        with store.transaction(), pytest.raises(ValueError):
            store.update_record(_local_record(b.state, object_id="a", local_key=b.local_key))

    def test_inserting_into_a_missing_scope_raises(self, store):
        with store.transaction(), pytest.raises(ValueError):
            store.insert_record(_local_record(RecordState.SYNCED))

    def test_updating_a_missing_record_raises(self, store):
        with store.transaction(), pytest.raises(ValueError, match="No record"):
            store.update_record(_local_record(RecordState.SYNCED, local_key=999))

    def test_failed_commit_rolls_back(self):
        class FailingCommit(MemoryStore):
            def _commit(self) -> None:
                raise OSError("commit failed")

        store = FailingCommit()
        with pytest.raises(OSError):
            with store.transaction():
                store.put_scope(Scope("s", None, _SCHEME, _MEDIA))
        store._commit = lambda: None  # type: ignore[method-assign]
        with store.transaction():
            assert store.scopes() == []

    def test_store_as_context_manager(self, make_store):
        with make_store() as store:
            with store.transaction(write=False):
                assert store.scopes() == []


class TestSQLiteStore:
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
        conn = sqlite3.connect(path)
        conn.execute(f"UPDATE {_META} SET schema_version = ?", (SCHEMA_VERSION + 1,))
        conn.commit()
        conn.close()
        with pytest.raises(StoreVersionError):
            SQLiteStore(path)

    def test_rollback_after_the_transaction_already_ended(self, tmp_path):
        with SQLiteStore(tmp_path / _DB_FILE) as store:
            with pytest.raises(OSError), store.transaction():
                store._conn.execute("COMMIT")
                raise OSError("after an implicit end")

    def test_threads_share_one_store(self, tmp_path, clock):
        with SQLiteStore(tmp_path / _DB_FILE) as store:
            replica = Replica(store, clock=clock)

            def work(n: int) -> None:
                scope = f"scope-{n}"
                objects = {f"o{i}": _changed(f"f{n}-{i}") for i in range(20)}
                with _open_session(replica, scope) as session:
                    session.ingest(_page(None, "T1", objects, full=True, scope=scope))

            with ThreadPoolExecutor(8) as pool:
                list(pool.map(work, range(8)))
            assert len(replica.scopes()) == 8
            assert [e.number for e in replica.read_feed()] == list(range(1, 161))
