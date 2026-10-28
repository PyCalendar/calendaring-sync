# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The core's entry point, which holds every sync rule on top of any :term:`store`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from typing import TypeVar, cast

from calendaring_sync._checks import check_argument_types, check_data, check_text
from calendaring_sync._classify import (
    ChangeClass,
    classify,
    same_conflict,
    same_data,
    same_remote,
)
from calendaring_sync.constants import FEED_PAGE_SIZE
from calendaring_sync.error import (
    DuplicateObjectError,
    FeedRangeError,
    ListingRequiredError,
    ObjectIdAssignedError,
    OpenConflictError,
    OtherScopeError,
    PendingEditError,
    PushInFlightError,
    RemovedRecordError,
    ScopeMismatchError,
    SessionError,
    StaleChangeSetError,
    StaleConflictError,
    TrimmedFeedError,
    UnknownHolderError,
    UnknownRecordError,
    UnknownScopeError,
    UnknownVersionError,
)
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.push import (
    Applied,
    Gone,
    PushOutcome,
    Rejected,
    Unknown,
)
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.resolution import (
    Delete,
    KeepLocal,
    KeepServer,
    Merge,
    Resolution,
    ResolvedConflicts,
    UpdateHolder,
)
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore

_T = TypeVar("_T")

# The record states the core reads one at a time.
_PENDING = frozenset({RecordState.PENDING})
_IN_FLIGHT = frozenset({RecordState.IN_FLIGHT})
_CONFLICT = frozenset({RecordState.CONFLICT})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _without_fingerprints(record: Record) -> Record:
    """The record after a fingerprint scheme change: no fingerprint, and no version to reuse."""
    edit, conflict = record.edit, record.conflict
    return replace(
        record,
        version=None,
        fingerprint=None,
        edit=None if edit is None else replace(edit, fingerprint=None),
        conflict=None
        if conflict is None
        else replace(conflict, remote_version=None, remote_fingerprint=None),
    )


def _check_resolution(record: Record, resolution: Resolution) -> None:
    """Raise ``ValueError`` unless ``record`` is in conflict and ``resolution`` fits its kind."""
    if record.state is not RecordState.CONFLICT or record.conflict is None:
        raise ValueError("resolve() takes a record in conflict")
    if isinstance(resolution, KeepLocal) and record.is_refused:
        raise ValueError("An edit refused for its logical key can't be pushed again unchanged")
    if isinstance(resolution, UpdateHolder) and not record.is_refused:
        raise ValueError("Only an edit refused for its logical key goes to the key's holder")


def _same_decision(stored: Record | None, record: Record) -> bool:
    """Whether ``stored`` still holds the conflict a decision was made on from ``record``."""
    return (
        stored is not None
        and stored.conflict is not None
        and record.conflict is not None
        and stored.edit == record.edit
        and same_conflict(stored.conflict, record.conflict)
    )


def _resolved(record: Record, resolution: Resolution, now: datetime) -> Record | None:
    """The record a resolution leaves, or ``None`` when it leaves nothing to keep.

    The synced copy becomes the server's current copy, so whatever is pushed
    next goes out against the server's current version.
    """
    conflict, edit = cast(Conflict, record.conflict), cast(LocalEdit, record.edit)
    on_server = conflict.remote_content is not None or record.is_refused_change
    if not on_server and isinstance(resolution, (KeepServer, Delete)):
        # The server doesn't have the object: a create it never had leaves nothing.
        return None if record.is_create else record.removed_at(now)
    if record.is_refused_change:
        # The server kept the synced copy.
        base = replace(record, state=RecordState.SYNCED, edit=None, conflict=None)
    else:
        base = record.synced_to(conflict, now)
    # Over a copy the server has, an edit is a change; with none, a create.
    kind = EditKind.CHANGE if on_server else EditKind.CREATE
    match resolution:
        case KeepServer():
            return base
        case KeepLocal():
            return _edited(base, edit if edit.kind is EditKind.DELETE else replace(edit, kind=kind))
        case Merge():
            merged = LocalEdit(
                kind,
                resolution.content,
                resolution.fields.fingerprint,
                resolution.fields.containers,
                resolution.fields.logical_key,
                now,
            )
            return _edited(base, merged)
    # Delete, the one resolution left.
    return _edited(base, LocalEdit(EditKind.DELETE, None, None, frozenset(), None, now))


def _edited(record: Record, edit: LocalEdit) -> Record:
    """The record holding ``edit``, or synced if the edit brings it back to its synced copy."""
    if edit.kind is EditKind.CHANGE and record.content is not None and same_data(edit, record):
        return replace(record, state=RecordState.SYNCED, edit=None)
    return replace(record, state=RecordState.PENDING, edit=edit)


def _changed_to(record: Record, edit: LocalEdit) -> Record:
    """The record after a change to ``edit``'s data; a create stays a create."""
    kind = EditKind.CREATE if record.is_create else EditKind.CHANGE
    return _edited(record, replace(edit, kind=kind))


def _waits_for_version(record: Record, *, guarded: bool) -> bool:
    """Whether a pending edit waits for an ingest, because its push would go out unguarded."""
    return guarded and record.version is None and not record.is_create


