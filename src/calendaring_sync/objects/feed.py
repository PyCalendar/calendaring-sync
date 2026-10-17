# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An entry in the :term:`change feed`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FeedEntryKind(StrEnum):
    """What a feed entry records."""

    #: A remote change was applied: the object is new, changed, or back on
    #: the server.
    CHANGED = "changed"
    #: A remote removal was applied.
    REMOVED = "removed"
    #: A :term:`scope` was :term:`dropped <drop>`, with every record in it.
    SCOPE_DROPPED = "scope_dropped"
    #: A :term:`conflict` was stored.
    CONFLICT_OPENED = "conflict_opened"
    #: The server's side of an open conflict changed.
    CONFLICT_UPDATED = "conflict_updated"
    #: An open conflict ended, and the record holds what it ended with.
    CONFLICT_RESOLVED = "conflict_resolved"


@dataclass(frozen=True)
class FeedEntry:
    """One feed entry.

    Attributes:
        number: The entry's number. Each entry's is larger than the one
            before it.
        scope: The id of the :term:`scope` the entry is about.
        kind: What happened.
        local_key: The :term:`local key` of the record the entry is about,
            ``None`` for a dropped scope. The record may since have been
            :term:`purged <purge>`.
        object_id: The record's :term:`object id` when the entry was
            written, ``None`` for a dropped scope.
        at: When the entry was written.
    """

    number: int
    scope: str
    kind: FeedEntryKind
    local_key: int | None
    object_id: str | None
    at: datetime
