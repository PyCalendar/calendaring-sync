# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The three ways an object can appear in a :term:`change set`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from calendaring_sync._checks import check_data, checked_when_built


@checked_when_built
@dataclass(frozen=True)
class Changed:
    """The object changed on the server, or is new there.

    Attributes:
        version: The object's :term:`version`, or ``None`` if the server
            gives none.
        fingerprint: The object's :term:`fingerprint`, computed by the
            :term:`adapter`.
        content: The object's :term:`content`, the bytes exactly as the server
            sent them.
        containers: The :term:`containers <container>` the object belongs to.
        logical_key: The object's :term:`logical key`, if it has one.

    Raises:
        TypeError: If a field has the wrong type.
        LoneSurrogateError: If a string holds a lone surrogate.

    Examples:
        An :term:`adapter` reports an object the server changed, and one it
        removed, in a change set from token ``t1`` to ``t2``:

        >>> from calendaring_sync import Changed, ChangeSet, Removed
        >>> changes = {"a": Changed("v2", "fp-a2", b"Team meeting, moved"), "b": Removed()}
        >>> change_set = ChangeSet("work", "t1", "t2", changes)
        >>> change_set.changes["a"].content
        b'Team meeting, moved'
    """

    version: str | None
    fingerprint: str
    content: bytes
    containers: frozenset[str] = frozenset()
    logical_key: str | None = None

    def __post_init__(self) -> None:
        check_data(self.fingerprint, self.containers, self.logical_key, version=self.version)


@dataclass(frozen=True)
class Removed:
    """The object is gone from the server."""


@dataclass(frozen=True)
class Unchanged:
    """The object hasn't changed since the :term:`version` the store holds.

    Only a :term:`full listing` reports this, so the object isn't fetched again.
    """


#: Any one of the three.
Change: TypeAlias = Changed | Removed | Unchanged
