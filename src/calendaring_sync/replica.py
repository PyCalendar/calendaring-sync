# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The core's entry point: sessions, ingesting change sets, and reading the :term:`change feed`.

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

from calendaring_sync.constants import FEED_PAGE_SIZE
from calendaring_sync.error import (
    ScopeMismatchError,
    SessionError,
    StaleChangeSetError,
    TrimmedFeedError,
    UnknownScopeError,
)
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _holds_same_data(record: Record, change: Changed) -> bool:
    """Whether a change leaves a record's data as it is, so it isn't reported.

    Fingerprints decide, except right after a fingerprint scheme change,
    when the record has none and its text is compared instead.
    """
    same_content = (
        record.content == change.content
        if record.fingerprint is None
        else record.fingerprint == change.fingerprint
    )
    return (
        same_content
        and record.containers == change.containers
        and record.logical_key == change.logical_key
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
    kept the store busy, which is safe to retry, and
    :class:`~calendaring_sync.error.StoreError` when the store can't read or
    write its data. Neither changes anything.

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
        """
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
                    self._store.update_record(replace(record, version=None, fingerprint=None))

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
        """Apply one object's change to the store."""
        record = self._store.find_record(scope_id, object_id)
        live = record if record is not None and record.state is not RecordState.REMOVED else None
        if isinstance(change, Removed):
            if live is not None:
                self._write(
                    replace(live, state=RecordState.REMOVED, synced_at=now),
                    FeedEntryKind.REMOVED,
                    now,
                )
        elif isinstance(change, Unchanged):
            if live is None or live.version is None:
                raise ValueError(
                    f"Object {object_id!r} is reported unchanged, "
                    "but the store has no version for it"
                )
        elif live is not None and _holds_same_data(live, change):
            self._store.update_record(
                replace(
                    live,
                    version=change.version,
                    fingerprint=change.fingerprint,
                    content=change.content,
                    synced_at=now,
                )
            )
        else:
            synced = Record(
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
                synced = replace(synced, local_key=self._store.insert_record(synced))
            self._write(synced, FeedEntryKind.CHANGED, now)

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

    def records(self, scope_id: str) -> list[Record]:
        """Return every record in a scope, removed ones included, ordered by local key.

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        with self._store.transaction(write=False):
            self._existing_scope(scope_id)
            return self._store.scope_records(scope_id)

    def known_versions(self, scope_id: str) -> dict[str, str]:
        """Return the version of every object in a scope the store has one for.

        An :term:`adapter` compares these with the server's during a
        :term:`full listing`, and reports objects whose version matches as
        ``Unchanged`` instead of fetching them. Removed records aren't included.

        Raises:
            UnknownScopeError: If there's no scope with this id.
        """
        return {
            record.object_id: record.version
            for record in self.records(scope_id)
            if record.state is not RecordState.REMOVED
            and record.object_id is not None
            and record.version is not None
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
