# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The core's entry point: sessions, ingests, local edits, and the :term:`change feed`.

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
from typing import TypeVar

from calendaring_sync._classify import ChangeClass, classify, same_data, same_remote
from calendaring_sync._text import check_text
from calendaring_sync.constants import FEED_PAGE_SIZE
from calendaring_sync.error import (
    EditRefusedError,
    OpenConflictError,
    RemovedRecordError,
    ScopeMismatchError,
    SessionError,
    StaleChangeSetError,
    TrimmedFeedError,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore

_T = TypeVar("_T")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _server_version(record: Record) -> str | None:
    """The version of the server's copy as the store knows it, ``None`` if it knows none."""
    if record.state is RecordState.REMOVED:
        return None
    if record.conflict is not None:
        return record.conflict.remote_version
    return record.version


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


def _is_create(record: Record) -> bool:
    """Whether the record is an object you created that the server doesn't have yet."""
    return record.edit is not None and record.edit.kind is EditKind.CREATE


def _conflict(kind: ConflictKind, change: Changed | Removed, opened_at: datetime) -> Conflict:
    """A conflict whose server side is what the change reported."""
    if isinstance(change, Removed):
        return Conflict(kind, None, None, None, frozenset(), None, opened_at)
    return Conflict(
        kind,
        change.version,
        change.fingerprint,
        change.content,
        change.containers,
        change.logical_key,
        opened_at,
    )


class SessionState(StrEnum):
    """Where a :term:`session` is."""

    #: Nothing ingested yet.
    IDLE = "idle"
    #: A :term:`truncated page` was ingested, and more pages follow.
    INGESTING = "ingesting"
    #: The last page was ingested, and the token is final.
    INGESTED = "ingested"


class Session:
    """One sync of one :term:`scope`, opened with :meth:`Replica.session`."""

    def __init__(self, replica: Replica, scope_id: str) -> None:
        self._replica = replica
        self._scope_id = scope_id
        self._state = SessionState.IDLE
        self._listing: bool | None = None
        self._mentioned: set[str] = set()
        self._closed = False

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
        didn't change only gets its version and the server's text, and isn't
        reported. A :term:`truncated page` of an :term:`incremental change set`
        moves the token to its :term:`checkpoint`. A :term:`full listing`
        moves the token only on its last page, which also removes every
        record the listing never mentioned. If anything fails, nothing
        changes.

        Raises:
            StaleChangeSetError: If the change set was fetched from a token
                the scope no longer has, or is a later page of a listing with
                another token, such as one another session has replaced. The
                session goes back to idle, so the adapter can fetch again and
                ingest from the start in the same session.
            SessionError: If the session has ended, already ingested its last
                page, or the page is a listing page in an incremental session
                or the other way round.
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
        if self._listing is not None and self._listing != change_set.full_listing:
            raise SessionError("One session's pages are all listing pages or all incremental")
        try:
            self._mentioned = self._replica._ingest(
                change_set, first_page=self._state is SessionState.IDLE, mentioned=self._mentioned
            )
        except StaleChangeSetError:
            self._restart()
            raise
        self._listing = change_set.full_listing
        self._state = SessionState.INGESTING if change_set.truncated else SessionState.INGESTED

    def _restart(self) -> None:
        """Go back to idle, dropping a half-done listing."""
        self._state = SessionState.IDLE
        self._listing = None
        self._mentioned = set()

    def _end(self) -> None:
        """Close the session when its ``with`` block ends."""
        self._closed = True
        self._restart()


class Replica:
    """The sync state of every :term:`scope` in one :term:`store`.

    Any method can also raise the store's errors:
    :class:`~calendaring_sync.error.StoreBusyError` when another process
    kept the store busy, which is safe to retry,
    :class:`~calendaring_sync.error.StoreError` when the store can't read or
    write its data, and :class:`~calendaring_sync.error.UnexpectedStoreError`
    for a bug in the store. None of them changes anything.

    Args:
        store: Where the state is kept.
        clock: Returns the current time for every timestamp. Defaults to
            the current time in UTC.
    """

    def __init__(self, store: StateStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._clock = clock or _utc_now
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = Lock()

    @contextmanager
    def session(
        self, scope_id: str, *, fingerprint_scheme: str, media_type: str
    ) -> Iterator[Session]:
        """Open a :term:`session` for one scope, for the length of the ``with`` block.

        The first session creates the scope. A different
        :term:`fingerprint scheme` from the stored one resets the scope and
        clears every record's version and fingerprint, so the next
        :term:`full listing` fetches every object again. Objects whose text
        is the same as before aren't reported as changed.

        Args:
            scope_id: The scope's id, chosen by the :term:`adapter`.
            fingerprint_scheme: The name of the adapter's fingerprint scheme.
            media_type: The media type of the scope's :term:`content`.

        Raises:
            SessionError: If this replica already has a session open for the scope.
            ScopeMismatchError: If the scope's content has another media type.
            ValueError: If a string isn't valid text.
        """
        check_text("A session", (scope_id, fingerprint_scheme, media_type))
        session = Session(self, scope_id)
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

    def _ingest(self, change_set: ChangeSet, *, first_page: bool, mentioned: set[str]) -> set[str]:
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
            for object_id, change in change_set.changes.items():
                if not change_set.full_listing and isinstance(change, Unchanged):
                    raise ValueError(f"Unchanged object {object_id!r} outside a full listing")
                self._apply(scope.id, object_id, change, now)
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

    def _apply(self, scope_id: str, object_id: str, change: Change, now: datetime) -> None:
        """Apply one object's change to the store, classified against its local edit."""
        record = self._store.find_record(scope_id, object_id)
        if isinstance(change, Unchanged):
            if record is None or _server_version(record) is None:
                raise ValueError(
                    f"Object {object_id!r} is reported unchanged, "
                    "but the store has no version for it"
                )
            return
        if record is not None and record.state is RecordState.IN_FLIGHT:
            # How an ingest settles a push in flight is decided with pushes.
            return
        live = None if record is None or record.state is RecordState.REMOVED else record
        if live is not None and live.conflict is not None and same_remote(live.conflict, change):
            if isinstance(change, Changed):
                conflict = replace(
                    live.conflict,
                    remote_version=change.version,
                    remote_fingerprint=change.fingerprint,
                    remote_content=change.content,
                )
                self._store.update_record(replace(live, conflict=conflict))
            return
        edit = None if live is None else live.edit
        synced = live if live is not None and live.content is not None else None
        change_class, kind = classify(synced, change, edit)
        was_conflict = live is not None and live.conflict is not None
        # A record leaving a conflict is reported as the conflict ending.
        settled = FeedEntryKind.CONFLICT_RESOLVED if was_conflict else None
        if kind is not None and live is not None:
            opened_at = now if live.conflict is None else live.conflict.opened_at
            self._write(
                replace(
                    live, state=RecordState.CONFLICT, conflict=_conflict(kind, change, opened_at)
                ),
                FeedEntryKind.CONFLICT_UPDATED if was_conflict else FeedEntryKind.CONFLICT_OPENED,
                now,
            )
        elif isinstance(change, Changed) and (
            change_class is ChangeClass.REMOTE_ONLY
            or (change_class is ChangeClass.UNCHANGED and edit is not None)
        ):
            synced_record = Record(
                local_key=0 if record is None else record.local_key,
                scope=scope_id,
                object_id=object_id,
                state=RecordState.SYNCED,
                version=change.version,
                fingerprint=change.fingerprint,
                content=change.content,
                containers=change.containers,
                logical_key=change.logical_key,
                synced_at=now,
                last_entry=0 if record is None else record.last_entry,
            )
            if record is None:
                synced_record = replace(
                    synced_record, local_key=self._store.insert_record(synced_record)
                )
            self._write(
                synced_record,
                settled or FeedEntryKind.CHANGED,
                now,
            )
        elif change_class is ChangeClass.DELETED_REMOTE and live is not None:
            self._write(
                replace(live, state=RecordState.REMOVED, edit=None, conflict=None, synced_at=now),
                settled or FeedEntryKind.REMOVED,
                now,
            )
        elif live is not None and (isinstance(change, Changed) or was_conflict):
            # The server's copy holds the synced data, so only its version and
            # text move; a conflict this settles leaves the edit to be pushed.
            if isinstance(change, Changed):
                live = replace(
                    live,
                    version=change.version,
                    fingerprint=change.fingerprint,
                    content=change.content,
                    synced_at=now,
                )
            if was_conflict:
                self._write(
                    replace(live, state=RecordState.PENDING, conflict=None),
                    FeedEntryKind.CONFLICT_RESOLVED,
                    now,
                )
            else:
                self._store.update_record(live)

    def _remove_unmentioned(self, scope_id: str, mentioned: set[str], now: datetime) -> None:
        """Remove every record with an object id the finished listing never mentioned."""
        for record in self._store.scope_records(scope_id):
            if record.object_id is not None and record.object_id not in mentioned:
                self._apply(scope_id, record.object_id, Removed(), now)

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

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        with self._store.transaction(write=False):
            return self._existing_scope(scope_id)

    def scopes(self) -> list[Scope]:
        """Return every scope, ordered by id."""
        with self._store.transaction(write=False):
            return self._store.scopes()

    def record(self, local_key: int) -> Record | None:
        """Return the record with this :term:`local key`, or ``None``."""
        with self._store.transaction(write=False):
            return self._store.get_record(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Return the scope's record with this :term:`object id`, or ``None``.

        A removed record is returned until it's :term:`purged <purge>`.

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        return self._read_in_scope(scope_id, lambda: self._store.find_record(scope_id, object_id))

    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        """Return the scope's records with this :term:`logical key`, ordered by local key.

        A record matches if its synced side or its :term:`pending edit` has
        the key. Several records can share one, and removed records are
        included until they're :term:`purged <purge>`.

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        return self._read_in_scope(
            scope_id, lambda: self._store.find_by_logical_key(scope_id, logical_key)
        )

    def create(
        self,
        scope_id: str,
        content: str,
        *,
        fingerprint: str,
        containers: frozenset[str] = frozenset(),
        logical_key: str | None = None,
        object_id: str | None = None,
    ) -> int:
        """Record the creation of an object, and return its :term:`local key`.

        The record is pending until a :term:`push` creates the object on
        the server. Build ``fingerprint``, ``containers`` and
        ``logical_key`` with the :term:`adapter`'s function for them, the
        same one it uses for the server's changes; built any other way, an
        identical copy on the server looks different.

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
            UnknownScopeError: If there's no scope with this id.
            DuplicateObjectError: If another record in the scope has the
                object id.
            ValueError: If a string isn't valid text.
        """
        check_text(
            "A create", (scope_id, content, fingerprint, logical_key, object_id, *containers)
        )
        with self._store.transaction():
            self._existing_scope(scope_id)
            edit = LocalEdit(
                EditKind.CREATE, content, fingerprint, containers, logical_key, self._clock()
            )
            return self._store.insert_record(
                Record(
                    local_key=0,
                    scope=scope_id,
                    object_id=object_id,
                    state=RecordState.PENDING,
                    version=None,
                    fingerprint=None,
                    content=None,
                    containers=frozenset(),
                    logical_key=None,
                    synced_at=None,
                    last_entry=0,
                    edit=edit,
                )
            )

    def change(
        self,
        local_key: int,
        content: str,
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

        Raises:
            UnknownRecordError: If there's no record with this local key.
            OpenConflictError: If the record holds an open :term:`conflict`.
            RemovedRecordError: If the record was removed from the server.
            EditRefusedError: If the record's :term:`push` is :term:`in flight`.
            ValueError: If a string isn't valid text.
        """
        check_text("A change", (content, fingerprint, logical_key, *containers))
        with self._editing(local_key) as record:
            created = _is_create(record)
            edit = LocalEdit(
                EditKind.CREATE if created else EditKind.CHANGE,
                content,
                fingerprint,
                containers,
                logical_key,
                self._clock(),
            )
            if not created and same_data(edit, record):
                self._drop_edit(record)
            else:
                self._store.update_record(replace(record, state=RecordState.PENDING, edit=edit))

    def delete(self, local_key: int) -> None:
        """Record the deletion of an object.

        Deleting an object you created that the server doesn't have yet
        removes its record, leaving nothing to push.

        Raises:
            UnknownRecordError: If there's no record with this local key.
            OpenConflictError: If the record holds an open :term:`conflict`.
            RemovedRecordError: If the record was removed from the server.
            EditRefusedError: If the record's :term:`push` is :term:`in flight`.
        """
        with self._editing(local_key) as record:
            if _is_create(record):
                self._drop_edit(record)
                return
            edit = LocalEdit(EditKind.DELETE, None, None, frozenset(), None, self._clock())
            self._store.update_record(replace(record, state=RecordState.PENDING, edit=edit))

    def discard(self, local_key: int) -> None:
        """Discard an object's :term:`pending edit`, so it matches the server's copy again.

        Discarding a create removes its record. A record with no edit is
        left as it is.

        Raises:
            UnknownRecordError: If there's no record with this local key.
            OpenConflictError: If the record holds an open :term:`conflict`.
            RemovedRecordError: If the record was removed from the server.
            EditRefusedError: If the record's :term:`push` is :term:`in flight`.
        """
        with self._editing(local_key) as record:
            if record.edit is not None:
                self._drop_edit(record)

    def _drop_edit(self, record: Record) -> None:
        """Leave the record as the server has it: synced, or gone if the server never had it."""
        if _is_create(record):
            self._store.delete_record(record.local_key)
        else:
            self._store.update_record(replace(record, state=RecordState.SYNCED, edit=None))

    @contextmanager
    def _editing(self, local_key: int) -> Iterator[Record]:
        """Open the write transaction an edit runs in, and yield the record it edits."""
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
            raise EditRefusedError(f"Record {local_key} has a push in flight")
        return record

    def records(self, scope_id: str) -> list[Record]:
        """Return every record in a scope, removed ones included, ordered by local key.

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        return self._read_in_scope(scope_id, lambda: self._store.scope_records(scope_id))

    def known_versions(self, scope_id: str) -> dict[str, str]:
        """Return the version of the server's copy of every object the store knows one for.

        For a record in :term:`conflict` that's the server's side. An
        :term:`adapter` compares these with the server's during a
        :term:`full listing`, and reports objects whose version matches as
        ``Unchanged`` instead of fetching them. Removed records aren't included.

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        return {
            record.object_id: version
            for record in self.records(scope_id)
            if record.object_id is not None and (version := _server_version(record)) is not None
        }

    def reset(self, scope_id: str) -> None:
        """:term:`Reset <reset>` a scope, so its next sync is a :term:`full listing`.

        Records keep their state.

        Raises:
            UnknownScopeError: If there's no scope with this id.
            SessionError: If a session for the scope is open.
        """
        with self._without_session(scope_id), self._store.transaction():
            scope = self._existing_scope(scope_id)
            self._store.put_scope(replace(scope, token=None, listing_token=None))

    def drop_scope(self, scope_id: str) -> DroppedScope:
        """:term:`Drop <drop>` a scope and every record in it.

        Returns:
            The records holding local work that the drop discarded.

        Raises:
            UnknownScopeError: If there's no scope with this id.
            SessionError: If a session for the scope is open.
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

    def feed_head(self) -> int:
        """Return the number of the last :term:`change feed` entry written, ``0`` for none.

        Start a new :term:`cursor` here after reading every record.
        """
        with self._store.transaction(write=False):
            return self._store.feed_bounds()[1]

    def read_feed(self, after: int = 0, limit: int = FEED_PAGE_SIZE) -> list[FeedEntry]:
        """Return the :term:`change feed` entries after a :term:`cursor`, oldest first.

        Save the new cursor only after handling the entries, so a crash in
        between reads them again instead of skipping them.

        Args:
            after: The number of the last entry already handled.
            limit: The most entries to return, at most
                :data:`~calendaring_sync.constants.FEED_PAGE_SIZE`.

        Raises:
            TrimmedFeedError: If entries after ``after`` were trimmed.
            ValueError: If ``after`` is negative or past the last entry,
                or ``limit`` is outside 1 to ``FEED_PAGE_SIZE``.
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

        Raises:
            ValueError: If ``through`` is negative or past the last entry.
        """
        with self._store.transaction():
            self._checked_feed_bounds(through, "through")
            self._store.trim_entries(through)

    def purge(self, through: int) -> int:
        """:term:`Purge <purge>` removed records whose last feed entry is at or below ``through``.

        Their feed entries stay until trimmed.

        Args:
            through: The number of a :term:`change feed` entry, such as the
                oldest :term:`cursor` your application still reads from, so
                every part of it has seen these removals.

        Returns:
            How many records were purged.

        Raises:
            ValueError: If ``through`` is negative or past the last entry.
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
