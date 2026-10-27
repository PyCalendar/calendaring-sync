# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`local edit` your application recorded and the server doesn't have yet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from calendaring_sync._checks import checked_when_built


class EditKind(StrEnum):
    """What a local edit does to its object."""

    #: Changes an object the server has.
    CHANGE = "change"
    #: Deletes an object the server has.
    DELETE = "delete"
    #: Creates an object the server doesn't have yet.
    CREATE = "create"


@dataclass(frozen=True)
class LocalEdit:
    """One :term:`pending edit` of a :term:`record`.

    Attributes:
        kind: What the edit does.
        content: The object's :term:`content` after the edit, ``None`` for a
            delete.
        fingerprint: The :term:`fingerprint` of :attr:`content`, ``None`` for
            a delete, or after a :term:`fingerprint scheme` change until the
            object is edited again.
        containers: The :term:`containers <container>` the object belongs to
            after the edit.
        logical_key: The object's :term:`logical key` after the edit, if it
            has one.
        recorded_at: When the edit was last recorded.
    """

    kind: EditKind
    content: bytes | None
    fingerprint: str | None
    containers: frozenset[str]
    logical_key: str | None
    recorded_at: datetime


@checked_when_built
@dataclass(frozen=True)
class EditFields:
    """What an edit records about an object beside its content.

    An :term:`adapter` reads these from content the same way for the
    server's copy and for an edit, so an edit back to the synced content
    cancels, and the server's copy of your own edit isn't a conflict. To
    build them for content you made, such as a :class:`Merge
    <calendaring_sync.objects.resolution.Merge>`, call the adapter's
    :meth:`~calendaring_sync.adapters.base.Adapter.edit_fields`.

    Attributes:
        fingerprint: The content's :term:`fingerprint`.
        containers: The :term:`containers <container>` the object belongs to.
        logical_key: The object's :term:`logical key`, if it has one.

    Raises:
        TypeError: If a field has the wrong type.

    Examples:
        >>> from calendaring_sync import EditFields
        >>> fields = EditFields("fp-a2", frozenset({"work"}), "meeting-1")
        >>> fields.logical_key
        'meeting-1'
    """

    fingerprint: str
    containers: frozenset[str] = frozenset()
    logical_key: str | None = None
