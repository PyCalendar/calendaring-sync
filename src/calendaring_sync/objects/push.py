# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a :term:`push` ended, as the :term:`adapter` reports it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from typing import NamedTuple, TypeAlias, cast

from calendaring_sync._checks import check_text, checked_when_built
from calendaring_sync.objects.change import Removed
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.feed import FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState


class _Settled(NamedTuple):
    """What settling a push writes."""

    #: The record as the outcome leaves it.
    record: Record
    #: The change feed entry about it.
    kind: FeedEntryKind
    #: An object id the record takes over from a removed record, if any.
    takes: str | None = None


def _deleted(record: Record, now: datetime) -> _Settled | None:
    """A push that removed the object, if the record's edit was a delete."""
    if cast(LocalEdit, record.edit).kind is EditKind.DELETE:
        return _Settled(record.removed_at(now), FeedEntryKind.PUSHED)
    return None


def _in_conflict(
    record: Record, kind: ConflictKind, now: datetime, holder: str | None = None
) -> _Settled:
    """A conflict whose server has no copy of the object, opened by a push's outcome."""
    conflict = replace(Conflict.from_change(kind, Removed(), now), holder=holder)
    opened = replace(record, state=RecordState.CONFLICT, conflict=conflict)
    return _Settled(opened, FeedEntryKind.CONFLICT_OPENED)


class _Outcome(ABC):
    """What every push outcome says about the record whose push it ends.

    That record is in flight, or pending again after an ingest, so it
    always holds its edit; the store checks it.
    """

    def _check(self, kind: EditKind, *, has_object_id: bool) -> None:  # noqa: B027, most fit any push
        """Raise ``ValueError`` if no push of an edit of this kind can end this way."""

    @abstractmethod
    def _settled(self, record: Record, now: datetime) -> _Settled | None:
        """What the outcome writes for the claimed record, or ``None`` for nothing."""


@checked_when_built
@dataclass(frozen=True)
class Applied(_Outcome):
    """The server applied the edit.

    Attributes:
        object_id: The :term:`object id` the server gave the object, for a
            create whose id the server picks, or when the server moved the
            object to another id. ``None`` keeps the record's id.

    Raises:
        TypeError: If the id isn't a string or ``None``.
        LoneSurrogateError: If the id holds a lone surrogate.

    Examples:
        An :term:`adapter` reports a create the server stored under an id it
        picked:

        >>> from calendaring_sync import Applied
        >>> Applied("event-17").object_id
        'event-17'
    """

    object_id: str | None = None

    def __post_init__(self) -> None:
        check_text(object_id=self.object_id)

    def _check(self, kind: EditKind, *, has_object_id: bool) -> None:
        if kind is EditKind.DELETE and self.object_id is not None:
            raise ValueError("An applied delete has no object id to report")
        if kind is EditKind.CREATE and self.object_id is None and not has_object_id:
            raise ValueError("An applied create needs the object id the server gave it")

    def _settled(self, record: Record, now: datetime) -> _Settled | None:
        deleted = _deleted(record, now)
        if deleted is not None:
            return deleted
        renamed = self.object_id
        # The server may store the edit rewritten, so no version is kept.
        pushed = record.synced_to(cast(LocalEdit, record.edit), now)
        if renamed is None or renamed == record.object_id:
            return _Settled(pushed, FeedEntryKind.PUSHED)
        return _Settled(replace(pushed, object_id=renamed), FeedEntryKind.PUSHED, renamed)


@dataclass(frozen=True)
class Rejected(_Outcome):
    """The server refused the write because something changed since your last sync.

    The edit stays pending. The next :term:`ingest` makes it a
    :term:`conflict` only if the object the edit is for changed, and
    otherwise it's pushed again.
    """

    def _settled(self, record: Record, now: datetime) -> _Settled | None:
        if record.state is RecordState.PENDING:
            # An ingest already put the push back; the store says what this would.
            return None
        return _Settled(replace(record, state=RecordState.PENDING), FeedEntryKind.PUSH_FAILED)


@dataclass(frozen=True)
class Gone(_Outcome):
    """The object no longer exists on the server.

    A delete is then done; a change becomes a :term:`conflict`.
    """

    def _check(self, kind: EditKind, *, has_object_id: bool) -> None:
        if kind is EditKind.CREATE:
            raise ValueError("A create can't be gone: the server never had the object")

    def _settled(self, record: Record, now: datetime) -> _Settled | None:
        deleted = _deleted(record, now)
        return deleted or _in_conflict(record, ConflictKind.REMOVED_REMOTELY, now)


@checked_when_built
@dataclass(frozen=True)
class LogicalKeyTaken(_Outcome):
    """The server refused a create or a change because another object holds its :term:`logical key`.

    Attributes:
        holder: The :term:`object id` of the object that holds the key, when
            the server names it; ``None`` when it doesn't.

    Raises:
        TypeError: If the id isn't a string or ``None``.
        LoneSurrogateError: If the id holds a lone surrogate.
    """

    holder: str | None = None

    def __post_init__(self) -> None:
        check_text(holder=self.holder)

    def _check(self, kind: EditKind, *, has_object_id: bool) -> None:
        if kind is EditKind.DELETE:
            raise ValueError("A delete can't be refused for its logical key")

    def _settled(self, record: Record, now: datetime) -> _Settled | None:
        return _in_conflict(record, ConflictKind.LOGICAL_KEY_TAKEN, now, self.holder)


@dataclass(frozen=True)
class Unknown(_Outcome):
    """The request may or may not have reached the server.

    The push stays :term:`in flight`, and the next complete :term:`ingest`
    settles it from what the server then holds.
    """

    def _settled(self, record: Record, now: datetime) -> _Settled | None:
        return None


#: Any one of the five.
PushOutcome: TypeAlias = Applied | Rejected | Gone | LogicalKeyTaken | Unknown