@check_argument_types
class Replica:
    """The sync state of every :term:`scope` in one :term:`store`.

    This is the class your application and the :term:`adapter` use: the
    adapter ingests what the server changed through a :meth:`session`, and
    your application records its edits and reads records and the
    :term:`change feed`. The store underneath only keeps the data.

    Each method runs in a store transaction of its own, so two calls can
    see another thread's or process's write between them. To read several
    things at one point in time, open the store's
    :meth:`~calendaring_sync.store.base.StateStore.transaction` with
    ``write=False`` and call the store's own methods inside it. A replica's
    methods can't run inside a transaction already open in the same
    thread; they raise :class:`~calendaring_sync.error.TransactionError`.

    Every method raises :class:`TypeError` for an argument of the wrong
    type, before it changes anything. Any method can also raise the
    store's errors, :class:`~calendaring_sync.error.StoreBusyError`,
    :class:`~calendaring_sync.error.StoreError` and
    :class:`~calendaring_sync.error.UnexpectedStoreError`, none of which
    changes anything.

    Args:
        store: Where the state is kept.
        clock: Returns the current time for every timestamp. Defaults to
            the current time in UTC.

    Examples:
        A replica on a store in memory, with one scope that a
        :term:`full listing` filled with one object. The examples on each
        method start from this replica. An adapter computes fingerprints
        from the content; these are short made-up ones.

        >>> from calendaring_sync import Changed, ChangeSet, MemoryStore, Replica
        >>> replica = Replica(MemoryStore())
        >>> listing = ChangeSet(
        ...     "work", None, "t1", {"a": Changed("v1", "fp-a", b"Team meeting")}, full_listing=True
        ... )
        >>> with replica.session(
        ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
        ... ) as session:
        ...     session.ingest(listing)
        >>> replica.find_record("work", "a").content
        b'Team meeting'
        >>> [(entry.kind, entry.object_id) for entry in replica.read_feed()]
        [(<FeedEntryKind.CHANGED: 'changed'>, 'a')]
    """

    def __init__(self, store: StateStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._clock = clock or _utc_now
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = Lock()

    @contextmanager
    def session(
        self,
        scope_id: str,
        *,
        fingerprint_scheme: str,
        media_type: str,
        guarded_by_version: bool = False,
        unique_logical_key: bool = False,
    ) -> Iterator[Session]:
        """Open a :term:`session` for one scope, for the length of the ``with`` block.

        The first session creates the scope. A different
        :term:`fingerprint scheme` from the stored one resets the scope and
        clears every record's version and fingerprint, so the next
        :term:`full listing` fetches every object again. Objects whose
        content is the same as before aren't reported as changed.

        Opening the session and each
        :meth:`~calendaring_sync.session.Session.ingest` run in a write
        transaction of their own.

        Args:
            scope_id: The scope's id, chosen by the :term:`adapter`.
            fingerprint_scheme: The name of the adapter's fingerprint scheme.
            media_type: The media type of the scope's :term:`content`.
            guarded_by_version: Whether the adapter's pushes only apply if
                the object still has the :term:`version` they were based on.
                A change or delete then waits for an ingest whenever its
                record has no version, so it never goes out unguarded.
            unique_logical_key: Whether no two objects in the scope can share
                a :term:`logical key`. A create whose answer never arrived is
                then recognized at the next ingest by its key and the data it
                was sent with, which a create the server picks the id for needs.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            SessionError: If this replica already has a session open for the scope.
            ScopeMismatchError: If the scope's content has another media type.
            LoneSurrogateError: If a string holds a lone surrogate.

        Examples:
            >>> changes = {"a": Changed("v2", "fp-a2", b"Team meeting, moved")}
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", changes))
            >>> replica.find_record("work", "a").content
            b'Team meeting, moved'
        """
        check_text(scope_id=scope_id, fingerprint_scheme=fingerprint_scheme, media_type=media_type)
        session = Session(
            self,
            scope_id,
            guarded_by_version=guarded_by_version,
            unique_logical_key=unique_logical_key,
        )
        with self._sessions_lock:
            if scope_id in self._sessions:
                raise SessionError(f"A session for {scope_id!r} is already open")
            self._sessions[scope_id] = session
        try:
            self._open_scope(scope_id, fingerprint_scheme, media_type)
            yield session
        finally:
            session._end()
            with self._sessions_lock:
                del self._sessions[scope_id]

    def _open_scope(self, scope_id: str, fingerprint_scheme: str, media_type: str) -> None:
        with self._store.transaction():
            scope = self._store.get_scope(scope_id)
            if scope is None:
                self._store.put_scope(Scope(scope_id, None, fingerprint_scheme, media_type))
                return
            if scope.media_type != media_type:
                raise ScopeMismatchError(
                    f"Scope {scope_id!r} holds {scope.media_type!r} content, not {media_type!r}"
                )
            if scope.fingerprint_scheme != fingerprint_scheme:
                self._store.put_scope(
                    replace(
                        scope, token=None, listing_token=None, fingerprint_scheme=fingerprint_scheme
                    )
                )
                for record in self._store.scope_records(scope_id):
                    self._store.update_record(_without_fingerprints(record))

    def _ingest(
        self,
        change_set: ChangeSet,
        *,
        first_page: bool,
        mentioned: set[str],
        unique_logical_key: bool,
    ) -> set[str]:
        """Apply one change set in one transaction; return the listing's mentioned ids after it."""
        now = self._clock()
        mentioned = set(mentioned) if not first_page else set()
        with self._store.transaction():
            scope = self._existing_scope(change_set.scope)
            if change_set.since != scope.token:
                raise StaleChangeSetError(
                    f"Change set from token {change_set.since!r}, "
                    f"scope {scope.id!r} is at {scope.token!r}"
                )
            if scope.token is None and not change_set.full_listing:
                raise ListingRequiredError(
                    f"Scope {scope.id!r} has no token, so it needs a full listing"
                )
            if change_set.full_listing:
                scope = self._check_listing_page(scope, change_set, first_page)
            claimed = self._claimed_creates(scope.id) if unique_logical_key else {}
            freed: set[str] = set()
            for object_id, change in change_set.changes.items():
                self._apply(scope.id, object_id, change, now, claimed, freed)
                if change_set.full_listing:
                    mentioned.add(object_id)
            if change_set.full_listing and not change_set.truncated:
                self._remove_unmentioned(scope.id, mentioned, now, freed)
                scope = replace(
                    scope, token=change_set.token, listing_token=None, last_full_listing_at=now
                )
            elif not change_set.full_listing:
                scope = replace(scope, token=change_set.token)
            self._lift_refusals(scope.id, freed, now)
            if not change_set.truncated:
                # The change set is complete: a push it didn't settle didn't land.
                for record in self._store.scope_records(scope.id, states=_IN_FLIGHT):
                    self._write(
                        replace(record, state=RecordState.PENDING), FeedEntryKind.PUSH_FAILED, now
                    )
                scope = replace(scope, last_sync_at=now)
            self._store.put_scope(scope)
        return mentioned

    def _check_listing_page(self, scope: Scope, change_set: ChangeSet, first_page: bool) -> Scope:
        """Mark a listing as started, or check a later page belongs to the listing in progress."""
        if first_page:
            return replace(scope, listing_token=change_set.token)
        if scope.listing_token != change_set.token:
            raise StaleChangeSetError(
                f"Page of listing {change_set.token!r}, but {scope.id!r} is listing "
                f"{scope.listing_token!r}"
            )
        return scope

    def _apply(
        self,
        scope_id: str,
        object_id: str,
        change: Change,
        now: datetime,
        claimed: dict[str, Record],
        freed: set[str],
    ) -> None:
        """Apply one object's change to the store, classified against its local edit.

        ``claimed`` maps a logical key to the one create in flight without
        an object id that holds it; a new object with that key is taken as
        that create, and leaves the map. A logical key the server's copy held
        before the change and doesn't after it goes into ``freed``.
        """
        record = self._store.find_record(scope_id, object_id)
        held = None if record is None else record.server_logical_key
        if held is not None and (
            isinstance(change, Removed)
            or (isinstance(change, Changed) and change.logical_key != held)
        ):
            freed.add(held)
        if isinstance(change, Unchanged):
            if record is None or record.server_version is None:
                raise UnknownVersionError(
                    f"Object {object_id!r} is reported unchanged, "
                    "but the store has no version for it"
                )
            return
        if record is None and isinstance(change, Changed) and change.logical_key is not None:
            # A new object with the key is the create only if it holds what was sent;
            # anything else is someone else's object, and the create goes out again.
            create = claimed.get(change.logical_key)
            if create is not None and create.edit is not None and same_data(change, create.edit):
                record = claimed.pop(change.logical_key)
        in_flight = record is not None and record.state is RecordState.IN_FLIGHT
        live = None if record is None or record.state is RecordState.REMOVED else record
        # A refused change holds no copy of the server's object, so whatever the
        # server reports about it is news, a removal included.
        if (
            live is not None
            and live.conflict is not None
            and not live.is_refused_change
            and same_remote(live.conflict, change)
        ):
            if isinstance(change, Changed):
                conflict = Conflict.from_change(live.conflict.kind, change, live.conflict.opened_at)
                self._store.update_record(replace(live, conflict=conflict))
            return
        edit = None if live is None else live.edit
        synced = live if live is not None and live.content is not None else None
        change_class, kind = classify(synced, change, edit)
        was_conflict = live is not None and live.conflict is not None
        # A record leaving a conflict is reported as the conflict ending, and
        # one whose push was in flight as the push reaching the server.
        if was_conflict:
            settled: FeedEntryKind | None = FeedEntryKind.CONFLICT_RESOLVED
        elif in_flight:
            settled = FeedEntryKind.PUSHED
        else:
            settled = None
        match change_class:
            case ChangeClass.CONFLICT if kind is not None and live is not None:
                opened_at = now if live.conflict is None else live.conflict.opened_at
                self._write(
                    replace(
                        live,
                        state=RecordState.CONFLICT,
                        conflict=Conflict.from_change(kind, change, opened_at),
                    ),
                    FeedEntryKind.CONFLICT_UPDATED
                    if was_conflict
                    else FeedEntryKind.CONFLICT_OPENED,
                    now,
                )
            case ChangeClass.REMOTE_ONLY if isinstance(change, Changed):
                self._store_server_copy(record, scope_id, object_id, change, now, settled)
            case ChangeClass.UNCHANGED if isinstance(change, Changed) and edit is not None:
                # Both sides arrived at the same content, so the edit is done.
                self._store_server_copy(record, scope_id, object_id, change, now, settled)
            case ChangeClass.DELETED_REMOTE if live is not None:
                self._write(live.removed_at(now), settled or FeedEntryKind.REMOVED, now)
            case ChangeClass.UNCHANGED | ChangeClass.LOCAL_ONLY | ChangeClass.DELETED_LOCAL if (
                live is not None and (isinstance(change, Changed) or was_conflict or in_flight)
            ):
                # The server's copy holds the synced data, so only its version and
                # content move; a conflict this settles leaves the edit to be pushed,
                # and so does a push in flight that hasn't landed.
                if isinstance(change, Changed):
                    live = live.with_copy(change, now)
                if was_conflict or in_flight:
                    self._write(
                        replace(live, state=RecordState.PENDING, conflict=None),
                        FeedEntryKind.CONFLICT_RESOLVED
                        if was_conflict
                        else FeedEntryKind.PUSH_FAILED,
                        now,
                    )
                else:
                    self._store.update_record(live)

    def _store_server_copy(
        self,
        record: Record | None,
        scope_id: str,
        object_id: str,
        change: Changed,
        now: datetime,
        settled: FeedEntryKind | None,
    ) -> None:
        """Store the server's copy as the record's synced copy, with the feed entry about it."""
        known = Record.new(scope_id, object_id) if record is None else record
        synced = replace(known.synced_to(change, now), object_id=object_id)
        if record is None:
            synced = replace(synced, local_key=self._store.insert_record(synced))
        self._write(synced, settled or FeedEntryKind.CHANGED, now)

    def _claimed_creates(self, scope_id: str) -> dict[str, Record]:
        """Map each logical key to the one create in flight without an object id that holds it.

        A key two such creates hold is left out: neither can be told apart
        from the other by it.
        """
        found: dict[str, list[Record]] = {}
        for record in self._store.scope_records(scope_id, states=_IN_FLIGHT):
            key = record.edit.logical_key if record.edit else None
            if record.is_create and record.object_id is None and key is not None:
                found.setdefault(key, []).append(record)
        return {key: records[0] for key, records in found.items() if len(records) == 1}

    def _remove_unmentioned(
        self, scope_id: str, mentioned: set[str], now: datetime, freed: set[str]
    ) -> None:
        """Remove every record with an object id the finished listing never mentioned."""
        for record in self._store.scope_records(scope_id):
            if record.object_id is not None and record.object_id not in mentioned:
                self._apply(scope_id, record.object_id, Removed(), now, {}, freed)

    def _lift_refusals(self, scope_id: str, freed: set[str], now: datetime) -> None:
        """Make each edit refused for a key in ``freed`` pending, unless a server copy holds it.

        Only a key the server stopped holding here lifts a refusal: a key
        held by an object outside the scope never shows up as freed, so its
        edit isn't sent again to be refused again.
        """
        if not freed:
            return
        for record in self._store.scope_records(scope_id, states=_CONFLICT):
            key = record.edit.logical_key if record.edit else None
            if not record.is_refused or key is None or key not in freed:
                continue
            holders = [
                other
                for other in self._store.find_by_logical_key(scope_id, key)
                if other.local_key != record.local_key and other.server_logical_key == key
            ]
            if not holders:
                self._write(
                    replace(record, state=RecordState.PENDING, conflict=None),
                    FeedEntryKind.CONFLICT_RESOLVED,
                    now,
                )

    def _write(self, record: Record, kind: FeedEntryKind, at: datetime) -> None:
        """Store a record together with the feed entry about it."""
        entry = self._store.append_entry(record.scope, kind, record.local_key, record.object_id, at)
        self._store.update_record(replace(record, last_entry=entry.number))

    def _existing_scope(self, scope_id: str) -> Scope:
        scope = self._store.get_scope(scope_id)
        if scope is None:
            raise UnknownScopeError(f"No scope {scope_id!r}")
        return scope

    def _read_in_scope(self, scope_id: str, read: Callable[[], _T]) -> _T:
        """Run ``read`` in a read transaction, once the scope is known to exist."""
        with self._store.transaction(write=False):
            self._existing_scope(scope_id)
            return read()

    @contextmanager
    def _without_session(self, scope_id: str) -> Iterator[None]:
        """Hold off new sessions for a scope that has none open, for the ``with`` block."""
        with self._sessions_lock:
            if scope_id in self._sessions:
                raise SessionError(f"A session for {scope_id!r} is open")
            yield

    def scope(self, scope_id: str) -> Scope:
        """Return one scope.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.

        Examples:
            >>> replica.scope("work").token
            't1'
        """
        with self._store.transaction(write=False):
            return self._existing_scope(scope_id)

    def scopes(self) -> list[Scope]:
        """Return every scope, ordered by id.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.

        Examples:
            >>> [scope.id for scope in replica.scopes()]
            ['work']
        """
        with self._store.transaction(write=False):
            return self._store.scopes()

    def record(self, local_key: int) -> Record | None:
        """Return the record with this :term:`local key`, or ``None``.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.

        Examples:
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.record(key).object_id
            'a'
        """
        with self._store.transaction(write=False):
            return self._store.get_record(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Return the scope's record with this :term:`object id`, or ``None``.

        A removed record is returned until it's :term:`purged <purge>`.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.

        Examples:
            >>> replica.find_record("work", "a").state
            <RecordState.SYNCED: 'synced'>
            >>> replica.find_record("work", "z") is None
            True
        """
        return self._read_in_scope(scope_id, lambda: self._store.find_record(scope_id, object_id))

    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        """Return the scope's records with this :term:`logical key`, ordered by local key.

        A record matches if its synced copy or its :term:`local edit` has
        the key. Several records can share one, and removed records are
        included until they're :term:`purged <purge>`.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.

        Examples:
            >>> key = replica.create("work", b"Lunch", fingerprint="fp-l", logical_key="uid-1")
            >>> found = replica.find_by_logical_key("work", "uid-1")
            >>> [record.local_key for record in found] == [key]
            True
        """
        return self._read_in_scope(
            scope_id, lambda: self._store.find_by_logical_key(scope_id, logical_key)
        )

    def create(
        self,
        scope_id: str,
        content: bytes,
        *,
        fingerprint: str,
        containers: frozenset[str] = frozenset(),
        logical_key: str | None = None,
        object_id: str | None = None,
    ) -> int:
        """Record the creation of an object, and return its :term:`local key`.

        The record is pending until a :term:`push` creates the object on
        the server. A create at the object id of a record the server removed
        takes that record over, as the server does when it has an object at
        that id again: it keeps its local key and drops the removed copy.
        Pass the ``fingerprint``, ``containers`` and ``logical_key`` the
        :term:`adapter`'s :meth:`~calendaring_sync.adapters.base.Adapter.edit_fields`
        reads from the content; built any other way, an identical copy on
        the server looks different. An adapter's own ``create`` does this
        for you.

        Runs in a write transaction of its own.

        Args:
            scope_id: The scope the object belongs to.
            content: The object's :term:`content`.
            fingerprint: The content's :term:`fingerprint`.
            containers: The :term:`containers <container>` the object belongs to.
            logical_key: The object's :term:`logical key`, if it has one.
            object_id: The :term:`object id` the create chose, where the
                server takes ids from its clients; ``None`` where the server
                picks one.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.
            DuplicateObjectError: If a record that isn't removed holds the
                object id.
            LoneSurrogateError: If a string holds a lone surrogate.

        Examples:
            >>> key = replica.create("work", b"Lunch", fingerprint="fp-l", object_id="b")
            >>> replica.record(key).state
            <RecordState.PENDING: 'pending'>
            >>> replica.record(key).edit.content
            b'Lunch'
        """
        check_data(fingerprint, containers, logical_key, scope_id=scope_id, object_id=object_id)
        with self._store.transaction():
            self._existing_scope(scope_id)
            edit = LocalEdit(
                EditKind.CREATE, content, fingerprint, containers, logical_key, self._clock()
            )
            created = replace(Record.new(scope_id, object_id), state=RecordState.PENDING, edit=edit)
            removed = None if object_id is None else self._removed_holder(scope_id, object_id)
            if removed is None:
                return self._store.insert_record(created)
            self._store.update_record(
                replace(
                    created,
                    local_key=removed.local_key,
                    synced_at=removed.synced_at,
                    last_entry=removed.last_entry,
                )
            )
            return removed.local_key

    def change(
        self,
        local_key: int,
        content: bytes,
        *,
        fingerprint: str,
        containers: frozenset[str] = frozenset(),
        logical_key: str | None = None,
    ) -> None:
        """Record a change to an object.

        A change back to the :term:`synced copy` cancels the edit, a
        change after a delete replaces it, and a change to an object you
        created is still a create. Build ``fingerprint``, ``containers`` and
        ``logical_key`` as for :meth:`create`.

        Runs in a write transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownRecordError: If there's no record with this local key.
            OpenConflictError: If the record holds an open :term:`conflict`.
            RemovedRecordError: If the record was removed from the server.
            PushInFlightError: If the record's :term:`push` is :term:`in flight`.
            LoneSurrogateError: If a string holds a lone surrogate.

        Examples:
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, moved", fingerprint="fp-a2")
            >>> replica.record(key).edit.content
            b'Team meeting, moved'

            A change back to the synced copy cancels the edit:

            >>> replica.change(key, b"Team meeting", fingerprint="fp-a")
            >>> replica.record(key).state
            <RecordState.SYNCED: 'synced'>
        """
        check_data(fingerprint, containers, logical_key)
        with self._editing(local_key) as record:
            edit = LocalEdit(
                EditKind.CHANGE, content, fingerprint, containers, logical_key, self._clock()
            )
            self._store.update_record(_changed_to(record, edit))

    def delete(self, local_key: int) -> None:
        """Record the deletion of an object.

        Deleting an object you created that the server doesn't have yet
        removes its record, leaving nothing to push.

        Runs in a write transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownRecordError: If there's no record with this local key.
            OpenConflictError: If the record holds an open :term:`conflict`.
            RemovedRecordError: If the record was removed from the server.
            PushInFlightError: If the record's :term:`push` is :term:`in flight`.

        Examples:
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.delete(key)
            >>> replica.record(key).edit.kind
            <EditKind.DELETE: 'delete'>
        """
        with self._editing(local_key) as record:
            if record.is_create:
                self._drop_edit(record)
                return
            edit = LocalEdit(EditKind.DELETE, None, None, frozenset(), None, self._clock())
            self._store.update_record(replace(record, state=RecordState.PENDING, edit=edit))

    def discard(self, local_key: int) -> None:
        """Discard an object's :term:`pending edit`, so it matches the server's copy again.

        Discarding a create removes its record. A record with no edit is
        left as it is.

        Runs in a write transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownRecordError: If there's no record with this local key.
            OpenConflictError: If the record holds an open :term:`conflict`.
            RemovedRecordError: If the record was removed from the server.
            PushInFlightError: If the record's :term:`push` is :term:`in flight`.

        Examples:
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.delete(key)
            >>> replica.discard(key)
            >>> replica.record(key).state
            <RecordState.SYNCED: 'synced'>
        """
        with self._editing(local_key) as record:
            if record.edit is not None:
                self._drop_edit(record)

    def _to_push(self, scope_id: str, *, guarded: bool) -> list[Record]:
        with self._store.transaction(write=False):
            pending = self._store.scope_records(scope_id, states=_PENDING)
        return [r for r in pending if not _waits_for_version(r, guarded=guarded)]

    def _claim(
        self, scope_id: str, local_key: int, object_id: str | None, *, guarded: bool
    ) -> Record | None:
        with self._store.transaction():
            record = self._store.get_record(local_key)
            if record is None:
                return None
            if record.scope != scope_id:
                raise OtherScopeError(f"Record {local_key} belongs to scope {record.scope!r}")
            if object_id is not None and record.object_id is not None:
                raise ObjectIdAssignedError(
                    f"Record {local_key} already has object id {record.object_id!r}"
                )
            if record.state is not RecordState.PENDING or _waits_for_version(
                record, guarded=guarded
            ):
                return None
            if object_id is not None:
                self._free_object_id(scope_id, object_id, record.local_key)
            claimed = replace(
                record,
                state=RecordState.IN_FLIGHT,
                object_id=object_id or record.object_id,
                claims=record.claims + 1,
            )
            self._store.update_record(claimed)
            return claimed

    def _settle(self, claimed: Record, outcome: PushOutcome) -> bool:
        now = self._clock()
        with self._store.transaction():
            record = self._store.get_record(claimed.local_key)
            # Only the latest claim of the same edit counts: another process may
            # have settled this push, and claimed the edit again since.
            if (
                record is None
                or record.edit != claimed.edit
                or record.claims != claimed.claims
                or record.state not in (RecordState.IN_FLIGHT, RecordState.PENDING)
            ):
                return False
            pending_again = record.state is RecordState.PENDING
            held = record.server_logical_key
            # A record in flight or pending always holds an edit; the store checks it.
            edit = cast(LocalEdit, record.edit)
            match outcome:
                case Rejected() | Unknown() if pending_again:
                    pass
                case Rejected():
                    self._write(
                        replace(record, state=RecordState.PENDING), FeedEntryKind.PUSH_FAILED, now
                    )
                case Unknown():
                    pass
                case Applied() | Gone() if edit.kind is EditKind.DELETE:
                    self._write(record.removed_at(now), FeedEntryKind.PUSHED, now)
                case Applied(object_id=renamed):
                    if renamed is not None and renamed != record.object_id:
                        self._free_object_id(record.scope, renamed, record.local_key)
                    # The server may store the edit rewritten, so no version is kept.
                    pushed = replace(
                        record.synced_to(edit, now), object_id=renamed or record.object_id
                    )
                    self._write(pushed, FeedEntryKind.PUSHED, now)
                case Gone():
                    self._open_conflict(record, ConflictKind.REMOVED_REMOTELY, None, now)
                case _:
                    # LogicalKeyTaken, the one outcome left.
                    self._open_conflict(record, ConflictKind.LOGICAL_KEY_TAKEN, outcome.holder, now)
            settled = self._store.get_record(record.local_key)
            if held is not None and (settled is None or settled.server_logical_key != held):
                self._lift_refusals(record.scope, {held}, now)
        return True

    def _open_conflict(
        self, record: Record, kind: ConflictKind, holder: str | None, now: datetime
    ) -> None:
        """Store a conflict whose server has no copy of the object, from a push's outcome."""
        conflict = replace(Conflict.from_change(kind, Removed(), now), holder=holder)
        self._write(
            replace(record, state=RecordState.CONFLICT, conflict=conflict),
            FeedEntryKind.CONFLICT_OPENED,
            now,
        )

    def _free_object_id(self, scope_id: str, object_id: str, owner: int) -> None:
        """Make ``object_id`` free for record ``owner``.

        A removed record holding it gives the id up but stays, as a
        :term:`change feed` reader may still read it, until it's purged.

        Raises:
            DuplicateObjectError: If a record that isn't removed holds it.
        """
        removed = self._removed_holder(scope_id, object_id, owner)
        if removed is not None:
            self._store.update_record(replace(removed, object_id=None))

    def _removed_holder(
        self, scope_id: str, object_id: str, owner: int | None = None
    ) -> Record | None:
        """The removed record that hands ``object_id`` over, or ``None`` if no other holds it.

        Raises:
            DuplicateObjectError: If a record that isn't removed holds it.
        """
        holder = self._store.find_record(scope_id, object_id)
        if holder is None or holder.local_key == owner:
            return None
        if holder.state is not RecordState.REMOVED:
            raise DuplicateObjectError(f"Scope {scope_id!r} already has object {object_id!r}")
        return holder

    def _drop_edit(self, record: Record) -> None:
        """Leave the record as the server has it: synced, or gone if the server never had it."""
        if record.is_create:
            self._store.delete_record(record.local_key)
        else:
            self._store.update_record(replace(record, state=RecordState.SYNCED, edit=None))

    @contextmanager
    def _editing(self, local_key: int) -> Iterator[Record]:
        """Open the write transaction an edit runs in, and yield the record it edits.

        The record is frozen, so nothing is saved by changing it: the edit
        writes its new copy with ``update_record`` inside the block, and
        leaving the block commits it.
        """
        with self._store.transaction():
            yield self._editable(local_key)

    def _editable(self, local_key: int) -> Record:
        """Return the record with this local key, or raise if it takes no local edit."""
        record = self._store.get_record(local_key)
        if record is None:
            raise UnknownRecordError(f"No record with local key {local_key}")
        if record.state is RecordState.CONFLICT:
            raise OpenConflictError(f"Record {local_key} holds an open conflict")
        if record.state is RecordState.REMOVED:
            raise RemovedRecordError(f"Record {local_key} was removed from the server")
        if record.state is RecordState.IN_FLIGHT:
            raise PushInFlightError(f"Record {local_key} has a push in flight")
        return record

    def records(
        self, scope_id: str, *, states: frozenset[RecordState] | None = None
    ) -> list[Record]:
        """Return the records in a scope, removed ones included, ordered by local key.

        Runs in a read transaction of its own.

        Args:
            scope_id: The scope to read.
            states: Only the records in one of these states; ``None`` for every record.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.

        Examples:
            >>> [record.object_id for record in replica.records("work")]
            ['a']
            >>> replica.records("work", states=frozenset({RecordState.PENDING}))
            []
        """
        return self._read_in_scope(
            scope_id, lambda: self._store.scope_records(scope_id, states=states)
        )

    def known_versions(self, scope_id: str) -> dict[str, str]:
        """Return the version of the server's copy of every object the store knows one for.

        For a record in :term:`conflict` that's the :term:`server's copy`. An
        :term:`adapter` compares these with the server's during a
        :term:`full listing`, and reports objects whose version matches as
        ``Unchanged`` instead of fetching them. Removed records aren't included.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.

        Examples:
            >>> replica.known_versions("work")
            {'a': 'v1'}
        """
        return {
            record.object_id: version
            for record in self.records(scope_id)
            if record.object_id is not None and (version := record.server_version) is not None
        }

    def reset(self, scope_id: str) -> None:
        """:term:`Reset <reset>` a scope, so its next sync is a :term:`full listing`.

        Records keep their state.

        Runs in a write transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.
            SessionError: If a session for the scope is open.

        Examples:
            >>> replica.reset("work")
            >>> replica.scope("work").token is None
            True
        """
        with self._without_session(scope_id), self._store.transaction():
            scope = self._existing_scope(scope_id)
            self._store.put_scope(replace(scope, token=None, listing_token=None))

    def drop_scope(self, scope_id: str) -> DroppedScope:
        """:term:`Drop <drop>` a scope and every record in it.

        Runs in a write transaction of its own.

        Returns:
            The records holding local work that the drop discarded.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.
            SessionError: If a session for the scope is open.

        Examples:
            >>> dropped = replica.drop_scope("work")
            >>> dropped.pending, dropped.conflicts
            ((), ())
            >>> replica.scopes()
            []
        """
        with self._without_session(scope_id), self._store.transaction():
            self._existing_scope(scope_id)
            records = self._store.scope_records(scope_id)
            self._store.delete_scope(scope_id)
            self._store.append_entry(
                scope_id, FeedEntryKind.SCOPE_DROPPED, None, None, self._clock()
            )
        pending = (RecordState.PENDING, RecordState.IN_FLIGHT)
        return DroppedScope(
            scope=scope_id,
            pending=tuple(r for r in records if r.state in pending),
            conflicts=tuple(r for r in records if r.state is RecordState.CONFLICT),
        )

    def resolve(self, record: Record, resolution: Resolution) -> None:
        """Resolve one :term:`conflict`, as your application decided from ``record``.

        Anything but the server's copy becomes a :term:`pending edit` over the
        server's current copy, so the next sync pushes it against the server's
        current version. Writes a ``CONFLICT_RESOLVED`` entry to the
        :term:`change feed`. Runs in a write transaction of its own.

        Args:
            record: The record in conflict, as your application read it.
            resolution: What to do with it.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            StaleConflictError: If the conflict changed since ``record`` was
                read: it ended, or its edit or the server's copy moved on. A
                move of that copy's version alone doesn't count.
            UnknownHolderError: For ``UpdateHolder``, if the server named no
                record that's still there, and no single record holds the
                refused edit's logical key, or it was sent without one.
            OpenConflictError: For ``UpdateHolder``, if the holder is in conflict.
            PushInFlightError: For ``UpdateHolder``, if the holder's push is in flight.
            PendingEditError: For ``UpdateHolder``, if the holder holds a
                pending edit of its own, which this would replace.
            ValueError: If ``record`` isn't in conflict, or the resolution
                doesn't fit its kind: ``KeepLocal`` for an edit the server
                refused for its logical key, or ``UpdateHolder`` for any other.

        Examples:
            >>> from calendaring_sync import KeepLocal
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, room 2", fingerprint="fp-mine")
            >>> theirs = {"a": Changed("v2", "fp-theirs", b"Team meeting, room 3")}
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", theirs))
            >>> record = replica.record(key)
            >>> record.conflict.kind
            <ConflictKind.BOTH_CHANGED: 'both_changed'>
            >>> replica.resolve(record, KeepLocal())
            >>> replica.record(key).state, replica.record(key).version
            (<RecordState.PENDING: 'pending'>, 'v2')
        """
        _check_resolution(record, resolution)
        now = self._clock()
        with self._store.transaction():
            stored = self._store.get_record(record.local_key)
            if not _same_decision(stored, record):
                raise StaleConflictError(f"Record {record.local_key}'s conflict changed")
            stored = cast(Record, stored)
            if isinstance(resolution, UpdateHolder):
                holder = self._editable(self._holder(stored).local_key)
                if holder.edit is not None:
                    raise PendingEditError(
                        f"Record {holder.local_key} holds a pending edit of its own"
                    )
                edit = cast(LocalEdit, stored.edit)
                self._store.update_record(_changed_to(holder, replace(edit, recorded_at=now)))
                # A refused create leaves nothing; a refused change, the synced copy.
                resolved = None if stored.is_create else _resolved(stored, KeepServer(), now)
            else:
                resolved = _resolved(stored, resolution, now)
            if resolved is None:
                self._store.append_entry(
                    stored.scope,
                    FeedEntryKind.CONFLICT_RESOLVED,
                    stored.local_key,
                    stored.object_id,
                    now,
                )
                self._store.delete_record(stored.local_key)
            else:
                self._write(resolved, FeedEntryKind.CONFLICT_RESOLVED, now)

    def resolve_conflicts(
        self, resolver: Callable[[Record], Resolution | None]
    ) -> ResolvedConflicts:
        """Pass every open :term:`conflict` to ``resolver``, and resolve each as it answers.

        The conflicts are read in one read transaction, then ``resolver`` runs
        outside any transaction, so it can take its time, and each answer is
        resolved with :meth:`resolve` in a write transaction of its own.
        ``None`` leaves a record open. A record that changed after it was read
        is skipped, and comes back on the next call. An exception from
        ``resolver`` or :meth:`resolve` stops the loop and keeps every
        resolution before it.

        Args:
            resolver: Your function. It gets each record in conflict, from every
                scope, and returns a resolution or ``None``.

        Returns:
            Which conflicts were resolved, left open, or changed meanwhile.

        Raises:
            TransactionError: If a store transaction is already open in this thread.

        Examples:
            An application that takes the server's copy in every conflict,
            without asking anyone, resolves them all in one call:

            >>> from calendaring_sync import KeepServer
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, room 2", fingerprint="fp-mine")
            >>> theirs = {"a": Changed("v2", "fp-theirs", b"Team meeting, room 3")}
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", theirs))
            >>> replica.resolve_conflicts(lambda record: KeepServer()).resolved == (key,)
            True
            >>> replica.record(key).content
            b'Team meeting, room 3'
        """
        with self._store.transaction(write=False):
            open_conflicts = [
                record
                for scope in self._store.scopes()
                for record in self._store.scope_records(scope.id, states=_CONFLICT)
            ]
        resolved: list[int] = []
        left_open: list[int] = []
        changed: list[int] = []
        for record in open_conflicts:
            resolution = resolver(record)
            if resolution is None:
                left_open.append(record.local_key)
                continue
            try:
                self.resolve(record, resolution)
            except StaleConflictError:
                changed.append(record.local_key)
                continue
            resolved.append(record.local_key)
        return ResolvedConflicts(tuple(resolved), tuple(left_open), tuple(changed))

    def _holder(self, record: Record) -> Record:
        """The one live record holding a refused create's logical key.

        The id the server named comes first; without one, or before that
        object is synced, the key of a synced copy decides, since the server
        refused the create for an object it has. A create sent without a key
        has only the id to go by.

        Raises:
            UnknownHolderError: If no single record holds it.
        """
        conflict, edit = cast(Conflict, record.conflict), cast(LocalEdit, record.edit)
        if conflict.holder is not None:
            named = self._store.find_record(record.scope, conflict.holder)
            if named is not None and named.state is not RecordState.REMOVED:
                return named
        keyed = (
            []
            if edit.logical_key is None
            else self._store.find_by_logical_key(record.scope, edit.logical_key)
        )
        holders = [
            other
            for other in keyed
            if other.logical_key == edit.logical_key and other.state is not RecordState.REMOVED
        ]
        if len(holders) != 1:
            raise UnknownHolderError(
                f"{len(holders)} records hold logical key {edit.logical_key!r}, not one"
            )
        return holders[0]

    def feed_head(self) -> int:
        """Return the number of the last :term:`change feed` entry written, ``0`` for none.

        Start a new :term:`cursor` here after reading every record.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.

        Examples:
            >>> replica.feed_head()
            1
        """
        with self._store.transaction(write=False):
            return self._store.feed_bounds()[1]

    def read_feed(self, after: int = 0, limit: int = FEED_PAGE_SIZE) -> list[FeedEntry]:
        """Return the :term:`change feed` entries after a :term:`cursor`, oldest first.

        :doc:`/how-to/change-feed` shows how to keep a cursor so a crash
        never skips an entry.

        Runs in a read transaction of its own.

        Args:
            after: The number of the last entry already handled.
            limit: The most entries to return, at most
                :data:`~calendaring_sync.constants.FEED_PAGE_SIZE`.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            TrimmedFeedError: If entries after ``after`` were trimmed.
            FeedRangeError: If ``after`` is negative or past the last entry.
            ValueError: If ``limit`` is outside 1 to ``FEED_PAGE_SIZE``.

        Examples:
            >>> [(entry.number, entry.kind) for entry in replica.read_feed(after=0)]
            [(1, <FeedEntryKind.CHANGED: 'changed'>)]
            >>> replica.read_feed(after=replica.feed_head())
            []
        """
        if not 1 <= limit <= FEED_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {FEED_PAGE_SIZE}, not {limit}")
        with self._store.transaction(write=False):
            trimmed = self._checked_feed_bounds(after, "after")
            if after < trimmed:
                raise TrimmedFeedError(
                    f"Cursor {after} is older than the feed, which is trimmed through {trimmed}"
                )
            return self._store.entries(after, limit)

    def trim_feed(self, through: int) -> None:
        """Delete every :term:`change feed` entry numbered at or below ``through``.

        Runs in a write transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            FeedRangeError: If ``through`` is negative or past the last entry.

        Examples:
            >>> replica.trim_feed(replica.feed_head())
            >>> replica.read_feed(after=replica.feed_head())
            []
        """
        with self._store.transaction():
            self._checked_feed_bounds(through, "through")
            self._store.trim_entries(through)

    def purge(self, through: int) -> int:
        """:term:`Purge <purge>` removed records whose last feed entry is at or below ``through``.

        Their feed entries stay until trimmed.

        Runs in a write transaction of its own.

        Args:
            through: The number of a :term:`change feed` entry, such as the
                oldest :term:`cursor` your application still reads from, so
                every part of it has seen these removals.

        Returns:
            How many records were purged.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            FeedRangeError: If ``through`` is negative or past the last entry.

        Examples:
            >>> from calendaring_sync import Removed
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", {"a": Removed()}))
            >>> replica.purge(replica.feed_head())
            1
            >>> replica.records("work")
            []
        """
        with self._store.transaction():
            self._checked_feed_bounds(through, "through")
            records = self._store.removed_records(through)
            for record in records:
                self._store.delete_record(record.local_key)
        return len(records)

    def _checked_feed_bounds(self, number: int, name: str) -> int:
        """Check a feed number against the last entry, and return where the feed is trimmed."""
        trimmed, head = self._store.feed_bounds()
        if not 0 <= number <= head:
            raise FeedRangeError(
                f"{name} must be between 0 and the last entry, {head}, not {number}"
            )
        return trimmed


# Last, since session.py imports Replica from this module.
from calendaring_sync.session import Session  # noqa: E402
