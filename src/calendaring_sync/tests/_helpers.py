# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Values and helpers the unit test files share."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from calendaring_sync import (
    Change,
    Changed,
    ChangeSet,
    Conflict,
    ConflictKind,
    EditKind,
    FeedEntryKind,
    LocalEdit,
    LogicalKeyTaken,
    PushOutcome,
    Record,
    RecordState,
    Removed,
    Replica,
    Scope,
    Session,
    SessionState,
    StateStore,
)
from calendaring_sync.tests import _examples
from calendaring_sync.tests._scenarios import done

# Values the tests share.

_SCOPE = "cal"
_OTHER = "other"
_SCHEME = "scheme-1"
_OTHER_SCHEME = "scheme-2"
_MEDIA = "text/plain"
_START = datetime(2026, 10, 1, tzinfo=UTC)
# How long a test waits for another thread before it counts as stuck.
_WAIT = 5.0
# Fields a create can carry beyond its content.
_FIELDS: dict[str, Any] = {"containers": frozenset({"x", "y"}), "logical_key": "u"}
# A string no store can keep: a lone surrogate.
_BAD_TEXT = "a\ud800b"


# The tests that run the docs' examples, which an installed wheel doesn't hold.
_needs_examples = pytest.mark.skipif(
    not _examples.EXAMPLES.is_dir(), reason="The docs examples aren't installed"
)


# Building change sets and syncing them.


def _content(fingerprint: str) -> bytes:
    """The content a test gives each fingerprint, so equal fingerprints mean equal content."""
    return f"text of {fingerprint}".encode()


def _changed(
    fingerprint: str,
    version: str | None = "v1",
    *,
    content: bytes | None = None,
    containers: frozenset[str] = frozenset(),
    logical_key: str | None = None,
) -> Changed:
    """A change the server reports, holding the content a test gives ``fingerprint``."""
    if content is None:
        content = _content(fingerprint)
    return Changed(version, fingerprint, content, containers, logical_key)


def _remote(remote: str, version: str = "v2") -> Change:
    """The server's report written as ``removed``, or as a fingerprint at ``version``."""
    return Removed() if remote == "removed" else _changed(remote, version)


def _page(
    since: str | None,
    token: str,
    changes: Mapping[str, Change],
    *,
    full: bool = False,
    truncated: bool = False,
    scope: str = _SCOPE,
) -> ChangeSet:
    """A change set for ``scope``; ``full`` makes it a full listing."""
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
    """Open a session on ``scope`` with the test scheme and media type."""
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


def _seeded(replica: Replica) -> Replica:
    """A scope at token T1 holding a and b."""
    _sync(replica, _page(None, "T1", {"a": _changed("fa"), "b": _changed("fb")}, full=True))
    return replica


def _a_removed(replica: Replica) -> Replica:
    """A seeded scope at token T2, where a was removed."""
    _sync(_seeded(replica), _page("T1", "T2", {"a": Removed()}))
    return replica


# Reading what a replica holds.


def _key(replica: Replica, object_id: str = "a") -> int:
    """The local key of the record holding ``object_id`` in the test scope."""
    record = replica.find_record(_SCOPE, object_id)
    assert record is not None
    return record.local_key


def _states(replica: Replica) -> dict[str | None, RecordState]:
    """Each object's state in the test scope, by object id."""
    return {r.object_id: r.state for r in replica.records(_SCOPE)}


def _feed(replica: Replica, after: int = 0) -> list[tuple[FeedEntryKind, str | None]]:
    """The kind and object id of each feed entry after ``after``."""
    return [(e.kind, e.object_id) for e in replica.read_feed(after)]


def _summary(record: Record | None) -> tuple[object, ...] | None:
    """A record's state, edit kind and conflict kind."""
    if record is None:
        return None
    return (
        record.state,
        record.edit and record.edit.kind,
        record.conflict and record.conflict.kind,
    )


def _snapshot(replica: Replica, store: StateStore) -> tuple[object, ...]:
    """Everything a replica holds, to compare before and after; the feed from where it's trimmed."""
    with store.transaction(write=False):
        trimmed = store.feed_bounds()[0]
    feed = replica.read_feed(trimmed)
    return (replica.scopes(), replica.records(_SCOPE), feed, replica.feed_head())


# Local edits and conflicts.


def _change(
    replica: Replica,
    local_key: int,
    fingerprint: str,
    *,
    containers: frozenset[str] = frozenset(),
    logical_key: str | None = None,
) -> None:
    """Record a change to the content a test gives ``fingerprint``."""
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


def _seeded_edit(replica: Replica, local: str | None) -> tuple[int, str]:
    """Seed a scope and give it one local edit, as ``_edit_locally`` does."""
    return _edit_locally(_seeded(replica), local)


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


def _edited(replica: Replica) -> Replica:
    """A seeded scope at token T2 where a holds a pending change and b an open conflict."""
    _seeded(replica)
    _edit_locally(replica, "change")
    _in_conflict(replica, "b")
    return replica


# Pushes.


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


# Records a test writes straight to a store.


def _new_scope() -> Scope:
    """The test scope, new, as a test writes it straight to a store."""
    return Scope(_SCOPE, None, _SCHEME, _MEDIA)


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


# Checking what a call left behind.


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


# Expected results.


# What a record ends up as, in the shape _summary gives: state, edit kind,
# conflict kind.
_SYNCED = (RecordState.SYNCED, None, None)
_REMOVED = (RecordState.REMOVED, None, None)


def _pending(edit: EditKind) -> tuple[object, ...]:
    """What a record ends up as with a pending edit of kind ``edit``."""
    return (RecordState.PENDING, edit, None)


def _conflicted(edit: EditKind, kind: ConflictKind) -> tuple[object, ...]:
    """What a record ends up as with an edit of kind ``edit`` in a conflict of ``kind``."""
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
