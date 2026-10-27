# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How your application resolves a :term:`conflict`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from calendaring_sync._checks import check_data, checked_when_built
from calendaring_sync.objects.edit import EditFields


@dataclass(frozen=True)
class KeepServer:
    """Keep the server's side, and drop your :term:`local edit`.

    Where the server has the object, the record takes the server's copy.
    Where it doesn't, because the server removed the object or refused your
    create, the record is removed, or deleted if the server never had it.
    """


@dataclass(frozen=True)
class KeepLocal:
    """Push your :term:`local edit` as it is, over the server's current copy.

    Where the server removed the object, a change goes out as a create. An
    edit the server refused because another object holds its
    :term:`logical key` can't go out unchanged, so this refuses it.
    """


@checked_when_built
@dataclass(frozen=True)
class Merge:
    """Push content you merged from the conflict's versions, over the server's current copy.

    A record in conflict holds three versions of its object: the
    :term:`synced content` both sides last agreed on, in
    :attr:`~calendaring_sync.objects.record.Record.content`; your
    :term:`local edit`, in :attr:`~calendaring_sync.objects.record.Record.edit`;
    and the server's side, in
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
        server's side moved to, in one copy. An adapter would read its
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


@dataclass(frozen=True)
class Delete:
    """Delete the object, whatever the server's side holds.

    Where the server has the object, the delete goes out over its current
    copy, and for a change the server refused for its :term:`logical key`,
    that's the object the change was for. Where the server doesn't have it,
    the record is removed, or deleted if the server never had it.
    """


@dataclass(frozen=True)
class UpdateHolder:
    """Push a refused edit as a change to the object that holds its logical key.

    Only for a create or a change refused because another object holds its
    :term:`logical key`. A refused create then leaves nothing, and the object
    a refused change was for stays as it was synced.
    """


#: Any one of the five.
Resolution: TypeAlias = KeepServer | KeepLocal | Merge | Delete | UpdateHolder
