# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How your application resolves a :term:`conflict`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TypeAlias, cast

from calendaring_sync._checks import check_data, checked_when_built
from calendaring_sync.objects.conflict import Conflict
from calendaring_sync.objects.edit import EditFields, EditKind, LocalEdit
from calendaring_sync.objects.record import Record, RecordState

#: What a resolution leaves: the record over the server's current copy, or
#: ``None`` where nothing is left, and the edit it should hold, or ``None``
#: for the record as it is. Only a record in conflict is resolved, so every
#: function below can rely on its conflict and its edit being there.
_Resolved: TypeAlias = tuple[Record | None, LocalEdit | None]


def _on_server(record: Record) -> bool:
    """Whether the server has the object the record's conflict is about."""
    return cast(Conflict, record.conflict).remote_content is not None or record.is_refused_change


def _over_server_copy(record: Record, now: datetime) -> Record:
    """The record synced to the server's current copy, so the next push goes out against it."""
    if record.is_refused_change:
        # The server kept the synced copy.
        return replace(record, state=RecordState.SYNCED, edit=None, conflict=None)
    return record.synced_to(cast(Conflict, record.conflict), now)


def _gone(record: Record, now: datetime) -> _Resolved:
    """A record whose object the server doesn't have: removed, or nothing if it never had it."""
    return (None if record.is_create else record.removed_at(now)), None


def _edit_kind(record: Record) -> EditKind:
    """Over a copy the server has, an edit is a change; with none, a create."""
    return EditKind.CHANGE if _on_server(record) else EditKind.CREATE


class _Resolution(ABC):
    """What every resolution says about a record in conflict: if it applies, what it leaves."""

    def _check(self, record: Record) -> None:  # noqa: B027, most apply to any conflict
        """Raise ``ValueError`` if the resolution can't apply to the record's conflict."""

    @abstractmethod
    def _resolve(self, record: Record, now: datetime) -> _Resolved:
        """What the resolution leaves of the record, and the edit it should hold."""

    def _holder_edit(self, record: Record) -> LocalEdit | None:
        """The edit to give the object holding the record's logical key, if any."""
        return None


@dataclass(frozen=True)
class KeepServer(_Resolution):
    """Keep the :term:`server's copy`, and drop your :term:`local edit`.

    Where the server has the object, the record takes the server's copy.
    Where it doesn't, because the server removed the object or refused your
    create, the record is removed, or deleted if the server never had it.
    """

    def _resolve(self, record: Record, now: datetime) -> _Resolved:
        if not _on_server(record):
            return _gone(record, now)
        return _over_server_copy(record, now), None


@dataclass(frozen=True)
class KeepLocal(_Resolution):
    """Push your :term:`local edit` as it is, over the server's current copy.

    Where the server removed the object, a change goes out as a create. An
    edit the server refused because another object holds its
    :term:`logical key` can't go out unchanged, so this refuses it.
    """

    def _check(self, record: Record) -> None:
        if record.is_refused:
            raise ValueError("An edit refused for its logical key can't be pushed again unchanged")

    def _resolve(self, record: Record, now: datetime) -> _Resolved:
        edit = cast(LocalEdit, record.edit)
        if edit.kind is not EditKind.DELETE:
            edit = replace(edit, kind=_edit_kind(record))
        return _over_server_copy(record, now), edit


@checked_when_built
@dataclass(frozen=True)
class Merge(_Resolution):
    """Push content you merged from the conflict's versions, over the server's current copy.

    A record in conflict holds three versions of its object: the
    :term:`synced copy` both sides last agreed on, in
    :attr:`~calendaring_sync.objects.record.Record.content`; your
    :term:`local edit`, in :attr:`~calendaring_sync.objects.record.Record.edit`;
    and the :term:`server's copy`, in
    :attr:`~calendaring_sync.objects.record.Record.conflict`. Your application
    merges them into new content, and reads its fields with the
    :term:`adapter`'s :meth:`~calendaring_sync.adapters.base.Adapter.edit_fields`,
    so they match what a sync reads from the server's copy. Where the server
    removed the object, the merge goes out as a create.

    Attributes:
        content: The merged :term:`content`.
        fields: Its fingerprint, containers and logical key.

    Raises:
        TypeError: If a field has the wrong type.
        LoneSurrogateError: If a string holds a lone surrogate.

    Examples:
        Your application keeps the room your edit set and the time the
        server's copy moved to, in one copy. An adapter would read its
        fields; these are made up:

        >>> from calendaring_sync import EditFields, Merge
        >>> content = b"Team meeting, room 3, moved"
        >>> merged = Merge(content, EditFields("fp-merged"))
        >>> merged.fields.fingerprint
        'fp-merged'
    """

    content: bytes
    fields: EditFields

    def __post_init__(self) -> None:
        # Every other way in checks these strings where the core takes them.
        check_data(self.fields.fingerprint, self.fields.containers, self.fields.logical_key)

    def _resolve(self, record: Record, now: datetime) -> _Resolved:
        fields = self.fields
        merged = LocalEdit(
            _edit_kind(record),
            self.content,
            fields.fingerprint,
            fields.containers,
            fields.logical_key,
            now,
        )
        return _over_server_copy(record, now), merged


@dataclass(frozen=True)
class Delete(_Resolution):
    """Delete the object, whatever the server's copy holds.

    Where the server has the object, the delete goes out over its current
    copy, and for a change the server refused for its :term:`logical key`,
    that's the object the change was for. Where the server doesn't have it,
    the record is removed, or deleted if the server never had it.
    """

    def _resolve(self, record: Record, now: datetime) -> _Resolved:
        if not _on_server(record):
            return _gone(record, now)
        deleted = LocalEdit(EditKind.DELETE, None, None, frozenset(), None, now)
        return _over_server_copy(record, now), deleted


@dataclass(frozen=True)
class UpdateHolder(_Resolution):
    """Push a refused edit as a change to the object that holds its logical key.

    Only for a create or a change refused because another object holds its
    :term:`logical key`. A refused create then leaves nothing, and the object
    a refused change was for stays as it was synced.
    """

    def _check(self, record: Record) -> None:
        if not record.is_refused:
            raise ValueError("Only an edit refused for its logical key goes to the key's holder")

    def _resolve(self, record: Record, now: datetime) -> _Resolved:
        # The edit goes to the holder; this record keeps what the server has.
        return KeepServer()._resolve(record, now)

    def _holder_edit(self, record: Record) -> LocalEdit | None:
        return record.edit


@dataclass(frozen=True)
class ResolvedConflicts:
    """What :meth:`~calendaring_sync.replica.Replica.resolve_conflicts` did with each conflict.

    Each field holds :term:`local keys <local key>`, in the order the
    conflicts were passed to your resolver.

    Attributes:
        resolved: The conflicts resolved as your resolver answered.
        left_open: The conflicts your resolver answered ``None`` for.
        changed: The conflicts that changed while your resolver ran, so its
            answer wasn't applied. The next call passes them again.
    """

    resolved: tuple[int, ...] = ()
    left_open: tuple[int, ...] = ()
    changed: tuple[int, ...] = ()


#: Any one of the five.
Resolution: TypeAlias = KeepServer | KeepLocal | Merge | Delete | UpdateHolder
