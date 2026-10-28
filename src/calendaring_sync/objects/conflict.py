# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The :term:`server's copy` in a :term:`conflict`, kept on its :term:`record` until resolved."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from calendaring_sync.objects._data import NO_DATA, data
from calendaring_sync.objects.change import Changed, Removed


class ConflictKind(StrEnum):
    """How the :term:`server's copy` and the :term:`local edit` disagree."""

    #: Both sides changed the object.
    BOTH_CHANGED = "both_changed"
    #: The server removed the object, and you changed it.
    REMOVED_REMOTELY = "removed_remotely"
    #: You deleted the object, and the server changed it.
    DELETED_LOCALLY = "deleted_locally"
    #: The server has a new object at the id your unsent create chose.
    BOTH_CREATED = "both_created"
    #: The server refused your create, or your change, because another
    #: object holds its :term:`logical key`. Once that object stops holding
    #: it, the edit is pending again.
    LOGICAL_KEY_TAKEN = "logical_key_taken"


@dataclass(frozen=True)
class Conflict:
    """The server's copy of an object, kept where it conflicts with your edit.

    Together with its record's synced copy and :attr:`Record.edit
    <calendaring_sync.objects.record.Record.edit>`, it makes up the
    :term:`conflict record`: the record's
    :attr:`~calendaring_sync.objects.record.Record.content` is the object
    as both sides last agreed on it, ``edit.content`` is yours, and
    :attr:`remote_content` is the server's. A resolution can compare all
    three, for example to keep both sides where they changed different
    parts of the object.

    Attributes:
        kind: How the two sides disagree.
        remote_version: The :term:`version` of the server's copy, ``None`` if
            it has none, the server removed the object, or after a
            :term:`fingerprint scheme` change until the server's copy is
            fetched again.
        remote_fingerprint: The :term:`fingerprint` of the server's copy,
            ``None`` if the server removed the object, or after a
            :term:`fingerprint scheme` change until the server's copy is
            fetched again.
        remote_content: The server's copy, ``None`` if the server removed the
            object.
        remote_containers: The :term:`containers <container>` of the
            server's copy.
        remote_logical_key: The :term:`logical key` of the server's copy, if
            it has one.
        opened_at: When the conflict was first stored.
        holder: For a create refused because another object holds its
            :term:`logical key`, that object's :term:`object id` when the
            server named it; ``None`` otherwise.
    """

    kind: ConflictKind
    remote_version: str | None
    remote_fingerprint: str | None
    remote_content: bytes | None
    remote_containers: frozenset[str]
    remote_logical_key: str | None
    opened_at: datetime
    holder: str | None = None

    @classmethod
    def from_change(
        cls, kind: ConflictKind, change: Changed | Removed, opened_at: datetime
    ) -> Self:
        """Return a conflict whose server side is what a change reported."""
        if isinstance(change, Removed):
            return cls(kind, None, *NO_DATA, opened_at)
        return cls(kind, change.version, *data(change), opened_at)
