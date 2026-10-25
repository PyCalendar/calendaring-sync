# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`record`: everything the core stores about one :term:`object`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.edit import EditKind, LocalEdit


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

    @property
    def is_create(self) -> bool:
        """Whether the record is an object you created that the server doesn't have yet."""
        return self.edit is not None and self.edit.kind is EditKind.CREATE

    @property
    def is_refused_change(self) -> bool:
        """Whether the server refused this record's change for its :term:`logical key`.

        The server then still holds the synced copy, and the conflict holds
        no copy of its own.
        """
        return (
            self.conflict is not None
            and self.conflict.kind is ConflictKind.LOGICAL_KEY_TAKEN
            and not self.is_create
        )

    @property
    def server_version(self) -> str | None:
        """The :term:`version` of the server's copy, ``None`` if the store knows none.

        That's the conflict's while the record is in conflict, but the synced
        one for a change the server refused, and ``None`` once the server
        removed the object.
        """
        if self.state is RecordState.REMOVED:
            return None
        if self.conflict is not None and not self.is_refused_change:
            return self.conflict.remote_version
        return self.version
