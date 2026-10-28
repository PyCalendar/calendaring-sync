# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`change set`, whole or one page of it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from calendaring_sync._checks import check_text, checked_when_built
from calendaring_sync.objects.change import Change, Unchanged


@checked_when_built
@dataclass(frozen=True)
class ChangeSet:
    """One :term:`change set`, or one :term:`truncated page` of it, for one :term:`scope`.

    Attributes:
        scope: The id of the scope it covers.
        since: The :term:`sync token` it was fetched from, ``None`` for a
            scope that has none. Every page of a :term:`full listing` carries
            the scope's token from before the listing.
        token: The token the scope moves to. On a truncated page of an
            :term:`incremental change set` that's the page's
            :term:`checkpoint`. Every page of a full listing carries the same
            token, the one the listing read before it started, and the scope
            moves to it when the last page is ingested.
        changes: Each object id mapped to how the object changed. An object
            appears once.
        full_listing: Whether it lists every object in the scope, rather
            than only what changed.
        truncated: Whether more pages follow it.

    Raises:
        TypeError: If a field has the wrong type.
        LoneSurrogateError: If the scope id, a token or an object id holds a lone surrogate.
        ValueError: If an incremental change set reports an object ``Unchanged``,
            which only a full listing can.
    """

    scope: str
    since: str | None
    token: str
    changes: Mapping[str, Change]
    full_listing: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        check_text(scope=self.scope, since=self.since, token=self.token, object_ids=self.changes)
        if not self.full_listing:
            for object_id, change in self.changes.items():
                if isinstance(change, Unchanged):
                    raise ValueError(f"Unchanged object {object_id!r} outside a full listing")
