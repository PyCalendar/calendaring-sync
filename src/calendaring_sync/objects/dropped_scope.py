# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What :meth:`~calendaring_sync.replica.Replica.drop_scope` discards."""

from __future__ import annotations

from dataclasses import dataclass

from calendaring_sync.objects.record import Record


@dataclass(frozen=True)
class DroppedScope:
    """The local work a :term:`drop` discarded, for your application to save or show.

    Attributes:
        scope: The id of the dropped :term:`scope`.
        pending: The records that held a :term:`pending edit`, including
            ones :term:`in flight`.
        conflicts: The records that held an open :term:`conflict`.
    """

    scope: str
    pending: tuple[Record, ...]
    conflicts: tuple[Record, ...]
