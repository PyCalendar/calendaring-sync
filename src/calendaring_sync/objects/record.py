# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`record`: everything the core stores about one :term:`object`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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

    Attributes:
        local_key: The :term:`local key` the core gave the record. It never
            changes, and is never given to another record.
        scope: The id of the :term:`scope` the record belongs to.
        object_id: The :term:`object id`, or ``None`` until the server
            assigns one to an object you created.
        state: The record's state.
        version: The object's :term:`version`, or ``None``.
        fingerprint: The :term:`fingerprint` of :attr:`content`.
        content: The object's :term:`synced content`.
        containers: The :term:`containers <container>` the object belongs to.
        logical_key: The object's :term:`logical key`, if it has one.
        synced_at: When an :term:`ingest` last applied a change to the
            record, or ``None`` if none has.
        last_entry: The number of the last :term:`change feed` entry about
            the record, ``0`` if there's none.
    """

    local_key: int
    scope: str
    object_id: str | None
    state: RecordState
    version: str | None
    fingerprint: str
    content: str
    containers: frozenset[str]
    logical_key: str | None
    synced_at: datetime | None
    last_entry: int
