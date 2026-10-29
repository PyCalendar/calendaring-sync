# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local edits, and the conflicts an ingest finds against them."""

from __future__ import annotations

from dataclasses import replace
from functools import partial

import pytest

from calendaring_sync import (
    Applied,
    Conflict,
    ConflictKind,
    DuplicateObjectError,
    EditKind,
    FeedEntryKind,
    LocalEdit,
    LogicalKeyTaken,
    OpenConflictError,
    PushInFlightError,
    RecordState,
    Removed,
    RemovedRecordError,
    Replica,
    Unchanged,
    UnknownRecordError,
)
from calendaring_sync.tests._helpers import (
    _CLASSIFIED,
    _FIELDS,
    _REMOVED,
    _SCOPE,
    _START,
    _SYNCED,
    _a_removed,
    _assert_ended,
    _assert_refused,
    _change,
    _changed,
    _conflicted,
    _content,
    _create,
    _feed,
    _in_conflict,
    _insert,
    _key,
    _local_record,
    _page,
    _pending,
    _push,
    _refused_create,
    _remote,
    _seeded,
    _seeded_edit,
    _step,
    _summary,
    _sync,
)


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


# The server's copy a record names: version, logical key and containers.
_SYNCED_COPY = ("v1", "k1", frozenset({"c1"}))
_CONFLICTS_COPY = ("v2", "k2", frozenset({"c2"}))
_NO_COPY: tuple[None, None, frozenset[str]] = (None, None, frozenset())


def _conflict(kind: ConflictKind, copy: tuple[str | None, str | None, frozenset[str]]) -> Conflict:
    version, logical_key, containers = copy
    return Conflict(kind, version, None, None, containers, logical_key, _START)


class TestLocalEdits:
    """Recording, collapsing and refusing local edits."""

    def test_create_keeps_what_it_was_given(self, replica, clock):
        _seeded(replica)
        key = _create(
            replica,
            "fn",
            **_FIELDS,
            object_id="n",
        )
        record = replica.record(key)
        assert (record.object_id, record.content, record.version) == ("n", None, None)
        assert record.edit == LocalEdit(
            EditKind.CREATE, _content("fn"), "fn", frozenset({"x", "y"}), "u", clock.now
        )

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

    def test_a_create_takes_over_a_removed_records_id(self, replica, store):
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
    def test_a_record_that_cant_take_an_edit_refuses_it(self, replica, store, step):
        _a_removed(replica)
        _in_conflict(replica, "b", "T2", "T3")
        in_flight = _insert(store, _local_record(RecordState.IN_FLIGHT, object_id="f"))
        refused = {
            _key(replica, "a"): RemovedRecordError,
            _key(replica, "b"): OpenConflictError,
            in_flight: PushInFlightError,
            999: UnknownRecordError,
        }
        for key, error in refused.items():
            _assert_refused(replica, store, partial(_step, replica, key, step), error)

    def test_an_edit_from_one_handle_meets_an_ingest_from_another(self, make_store, clock):
        editor = _seeded(Replica(make_store(), clock=clock))
        syncer = Replica(make_store(), clock=clock)
        _change(editor, _key(editor), "fl")
        _sync(syncer, _page("T1", "T2", {"a": _changed("fx", "v2")}))
        assert _summary(editor.find_record(_SCOPE, "a")) == _conflicted(
            EditKind.CHANGE, ConflictKind.BOTH_CHANGED
        )


class TestConflicts:
    """An ingest classifying the server's change against a local edit."""

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


class TestRefusedEdits:
    """An edit the server refused because another object holds its key."""

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


class TestServerCopy:
    """What a record says the server holds, whatever its edit and conflict."""

    @pytest.mark.parametrize(
        "state, edit, conflict, expected",
        [
            pytest.param(RecordState.SYNCED, None, None, _SYNCED_COPY, id="synced"),
            pytest.param(RecordState.PENDING, EditKind.CHANGE, None, _SYNCED_COPY, id="pending"),
            pytest.param(
                RecordState.CONFLICT,
                EditKind.CHANGE,
                _conflict(ConflictKind.BOTH_CHANGED, _CONFLICTS_COPY),
                _CONFLICTS_COPY,
                id="both changed",
            ),
            pytest.param(
                RecordState.CONFLICT,
                EditKind.CHANGE,
                _conflict(ConflictKind.REMOVED_REMOTELY, _NO_COPY),
                _NO_COPY,
                id="removed remotely",
            ),
            pytest.param(
                RecordState.CONFLICT,
                EditKind.CHANGE,
                _conflict(ConflictKind.LOGICAL_KEY_TAKEN, _NO_COPY),
                _SYNCED_COPY,
                id="change refused",
            ),
            pytest.param(
                RecordState.CONFLICT,
                EditKind.CREATE,
                _conflict(ConflictKind.LOGICAL_KEY_TAKEN, _NO_COPY),
                _NO_COPY,
                id="create refused",
            ),
            pytest.param(RecordState.REMOVED, None, None, _NO_COPY, id="removed"),
        ],
    )
    def test_the_servers_copy_follows_the_record(self, state, edit, conflict, expected):
        version, logical_key, containers = _SYNCED_COPY
        record = replace(
            _local_record(RecordState.SYNCED, "x"),
            state=state,
            version=version,
            logical_key=logical_key,
            containers=containers,
            edit=None if edit is None else LocalEdit(edit, b"c", "f", frozenset(), None, _START),
            conflict=conflict,
        )
        held = (record.server_version, record.server_logical_key, record.server_containers)
        assert held == expected
