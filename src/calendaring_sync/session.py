# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`session`, one sync of one :term:`scope`: it ingests what changed, then pushes."""

from __future__ import annotations

from enum import StrEnum

from calendaring_sync._checks import check_argument_types, check_text
from calendaring_sync.error import OtherScopeError, SessionError, StaleChangeSetError
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.push import PushOutcome
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.replica import Replica


def _check_outcome(claimed_keys: set[int], claimed: Record, outcome: PushOutcome) -> None:
    """Raise ``ValueError`` unless the outcome is one a push of the claimed edit can have."""
    if (
        claimed.local_key not in claimed_keys
        or claimed.state is not RecordState.IN_FLIGHT
        or claimed.edit is None
    ):
        raise ValueError("settle() takes a record this session's claim() returned")
    outcome._check(claimed.edit.kind, claimed.object_id is not None)


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
    """One sync of one :term:`scope`, opened with :meth:`~calendaring_sync.replica.Replica.session`.

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
            OtherScopeError: If the change set is for another scope.
            ListingRequiredError: If an incremental change set arrives for a
                scope with no token.
            UnknownVersionError: If an ``Unchanged`` entry names a record
                with no version.
        """
        if change_set.scope != self._scope_id:
            raise OtherScopeError(
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
            OtherScopeError: If the record belongs to another scope.
            ObjectIdAssignedError: If ``object_id`` is given for a record
                that has one.
            LoneSurrogateError: If a string holds a lone surrogate.

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
                a delete, ``Applied`` with an id for a delete, or
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
