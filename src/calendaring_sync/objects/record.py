# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`record`: everything the core stores about one :term:`object`."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Self, TypeAlias, cast

from calendaring_sync.objects._data import REMOTE, data
from calendaring_sync.objects.change import Changed
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.edit import EditKind, LocalEdit

#: A copy of an object that a record can be synced to.
SyncedCopy: TypeAlias = Changed | LocalEdit | Conflict


class RecordState(StrEnum):
    """The five states a record can be in."""

    #: Matches the server's copy.
    SYNCED = "synced"
    #: Holds a :term:`pending edit`.
    PENDING = "pending"
    #: Its :term:`push` was :term:`claimed <claim>`, and how it ended isn't known yet.
    IN_FLIGHT = "in_flight"
    #: Holds an open :term:`conflict`.
    CONFLICT = "conflict"
    #: Gone from the server, kept until it's :term:`purged <purge>` or the
    #: object comes back, on the server or as a create at its id.
    REMOVED = "removed"


@dataclass(frozen=True)
class Record:
    """What the store keeps about one object on the server.

    From :attr:`version` to :attr:`logical_key`, the fields hold the synced
    copy: the object as your application and the server last agreed on it.
    Your :term:`pending edit` is kept beside it in :attr:`edit`, and the
    server's newer copy, when it conflicts with your edit, in
    :attr:`conflict`. A record in conflict therefore holds all three
    versions of the object: the synced copy, yours, and the server's.

    Attributes:
        local_key: The :term:`local key` the core gave the record. It never
            changes, and is never given to another record.
        scope: The id of the :term:`scope` the record belongs to.
        object_id: The :term:`object id`, or ``None`` until the server
            assigns one to an object you created.
        state: The record's state.
        version: The :term:`version` the server gave the synced copy, or
            ``None``. While the record is in conflict, the server holds a
            newer copy, whose version is ``conflict.remote_version``;
            :attr:`server_version` gives whichever is current.
        fingerprint: The :term:`fingerprint` of :attr:`content`, or ``None``
            after a :term:`fingerprint scheme` change until the object is
            listed again.
        content: The :term:`synced content`, the bytes the server sent, in
            the scope's media type. ``None`` for an object you created that
            the server doesn't have yet.
        containers: The :term:`containers <container>` of the synced copy.
        logical_key: The :term:`logical key` of the synced copy, if it has one.
        synced_at: When an :term:`ingest` last applied a change to the
            record, or ``None`` if none has.
        last_entry: The number of the last :term:`change feed` entry about
            the record, ``0`` if there's none.
        edit: The record's :term:`pending edit`, held while it's pending, in
            flight or in conflict, and ``None`` otherwise.
        conflict: The server's side of an open :term:`conflict`, held while
            the record is in conflict, and ``None`` otherwise.
        claims: How many times a :term:`push` of the record was
            :term:`claimed <claim>`. How a push ended only counts while no
            later claim has been made.
    """

    local_key: int
    scope: str
    object_id: str | None
    state: RecordState
    version: str | None
    fingerprint: str | None
    content: bytes | None
    containers: frozenset[str]
    logical_key: str | None
    synced_at: datetime | None
    last_entry: int
    edit: LocalEdit | None = None
    conflict: Conflict | None = None
    claims: int = 0

    @classmethod
    def new(cls, scope_id: str, object_id: str | None = None) -> Self:
        """Return a record the store doesn't hold yet, with nothing synced.

        Its :attr:`local_key` is ``0`` until a store's
        :meth:`~calendaring_sync.store.base.StateStore.insert_record` gives
        it one; fill in the rest with :meth:`synced_to`, or with an edit.

        Examples:
            >>> from calendaring_sync import Record
            >>> record = Record.new("work", "a")
            >>> record.local_key, record.object_id, record.content
            (0, 'a', None)
        """
        return cls(
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

    def with_copy(self, copy: SyncedCopy, at: datetime) -> Self:
        """Return the record whose synced copy is ``copy``, synced at ``at``.

        Its state, edit and conflict stay as they are. ``copy`` is one of:

        - a change from the server, at its version
        - your edit, once the server applied it, with no version: the server
          may store it rewritten, so the next ingest brings its version
        - a conflict's server side, at its version

        Examples:
            >>> from datetime import UTC, datetime
            >>> from calendaring_sync import Changed, Record
            >>> change = Changed("v2", "fp-a2", b"Team meeting, moved")
            >>> moved = Record.new("work", "a").with_copy(change, datetime(2026, 11, 2, tzinfo=UTC))
            >>> moved.version, moved.content
            ('v2', b'Team meeting, moved')
        """
        if isinstance(copy, Conflict):
            version, prefix = copy.remote_version, REMOTE
        else:
            version, prefix = (copy.version if isinstance(copy, Changed) else None), ""
        fingerprint, content, containers, logical_key = data(copy, prefix)
        return replace(
            self,
            version=version,
            fingerprint=fingerprint,
            content=content,
            containers=containers,
            logical_key=logical_key,
            synced_at=at,
        )

    def synced_to(self, copy: SyncedCopy, at: datetime) -> Self:
        """Return the record synced to ``copy``, with no edit or conflict left.

        ``copy`` is what :meth:`with_copy` takes.

        Examples:
            >>> from datetime import UTC, datetime
            >>> from calendaring_sync import Changed, Record
            >>> change = Changed("v1", "fp-a", b"Team meeting")
            >>> Record.new("work", "a").synced_to(change, datetime(2026, 11, 2, tzinfo=UTC)).state
            <RecordState.SYNCED: 'synced'>
        """
        return replace(self.with_copy(copy, at), state=RecordState.SYNCED, edit=None, conflict=None)

    def removed_at(self, at: datetime) -> Self:
        """Return the record once its object is gone from the server, with nothing left to push.

        It keeps its synced copy, so a reader of the :term:`change feed`
        can still see what was removed.

        Examples:
            >>> from datetime import UTC, datetime
            >>> from calendaring_sync import Record
            >>> Record.new("work", "a").removed_at(datetime(2026, 11, 2, tzinfo=UTC)).state
            <RecordState.REMOVED: 'removed'>
        """
        return replace(self, state=RecordState.REMOVED, edit=None, conflict=None, synced_at=at)

    @property
    def is_create(self) -> bool:
        """Whether the record is an object you created that the server doesn't have yet."""
        return self.edit is not None and self.edit.kind is EditKind.CREATE

    @property
    def is_refused(self) -> bool:
        """Whether the server refused this record's create or change for its :term:`logical key`."""
        return self.conflict is not None and self.conflict.kind is ConflictKind.LOGICAL_KEY_TAKEN

    @property
    def is_refused_change(self) -> bool:
        """Whether the server refused this record's change for its :term:`logical key`.

        The server then still holds the synced copy, and the conflict holds
        no copy of its own.
        """
        return self.is_refused and not self.is_create

    @property
    def server_version(self) -> str | None:
        """The :term:`version` of the server's copy, ``None`` if the store knows none.

        That's the conflict's while the record is in conflict, but the synced
        one for a change the server refused, and ``None`` once the server
        removed the object.
        """
        if self.state is RecordState.REMOVED:
            return None
        if self._conflict_holds_server_copy:
            return cast(Conflict, self.conflict).remote_version
        return self.version

    @property
    def server_logical_key(self) -> str | None:
        """The :term:`logical key` of the server's copy, ``None`` if the server holds none.

        As with :attr:`server_version`, that's the conflict's while the
        record is in conflict, and the synced one for a change the server
        refused. A create the server doesn't have has none.
        """
        if self.state is RecordState.REMOVED:
            return None
        if self._conflict_holds_server_copy:
            return cast(Conflict, self.conflict).remote_logical_key
        return self.logical_key

    @property
    def _conflict_holds_server_copy(self) -> bool:
        """Whether the conflict, not the synced copy, holds what the server has."""
        return self.conflict is not None and not self.is_refused_change
