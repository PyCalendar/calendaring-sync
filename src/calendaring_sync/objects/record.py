# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`record`: everything the core stores about one :term:`object`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from calendaring_sync.objects.conflict import Conflict
from calendaring_sync.objects.edit import LocalEdit


class RecordState(StrEnum):
    """The five states a record can be in."""

    #: Matches the server's copy.
    SYNCED = "synced"
    #: Holds a :term:`pending edit`.
    PENDING = "pending"
    #: Its :term:`push` was sent, and how it ended isn't known yet.
    IN_FLIGHT = "in_flight"
    #: Holds an open :term:`conflict`.
    CONFLICT = "conflict"
    #: Gone from the server, kept until it's :term:`purged <purge>`.
    REMOVED = "removed"


@dataclass(frozen=True)
class Record:
    """One record.

    From :attr:`version` to :attr:`logical_key`, the fields hold the copy
    both sides last agreed on. A pending edit and the server's side of a
    conflict are kept beside it, in :attr:`edit` and :attr:`conflict`.

    Attributes:
        local_key: The :term:`local key` the core gave the record. It never
            changes, and is never given to another record.
        scope: The id of the :term:`scope` the record belongs to.
        object_id: The :term:`object id`, or ``None`` until the server
            assigns one to an object you created.
        state: The record's state.
        version: The :term:`version` of the synced copy, or ``None``. While
            the record is in conflict, the server's current version is the
            conflict's.
        fingerprint: The :term:`fingerprint` of :attr:`content`, or ``None``
            after a :term:`fingerprint scheme` change until the object is
            listed again.
        content: The object's :term:`synced content`, ``None`` for an
            object you created that the server doesn't have yet.
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
    """

    local_key: int
    scope: str
    object_id: str | None
    state: RecordState
    version: str | None
    fingerprint: str | None
    content: str | None
    containers: frozenset[str]
    logical_key: str | None
    synced_at: datetime | None
    last_entry: int
    edit: LocalEdit | None = None
    conflict: Conflict | None = None
