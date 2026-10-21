# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The core's entry point: sessions, pushes, local edits, conflicts, and the :term:`change feed`.

Every sync rule lives here, on top of any
:class:`~calendaring_sync.store.base.StateStore`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
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
    OpenConflictError,
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
)
from calendaring_sync.objects._data import REMOTE, data
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.push import (
    Applied,
    Gone,
    LogicalKeyTaken,
    PushOutcome,
    Rejected,
    Unknown,
)
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.resolution import (
    Delete,
    KeepEdit,
    Merge,
    Resolution,
    TakeServer,
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


def _new_record(scope_id: str, object_id: str | None) -> Record:
    """A record the store doesn't hold yet, with nothing synced; the caller fills it in."""
    return Record(
        local_key=0,
        scope=scope_id,
        object_id=object_id,
        state=RecordState.SYNCED,
        version=None,
        fingerprint=None,
        content=None,
        containers=frozenset(),
        logical_key=None,
        synced_at=None,
        last_entry=0,
    )


def _with_copy(
    record: Record, copy: object, version: str | None, now: datetime, *, prefix: str = ""
) -> Record:
    """The record whose synced side is a copy of its object, at ``version``.

    ``copy`` is a change from the server, your edit once it's applied, or a
    conflict's server side, whose fields carry the :data:`REMOTE` prefix.
    """
    fingerprint, content, containers, logical_key = data(copy, prefix)
    return replace(
        record,
        version=version,
        fingerprint=fingerprint,
        content=content,
        containers=containers,
        logical_key=logical_key,
        synced_at=now,
    )


def _synced(
    record: Record, copy: object, version: str | None, now: datetime, *, prefix: str = ""
) -> Record:
    """The record synced to a copy of its object, with no edit or conflict left."""
    return replace(
        _with_copy(record, copy, version, now, prefix=prefix),
        state=RecordState.SYNCED,
        edit=None,
        conflict=None,
    )


def _removed(record: Record, now: datetime) -> Record:
    """The record once its object is gone from the server, with nothing left to push."""
    return replace(record, state=RecordState.REMOVED, edit=None, conflict=None, synced_at=now)


def _check_resolution(record: Record, resolution: Resolution) -> None:
    """Raise ``ValueError`` unless ``record`` is in conflict and ``resolution`` fits its kind."""
    if record.state is not RecordState.CONFLICT or record.conflict is None:
        raise ValueError("resolve() takes a record in conflict")
    refused_create = record.conflict.kind is ConflictKind.LOGICAL_KEY_TAKEN
    if isinstance(resolution, KeepEdit) and refused_create:
        raise ValueError("A create refused for its logical key can't be pushed again unchanged")
    if isinstance(resolution, UpdateHolder) and not refused_create:
        raise ValueError("Only a create refused for its logical key goes to the key's holder")


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

    The synced side becomes the server's current copy, so whatever is pushed
    next goes out against the server's current version.
    """
    conflict, edit = cast(Conflict, record.conflict), cast(LocalEdit, record.edit)
    on_server = conflict.remote_content is not None
    if not on_server and isinstance(resolution, (TakeServer, Delete)):
        # The server doesn't have the object: a create it never had leaves nothing.
        return None if record.is_create else _removed(record, now)
    base = _synced(record, conflict, conflict.remote_version, now, prefix=REMOTE)
    # Over a copy the server has, an edit is a change; with none, a create.
    kind = EditKind.CHANGE if on_server else EditKind.CREATE
    match resolution:
        case TakeServer():
            return base
        case KeepEdit():
            return _edited(base, edit if edit.kind is EditKind.DELETE else replace(edit, kind=kind))
        case Merge():
            merged = LocalEdit(
                kind,
                resolution.content,
                resolution.fingerprint,
                resolution.containers,
                resolution.logical_key,
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


def _check_outcome(claimed_keys: set[int], claimed: Record, outcome: PushOutcome) -> None:
    """Raise ``ValueError`` unless the outcome is one a push of the claimed edit can have."""
    if (
        claimed.local_key not in claimed_keys
        or claimed.state is not RecordState.IN_FLIGHT
        or claimed.edit is None
    ):
        raise ValueError("settle() takes a record this session's claim() returned")
    kind = claimed.edit.kind
    match outcome:
        case Gone() if kind is EditKind.CREATE:
            raise ValueError("A create can't be gone: the server never had the object")
        case LogicalKeyTaken() if kind is not EditKind.CREATE:
            raise ValueError(f"Only a create can be refused for its logical key, not a {kind}")
        case Applied(object_id=str()) if kind is EditKind.DELETE:
            raise ValueError("An applied delete has no object id to report")
        case Applied(object_id=None) if kind is EditKind.CREATE and claimed.object_id is None:
            raise ValueError("An applied create needs the object id the server gave it")


class SessionState(StrEnum):
    """Where a :term:`session` is."""

    #: Nothing ingested yet.
    IDLE = "idle"
    #: A :term:`truncated page` was ingested, and more pages follow.
    INGESTING = "ingesting"
    #: The last page was ingested, and the token is final.
    INGESTED = "ingested"
    #: A :term:`push` was claimed; no more pages can be ingested.
    PUSHING = "pushing"


@check_argument_types
class Session:
    """One sync of one :term:`scope`, opened with :meth:`Replica.session`.

    A session :term:`ingests <ingest>` every page first, then the
    :term:`adapter` pushes your :term:`pending edits <pending edit>`: for each
    record :meth:`to_push` lists, it :meth:`claims <claim>` it, sends it, and
    :meth:`settles <settle>` it with how the push ended.

    Every method raises :class:`TypeError` for an argument of the wrong
    type, before it changes anything.

    Examples:
        The adapter pushes an edit after ingesting what changed on the server,
        here nothing:

        >>> from calendaring_sync import Applied
        >>> key = replica.find_record("work", "a").local_key
        >>> replica.change(key, b"Team meeting, moved", fingerprint="fp-a2")
        >>> with replica.session(
        ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
        ... ) as session:
        ...     session.ingest(ChangeSet("work", "t1", "t2", {}))
        ...     for record in session.to_push():
        ...         claimed = session.claim(record.local_key)
        ...         # The adapter sends claimed.edit to the server here.
        ...         session.settle(claimed, Applied())
        True
        >>> replica.record(key).state, replica.record(key).content
        (<RecordState.SYNCED: 'synced'>, b'Team meeting, moved')
    """

    def __init__(
        self, replica: Replica, scope_id: str, *, guarded_by_version: bool, unique_logical_key: bool
    ) -> None:
        self._replica = replica
        self._scope_id = scope_id
        self._guarded_by_version = guarded_by_version
        self._unique_logical_key = unique_logical_key
        self._state = SessionState.IDLE
        self._listing: bool | None = None
        self._mentioned: set[str] = set()
        self._closed = False
        # The local keys this session claimed, the only ones it settles.
        self._claimed: set[int] = set()

    @property
    def scope_id(self) -> str:
        """The id of the session's scope."""
        return self._scope_id

    @property
    def state(self) -> SessionState:
        """Where the session is."""
        return self._state

    def ingest(self, change_set: ChangeSet) -> None:
        """:term:`Ingest` one change set, or one page of it, in one transaction.

        The core applies each object in it: a new object gets a record, a
        changed one gets the new data, and a removed one is marked removed.
        An object whose :term:`fingerprint`, containers and logical key
        didn't change only gets its version and the server's content, and isn't
        reported. A :term:`truncated page` of an :term:`incremental change set`
        moves the token to its :term:`checkpoint`. A :term:`full listing`
        moves the token only on its last page, which also removes every
        record the listing never mentioned. A complete change set, or the
        last page, also settles every :term:`push` still :term:`in flight`
        from what the server reported, and one it doesn't mention is
        pending again. If anything fails, nothing changes.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            StaleChangeSetError: If the change set was fetched from a token
                the scope no longer has, or is a later page of a listing with
                another token, such as one another session has replaced. The
                session goes back to idle, so the adapter can fetch again and
                ingest from the start in the same session.
            SessionError: If the session has ended, already ingested its last
                page, has started pushing, or the page is a listing page in an
                incremental session or the other way round.
            ValueError: If the change set is for another scope, an
                incremental change set arrives for a scope with no token, or
                an ``Unchanged`` entry is outside a listing or names a record
                with no version.
        """
        if change_set.scope != self._scope_id:
            raise ValueError(
                f"Change set for {change_set.scope!r} in a session for {self._scope_id!r}"
            )
        if self._closed:
            raise SessionError("The session has ended")
        if self._state is SessionState.INGESTED:
            raise SessionError("The session already ingested its last page")
        if self._state is SessionState.PUSHING:
            raise SessionError("The session already pushed, and a change set can't come after that")
        if self._listing is not None and self._listing != change_set.full_listing:
            raise SessionError("One session's pages are all listing pages or all incremental")
        try:
            self._mentioned = self._replica._ingest(
                change_set,
                first_page=self._state is SessionState.IDLE,
                mentioned=self._mentioned,
                unique_logical_key=self._unique_logical_key,
            )
        except StaleChangeSetError:
            self._restart()
            raise
        self._listing = change_set.full_listing
        self._state = SessionState.INGESTING if change_set.truncated else SessionState.INGESTED

    def to_push(self) -> list[Record]:
        """Return the :term:`pending edits <pending edit>` ready to :term:`push`, by local key.

        With ``guarded_by_version``, a change or delete of a record with no
        :term:`version` waits for an ingest that gives it one, and isn't
        listed. Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            SessionError: If the session hasn't ingested its last page, or has ended.

        Examples:
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, moved", fingerprint="fp-a2")
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", {}))
            ...     [record.local_key for record in session.to_push()] == [key]
            True
        """
        self._check_ready()
        return self._replica._to_push(self._scope_id, guarded=self._guarded_by_version)

    def claim(self, local_key: int, *, object_id: str | None = None) -> Record | None:
        """Mark a :term:`pending edit` :term:`in flight`, so no other process pushes it.

        Send the record this returns, then report how the push ended with
        :meth:`settle`. Runs in a write transaction of its own; when two
        processes claim one record, only one gets it.

        Args:
            local_key: The record to push.
            object_id: For a create the server takes ids for, the id it's
                about to be sent under. It's stored with the claim, so the
                create is recognized by it even if the answer never arrives.
                An id a removed record holds passes to this one.

        Returns:
            The claimed record, or ``None`` when it no longer holds a
            pending edit to push: another process claimed it, the edit was
            discarded or resolved, or it waits for a version.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            SessionError: If the session hasn't ingested its last page, or has ended.
            DuplicateObjectError: If a record that isn't removed holds ``object_id``.
            ValueError: If the record belongs to another scope, ``object_id``
                is given for a record that has one, or a string holds a
                :ref:`lone surrogate <lone-surrogates>`.

        Examples:
            A second claim of the same record gets nothing:

            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, moved", fingerprint="fp-a2")
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", {}))
            ...     claimed = session.claim(key)
            ...     claimed.state, session.claim(key)
            (<RecordState.IN_FLIGHT: 'in_flight'>, None)
        """
        check_text(object_id=object_id)
        self._check_ready()
        claimed = self._replica._claim(
            self._scope_id, local_key, object_id, guarded=self._guarded_by_version
        )
        if claimed is not None:
            self._state = SessionState.PUSHING
            self._claimed.add(local_key)
        return claimed

    def settle(self, claimed: Record, outcome: PushOutcome) -> bool:
        """Record how the :term:`push` of a claimed record ended.

        Runs in a write transaction of its own, and writes a :term:`change
        feed` entry for every outcome but ``Unknown``. If another process
        already settled this push, for example by ingesting while it was
        in flight, the outcome only counts while the record still holds the
        same edit and no one has claimed it again.

        Args:
            claimed: The record :meth:`claim` returned.
            outcome: How the push ended.

        Returns:
            ``True`` if the store now agrees with the outcome, ``False`` if
            the push had already been settled another way and nothing was
            written.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            SessionError: If the session has ended.
            DuplicateObjectError: If ``Applied`` names an id a record that
                isn't removed holds.
            ValueError: If ``claimed`` isn't a record this session's
                :meth:`claim` returned, or the outcome is one no push of its
                edit can have: ``Gone`` for a create, ``LogicalKeyTaken`` for
                anything but a create, ``Applied`` with an id for a delete, or
                ``Applied`` without one for a create that has none.

        Examples:
            The server refused the write, so the edit is pending again:

            >>> from calendaring_sync import Rejected
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, moved", fingerprint="fp-a2")
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", {}))
            ...     session.settle(session.claim(key), Rejected())
            True
            >>> replica.record(key).state
            <RecordState.PENDING: 'pending'>
        """
        _check_outcome(self._claimed, claimed, outcome)
        if self._closed:
            raise SessionError("The session has ended")
        return self._replica._settle(claimed, outcome)

    def _check_ready(self) -> None:
        """Raise unless the session may push: its last page is ingested and it hasn't ended."""
        if self._closed:
            raise SessionError("The session has ended")
        if self._state not in (SessionState.INGESTED, SessionState.PUSHING):
            raise SessionError("A session pushes only after it ingested its last page")

    def _restart(self) -> None:
        """Go back to idle, dropping a half-done listing."""
        self._state = SessionState.IDLE
        self._listing = None
        self._mentioned = set()

    def _end(self) -> None:
        """Close the session when its ``with`` block ends."""
        self._closed = True
        self._restart()


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
    store's errors:
    :class:`~calendaring_sync.error.StoreBusyError` when another process or
    thread kept the store busy, which is safe to retry,
    :class:`~calendaring_sync.error.StoreError` when the store can't read or
    write its data, and :class:`~calendaring_sync.error.UnexpectedStoreError`
    for a bug in the store. None of them changes anything.

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

        Opening the session and each :meth:`Session.ingest` run in a write
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
            ValueError: If a string holds a :ref:`lone surrogate <lone-surrogates>`.

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
                raise ValueError(f"Scope {scope.id!r} has no token, so it needs a full listing")
            if change_set.full_listing:
                scope = self._check_listing_page(scope, change_set, first_page)
            claimed = self._claimed_creates(scope.id) if unique_logical_key else {}
            for object_id, change in change_set.changes.items():
                if not change_set.full_listing and isinstance(change, Unchanged):
                    raise ValueError(f"Unchanged object {object_id!r} outside a full listing")
                self._apply(scope.id, object_id, change, now, claimed)
                if change_set.full_listing:
                    mentioned.add(object_id)
            if change_set.full_listing and not change_set.truncated:
                self._remove_unmentioned(scope.id, mentioned, now)
                scope = replace(
                    scope, token=change_set.token, listing_token=None, last_full_listing_at=now
                )
            elif not change_set.full_listing:
                scope = replace(scope, token=change_set.token)
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
    ) -> None:
        """Apply one object's change to the store, classified against its local edit.

        ``claimed`` maps a logical key to the one create in flight without
        an object id that holds it; a new object with that key is taken as
        that create, and leaves the map.
        """
        record = self._store.find_record(scope_id, object_id)
        if isinstance(change, Unchanged):
            if record is None or record.server_version is None:
                raise ValueError(
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
        if live is not None and live.conflict is not None and same_remote(live.conflict, change):
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
                self._write(_removed(live, now), settled or FeedEntryKind.REMOVED, now)
            case ChangeClass.UNCHANGED | ChangeClass.LOCAL_ONLY | ChangeClass.DELETED_LOCAL if (
                live is not None and (isinstance(change, Changed) or was_conflict or in_flight)
            ):
                # The server's copy holds the synced data, so only its version and
                # content move; a conflict this settles leaves the edit to be pushed,
                # and so does a push in flight that hasn't landed.
                if isinstance(change, Changed):
                    live = _with_copy(live, change, change.version, now)
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
        known = _new_record(scope_id, object_id) if record is None else record
        synced = replace(_synced(known, change, change.version, now), object_id=object_id)
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

    def _remove_unmentioned(self, scope_id: str, mentioned: set[str], now: datetime) -> None:
        """Remove every record with an object id the finished listing never mentioned."""
        for record in self._store.scope_records(scope_id):
            if record.object_id is not None and record.object_id not in mentioned:
                self._apply(scope_id, record.object_id, Removed(), now, {})

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

        A record matches if its synced side or its :term:`pending edit` has
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
        Build ``fingerprint``, ``containers`` and ``logical_key`` with the
        :term:`adapter`'s function for them, the same one it uses for the
        server's changes; built any other way, an identical copy on the
        server looks different.

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
            ValueError: If a string holds a :ref:`lone surrogate <lone-surrogates>`.

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
            created = replace(
                _new_record(scope_id, object_id), state=RecordState.PENDING, edit=edit
            )
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

        A change back to the :term:`synced content` cancels the edit, a
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
            ValueError: If a string holds a :ref:`lone surrogate <lone-surrogates>`.

        Examples:
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, moved", fingerprint="fp-a2")
            >>> replica.record(key).edit.content
            b'Team meeting, moved'

            A change back to the synced content cancels the edit:

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
                raise ValueError(f"Record {local_key} belongs to scope {record.scope!r}")
            if object_id is not None and record.object_id is not None:
                raise ValueError(f"Record {local_key} already has object id {record.object_id!r}")
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
                    self._write(_removed(record, now), FeedEntryKind.PUSHED, now)
                case Applied(object_id=renamed):
                    if renamed is not None and renamed != record.object_id:
                        self._free_object_id(record.scope, renamed, record.local_key)
                    # The server may store the edit rewritten, so no version is kept.
                    pushed = replace(
                        _synced(record, edit, None, now), object_id=renamed or record.object_id
                    )
                    self._write(pushed, FeedEntryKind.PUSHED, now)
                case Gone():
                    self._open_conflict(record, ConflictKind.REMOVED_REMOTELY, None, now)
                case _:
                    # LogicalKeyTaken, the one outcome left.
                    self._open_conflict(record, ConflictKind.LOGICAL_KEY_TAKEN, outcome.holder, now)
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

    def records(self, scope_id: str) -> list[Record]:
        """Return every record in a scope, removed ones included, ordered by local key.

        Runs in a read transaction of its own.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            UnknownScopeError: If there's no scope with this id.

        Examples:
            >>> [record.object_id for record in replica.records("work")]
            ['a']
        """
        return self._read_in_scope(scope_id, lambda: self._store.scope_records(scope_id))

    def known_versions(self, scope_id: str) -> dict[str, str]:
        """Return the version of the server's copy of every object the store knows one for.

        For a record in :term:`conflict` that's the server's side. An
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

        Anything but the server's side becomes a :term:`pending edit` over the
        server's current copy, so the next sync pushes it against the server's
        current version. Writes a ``CONFLICT_RESOLVED`` entry to the
        :term:`change feed`. Runs in a write transaction of its own.

        Args:
            record: The record in conflict, as your application read it.
            resolution: What to do with it.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            StaleConflictError: If the conflict changed since ``record`` was
                read: it ended, or its edit or the server's side moved on. A
                move of the server's version alone doesn't count.
            UnknownHolderError: For ``UpdateHolder``, if the server named no
                record that's still there, and no single record holds the
                refused create's logical key, or it was sent without one.
            OpenConflictError: For ``UpdateHolder``, if the holder is in conflict.
            PushInFlightError: For ``UpdateHolder``, if the holder's push is in flight.
            PendingEditError: For ``UpdateHolder``, if the holder holds a
                pending edit of its own, which this would replace.
            ValueError: If ``record`` isn't in conflict, or the resolution
                doesn't fit its kind: ``KeepEdit`` for a create the server
                refused for its logical key, or ``UpdateHolder`` for any other.

        Examples:
            >>> from calendaring_sync import KeepEdit
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
            >>> replica.resolve(record, KeepEdit())
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
                resolved = None
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

    def resolve_conflicts(self, resolver: Callable[[Record], Resolution | None]) -> int:
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
            How many conflicts were resolved.

        Raises:
            TransactionError: If a store transaction is already open in this thread.

        Examples:
            >>> from calendaring_sync import TakeServer
            >>> key = replica.find_record("work", "a").local_key
            >>> replica.change(key, b"Team meeting, room 2", fingerprint="fp-mine")
            >>> theirs = {"a": Changed("v2", "fp-theirs", b"Team meeting, room 3")}
            >>> with replica.session(
            ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
            ... ) as session:
            ...     session.ingest(ChangeSet("work", "t1", "t2", theirs))
            >>> replica.resolve_conflicts(lambda record: TakeServer())
            1
            >>> replica.record(key).content
            b'Team meeting, room 3'
        """
        with self._store.transaction(write=False):
            open_conflicts = [
                record
                for scope in self._store.scopes()
                for record in self._store.scope_records(scope.id, states=_CONFLICT)
            ]
        resolved = 0
        for record in open_conflicts:
            resolution = resolver(record)
            if resolution is None:
                continue
            try:
                self.resolve(record, resolution)
            except StaleConflictError:
                continue
            resolved += 1
        return resolved

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

        Save the new cursor only after handling the entries, so a crash in
        between reads them again instead of skipping them.

        Runs in a read transaction of its own.

        Args:
            after: The number of the last entry already handled.
            limit: The most entries to return, at most
                :data:`~calendaring_sync.constants.FEED_PAGE_SIZE`.

        Raises:
            TransactionError: If a store transaction is already open in this thread.
            TrimmedFeedError: If entries after ``after`` were trimmed.
            ValueError: If ``after`` is negative or past the last entry,
                or ``limit`` is outside 1 to ``FEED_PAGE_SIZE``.

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
            ValueError: If ``through`` is negative or past the last entry.

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
            ValueError: If ``through`` is negative or past the last entry.

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
            raise ValueError(f"{name} must be between 0 and the last entry, {head}, not {number}")
        return trimmed
