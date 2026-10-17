# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The server's side of a :term:`conflict`, kept on its :term:`record`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ConflictKind(StrEnum):
    """How the server's side and the :term:`local edit` disagree."""

    #: Both sides changed the object.
    BOTH_CHANGED = "both_changed"
    #: The server removed the object, and you changed it.
    REMOVED_REMOTELY = "removed_remotely"
    #: You deleted the object, and the server changed it.
    DELETED_LOCALLY = "deleted_locally"
    #: The server has a new object at the id your unsent create chose.
    BOTH_CREATED = "both_created"
    #: The server refused your create because another object holds its
    #: :term:`logical key`.
    LOGICAL_KEY_TAKEN = "logical_key_taken"


@dataclass(frozen=True)
class Conflict:
    """An open conflict.

    Together with its record's synced side and :attr:`Record.edit
    <calendaring_sync.objects.record.Record.edit>`, it makes up the
    :term:`conflict record`.

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
    """

    kind: ConflictKind
    remote_version: str | None
    remote_fingerprint: str | None
    remote_content: str | None
    remote_containers: frozenset[str]
    remote_logical_key: str | None
    opened_at: datetime
