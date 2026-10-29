# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pushes, how each one ends, and resolving conflicts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from typing import Any

import pytest

from calendaring_sync import (
    Applied,
    Change,
    ConflictKind,
    Delete,
    DuplicateObjectError,
    EditFields,
    EditKind,
    FeedEntryKind,
    Gone,
    KeepLocal,
    KeepServer,
    LogicalKeyTaken,
    LoneSurrogateError,
    Merge,
    ObjectIdAssignedError,
    OpenConflictError,
    OtherScopeError,
    PendingEditError,
    PushInFlightError,
    PushOutcome,
    Record,
    RecordState,
    Rejected,
    Removed,
    Replica,
    ResolvedConflicts,
    Session,
    SessionError,
    SessionState,
    StaleConflictError,
    Unknown,
    UnknownHolderError,
    UpdateHolder,
)
from calendaring_sync.adapters.base import Steps
from calendaring_sync.adapters.caldav import _CalDAVFlow
from calendaring_sync.adapters.jmap import _JMAPFlow
from calendaring_sync.tests import _examples
from calendaring_sync.tests._helpers import (
    _BAD_TEXT,
    _CLASSIFIED,
    _OPENED_BY,
    _OTHER,
    _REMOVED,
    _SCOPE,
    _SYNCED,
    _a_removed,
    _assert_ended,
    _assert_refused,
    _change,
    _changed,
    _claim,
    _conflicted,
    _content,
    _create,
    _edit_locally,
    _edited,
    _feed,
    _in_conflict,
    _key,
    _open_session,
    _page,
    _pending,
    _push,
    _pushing,
    _refused_create,
    _remote,
    _seeded,
    _seeded_edit,
    _summary,
    _sync,
)


def _two_conflicts(replica: Replica) -> tuple[int, int]:
    """Seed a scope that ends at T3, where a and b both changed on both sides; return their keys."""
    return _in_conflict(_seeded(replica)), _in_conflict(replica, "b", "T2", "T3")


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


# A push whose outcome never arrived ends as an ingest classifies its edit.
# Where the server's copy now holds the edit, the push landed; where the edit
# is pending again, it failed. Another object at a create's id isn't the
# create, so that row stays a conflict.
_LANDED = {FeedEntryKind.CHANGED: FeedEntryKind.PUSHED, FeedEntryKind.REMOVED: FeedEntryKind.PUSHED}


_LEFT_IN_FLIGHT = [
    (
        edit,
        remote,
        ends,
        [FeedEntryKind.PUSH_FAILED]
        if ends[0] is RecordState.PENDING
        else [_LANDED.get(kind, kind) for kind in feed],
    )
    for edit, remote, ends, feed in _CLASSIFIED
    if edit is not None
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


class TestPushes:
    """Listing, claiming and settling pushes."""

    def test_to_push_lists_pending_edits_by_local_key(self, replica):
        _edited(replica)
        created = _create(replica, "fn")
        with _pushing(replica, "T2", "T3") as session:
            assert [r.local_key for r in session.to_push()] == [_key(replica), created]

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

    def test_claim_passes_over_what_has_nothing_to_push(self, replica):
        _edited(replica)
        a, b = _key(replica, "a"), _key(replica, "b")
        with _pushing(replica, "T2", "T3") as session:
            states = [session.state]
            assert _claim(session, a).state is RecordState.IN_FLIGHT
            states.append(session.state)
            assert states == [SessionState.INGESTED, SessionState.PUSHING]
            assert [session.claim(key) for key in (a, b, 999)] == [None, None, None]

    @pytest.mark.parametrize("at_claim", [True, False], ids=["claimed", "applied"])
    @pytest.mark.parametrize("object_id", ["x", "a", "b"])
    def test_a_create_takes_the_id_it_goes_out_under(self, replica, store, at_claim, object_id):
        _a_removed(replica)
        removed = replica.find_record(_SCOPE, "a")
        key = _create(replica, "fn")
        with _pushing(replica, "T2", "T3") as session:
            give: Callable[[], object]
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


class TestPushesLeftInFlight:
    """Pushes whose outcome never arrived, settled later."""

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


class TestResolutions:
    """Resolving one conflict."""

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

    def test_a_merge_the_server_already_holds_is_synced(self, replica):
        key = _in_conflict(_seeded(replica))
        replica.resolve(replica.record(key), Merge(_content("fx"), EditFields("fx")))
        assert _summary(replica.record(key)) == _SYNCED

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

    @pytest.mark.parametrize(
        "holding, error",
        [
            ("conflict", OpenConflictError),
            ("in flight", PushInFlightError),
            ("an edit", PendingEditError),
        ],
    )
    def test_a_holder_that_cant_take_an_edit_is_refused(self, replica, store, holding, error):
        key = _refused_create(_seeded(replica), "b")
        if holding == "conflict":
            _in_conflict(replica, "b", "T2", "T3")
        elif holding == "in flight":
            _push(replica, _edit_locally(replica, "change", "b")[0], None, "T2", "T3")
        else:
            _edit_locally(replica, "change", "b")
        refused = replica.record(key)
        _assert_refused(replica, store, lambda: replica.resolve(refused, UpdateHolder()), error)

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


class TestResolveConflicts:
    """Resolving every open conflict with a resolver."""

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
