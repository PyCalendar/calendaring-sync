# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The replica's scopes, lookups, the values it accepts, and the change feed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from calendaring_sync import (
    Applied,
    Change,
    Changed,
    ChangeSet,
    EditFields,
    FeedEntryKind,
    FeedRangeError,
    LogicalKeyTaken,
    LoneSurrogateError,
    Merge,
    RecordState,
    Removed,
    Replica,
    SQLiteStore,
    TrimmedFeedError,
    Unchanged,
    UnknownScopeError,
)
from calendaring_sync.constants import FEED_PAGE_SIZE
from calendaring_sync.tests._helpers import (
    _BAD_TEXT,
    _EDIT,
    _OTHER,
    _SCOPE,
    _a_removed,
    _assert_refused,
    _change,
    _changed,
    _create,
    _edited,
    _enter,
    _feed,
    _insert,
    _key,
    _local_record,
    _new_scope,
    _open_session,
    _page,
    _seeded,
    _states,
    _sync,
)

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


def _wrong(value: object) -> Any:
    """A value of the wrong type, passed where the type checker can't object."""
    return value


_NOT_BYTES = _wrong("text of fa")


class TestScopes:
    """Scopes in one store."""

    def test_two_scopes_stay_independent(self, replica):
        _a_removed(replica)
        _sync(replica, _page(None, "O1", {"a": _changed("other-a")}, full=True, scope=_OTHER))
        assert [s.id for s in replica.scopes()] == [_SCOPE, _OTHER]
        assert replica.records(_OTHER)[0].state is RecordState.SYNCED
        assert replica.scope(_OTHER).token == "O1"
        replica.drop_scope(_SCOPE)
        assert [s.id for s in replica.scopes()] == [_OTHER]

    def test_dropping_a_scope_returns_what_it_held(self, replica, store):
        _edited(replica)
        created = _create(replica, "fn")
        in_flight = _insert(store, _local_record(RecordState.IN_FLIGHT, object_id="f"))
        pending, head = [_key(replica), created, in_flight], replica.feed_head()
        dropped = replica.drop_scope(_SCOPE)
        assert dropped.scope == _SCOPE
        assert [r.local_key for r in dropped.pending] == pending
        assert [r.object_id for r in dropped.conflicts] == ["b"]
        assert replica.scopes() == []
        assert _feed(replica, head) == [(FeedEntryKind.SCOPE_DROPPED, None)]

    def test_an_unknown_scope_is_refused(self, replica, store):
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
            _assert_refused(replica, store, partial(call, "nope"), UnknownScopeError)


class TestLookups:
    """Finding a record by local key, object id or logical key."""

    def test_find_record_includes_removed_records(self, replica):
        _a_removed(replica)
        assert replica.find_record(_SCOPE, "a").state is RecordState.REMOVED
        assert replica.find_record(_SCOPE, "b").state is RecordState.SYNCED
        assert replica.find_record(_SCOPE, "z") is None

    def test_find_by_logical_key_matches_synced_and_edited_keys(self, replica):
        objects = {"a": _changed("fa", logical_key="u1"), "b": _changed("fb", logical_key="u2")}
        _sync(replica, _page(None, "T1", objects, full=True))
        created = _create(replica, "fn", logical_key="u1")
        _sync(replica, _page("T1", "T2", {"a": Removed()}))
        found = replica.find_by_logical_key(_SCOPE, "u1")
        assert [r.local_key for r in found] == [_key(replica), created]
        assert replica.find_by_logical_key(_SCOPE, "u3") == []

    def test_unknown_local_key_reads_as_none(self, replica):
        _seeded(replica)
        assert replica.record(999) is None


class TestValues:
    """The values the replica takes in and gives back."""

    @pytest.mark.parametrize("content, text", list(zip(_CONTENTS, _TEXTS, strict=True)))
    def test_content_and_strings_round_trip_exactly(self, replica, content, text):
        _sync(replica, _page(None, "T1", {"a": _changed("fa", content=content)}, full=True))
        created = replica.create(_SCOPE, content, fingerprint="fn")
        _change(replica, _key(replica), "fl", containers=frozenset({text}), logical_key=text)
        record = replica.find_record(_SCOPE, "a")
        assert record.content == content
        assert (record.edit.containers, record.edit.logical_key) == (frozenset({text}), text)
        assert replica.record(created).edit.content == content

    def test_a_change_set_keeps_its_own_changes(self):
        changes: dict[str, Change] = {"a": Removed()}
        change_set = _page("T1", "T2", changes)
        # What's added to the caller's mapping afterwards was never checked, so it isn't ingested.
        changes["b"] = Unchanged()
        assert dict(change_set.changes) == {"a": Removed()}
        with pytest.raises(TypeError):
            change_set.changes["b"] = Unchanged()  # type: ignore[index]

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
    """The change feed: reading, trimming and purging it."""

    def test_each_change_gets_a_numbered_entry(self, replica):
        assert replica.feed_head() == 0
        assert replica.read_feed() == []
        _seeded(replica)
        entries = replica.read_feed()
        assert [e.number for e in entries] == [1, 2]
        assert entries[0].kind is FeedEntryKind.CHANGED and entries[0].scope == _SCOPE
        assert entries[0].local_key == replica.records(_SCOPE)[0].local_key
        assert replica.feed_head() == 2

    def test_the_feed_reads_in_pages(self, replica):
        _sync(replica, _page(None, "T1", {f"o{i}": _changed(f"f{i}") for i in range(5)}, full=True))
        assert [e.number for e in replica.read_feed(0, limit=2)] == [1, 2]
        assert [e.number for e in replica.read_feed(2, limit=2)] == [3, 4]
        assert [e.number for e in replica.read_feed(5)] == []

    def test_a_cursor_below_the_trim_is_refused(self, replica, store):
        _a_removed(replica)
        replica.trim_feed(2)
        assert [e.number for e in replica.read_feed(2)] == [3]
        _assert_refused(replica, store, lambda: replica.read_feed(1), TrimmedFeedError)
        replica.trim_feed(1)
        _assert_refused(replica, store, lambda: replica.read_feed(0), TrimmedFeedError)

    def test_purge_deletes_removed_records_up_to_an_entry(self, replica):
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
    def test_bad_read_arguments_are_refused(self, replica, store, after, limit, error):
        _seeded(replica)
        _assert_refused(
            replica,
            store,
            lambda: replica.read_feed(after, limit),
            error,
            before_a_transaction=error is ValueError,
        )

    @pytest.mark.parametrize("method", ["trim_feed", "purge"])
    @pytest.mark.parametrize("through", [-1, 3])
    def test_a_through_outside_the_feed_is_refused(self, replica, store, method, through):
        _seeded(replica)
        call = getattr(replica, method)
        _assert_refused(replica, store, lambda: call(through), FeedRangeError)
