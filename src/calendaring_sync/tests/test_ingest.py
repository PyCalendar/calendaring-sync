# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ingesting change sets: incremental ones, full listings, sessions, and resets."""

from __future__ import annotations

import pytest

from calendaring_sync import (
    ConflictKind,
    EditKind,
    FeedEntryKind,
    ListingRequiredError,
    OtherScopeError,
    Record,
    RecordState,
    Removed,
    Replica,
    ScopeMismatchError,
    SessionError,
    SessionState,
    StaleChangeSetError,
    Unchanged,
    UnknownVersionError,
)
from calendaring_sync.tests._helpers import (
    _OTHER,
    _OTHER_SCHEME,
    _REMOVED,
    _SCOPE,
    _a_removed,
    _assert_refused,
    _change,
    _changed,
    _conflicted,
    _content,
    _create,
    _edited,
    _enter,
    _feed,
    _key,
    _open_session,
    _page,
    _pending,
    _seeded,
    _snapshot,
    _states,
    _summary,
    _sync,
)


class TestIngest:
    """Ingesting a change set: what it stores, refuses, and leaves after a failure."""

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

    def test_an_incremental_change_set_stores_changes_and_new_objects(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "T2", {"a": _changed("fa2", "v2"), "c": _changed("fc")}))
        records = {r.object_id: r for r in replica.records(_SCOPE)}
        assert records["a"].fingerprint == "fa2" and records["a"].version == "v2"
        assert records["c"].state is RecordState.SYNCED
        assert _feed(replica, 2) == [(FeedEntryKind.CHANGED, "a"), (FeedEntryKind.CHANGED, "c")]
        assert replica.scope(_SCOPE).token == "T2"

    def test_a_removal_marks_the_record_removed(self, replica):
        _a_removed(replica)
        assert _states(replica) == {"a": RecordState.REMOVED, "b": RecordState.SYNCED}
        assert _feed(replica, 2) == [(FeedEntryKind.REMOVED, "a")]

    def test_removed_record_comes_back_as_the_same_record(self, replica):
        _a_removed(replica)
        key = replica.records(_SCOPE)[0].local_key
        _sync(replica, _page("T2", "T3", {"a": _changed("fa")}))
        record = replica.record(key)
        assert record is not None and record.state is RecordState.SYNCED
        assert _feed(replica, 3) == [(FeedEntryKind.CHANGED, "a")]

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

    def test_removing_unknown_or_removed_object_does_nothing(self, replica):
        _a_removed(replica)
        _sync(replica, _page("T2", "T3", {"a": Removed(), "zz": Removed()}))
        assert _feed(replica, 3) == []
        assert "zz" not in _states(replica)

    def test_truncated_incremental_pages_reach_the_final_token(self, replica):
        _seeded(replica)
        with _open_session(replica) as session:
            session.ingest(_page("T1", "C1", {"a": _changed("fa2")}, truncated=True))
            states = [session.state]
            assert replica.scope(_SCOPE).token == "C1"
            session.ingest(_page("C1", "C2", {"b": _changed("fb2")}, truncated=True))
            session.ingest(_page("C2", "T2", {}))
            states.append(session.state)
            assert states == [SessionState.INGESTING, SessionState.INGESTED]
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
    """A full listing, in one page or several."""

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

    def test_a_listing_can_report_a_removal(self, replica):
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

    def test_pages_of_one_listing_carry_one_token(self, replica, store):
        with _open_session(replica) as session:
            session.ingest(_page(None, "L1", {}, full=True, truncated=True))
            page = _page(None, "L2", {}, full=True)
            _assert_refused(
                replica, store, lambda: session.ingest(page), StaleChangeSetError, "Page of listing"
            )

    def test_a_listing_runs_against_the_current_token(self, replica):
        _seeded(replica)
        _sync(replica, _page("T1", "L", {"a": Unchanged(), "b": _changed("fb2", "v2")}, full=True))
        assert replica.scope(_SCOPE).token == "L"
        assert replica.known_versions(_SCOPE) == {"a": "v1", "b": "v2"}

    def test_an_interrupted_listing_is_started_over(self, replica):
        _seeded(replica)
        replica.reset(_SCOPE)
        with pytest.raises(RuntimeError), _open_session(replica) as session:
            session.ingest(
                _page(None, "L1", {"a": Unchanged(), "b": Unchanged()}, full=True, truncated=True)
            )
            raise RuntimeError("connection lost")
        assert replica.scope(_SCOPE).token is None
        _sync(replica, _page(None, "L2", {"a": Unchanged()}, full=True))
        assert _states(replica) == {"a": RecordState.SYNCED, "b": RecordState.REMOVED}
        assert replica.scope(_SCOPE).token == "L2"

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
    """The order a session's calls must come in."""

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


class TestResets:
    """A reset, or a session opened with another fingerprint scheme."""

    # Synced records, and a pending edit beside an open conflict.
    @pytest.mark.parametrize("make", [_seeded, _edited], ids=["synced", "edited"])
    def test_a_reset_clears_only_the_token(self, replica, make):
        make(replica)
        before = replica.records(_SCOPE)
        replica.reset(_SCOPE)
        assert replica.scope(_SCOPE).token is None
        assert replica.records(_SCOPE) == before

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

    def test_another_media_type_is_refused(self, replica, store):
        _seeded(replica)
        other = _open_session(replica, media="application/json")
        _assert_refused(replica, store, lambda: _enter(other), ScopeMismatchError)
        _enter(_open_session(replica))
