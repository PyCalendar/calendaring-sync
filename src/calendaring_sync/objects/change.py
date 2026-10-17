# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The three ways an object can appear in a :term:`change set`."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Changed:
    """The object changed on the server, or is new there.

    Attributes:
        version: The object's :term:`version`, or ``None`` if the server
            gives none.
        fingerprint: The object's :term:`fingerprint`, computed by the
            :term:`adapter`.
        content: The object's :term:`content`, exactly as the server sent it.
        containers: The :term:`containers <container>` the object belongs to.
        logical_key: The object's :term:`logical key`, if it has one.
    """

    version: str | None
    fingerprint: str
    content: str
    containers: frozenset[str] = frozenset()
    logical_key: str | None = None


@dataclass(frozen=True)
class Removed:
    """The object is gone from the server."""


@dataclass(frozen=True)
class Unchanged:
    """The object hasn't changed since the :term:`version` the store holds.

    Only a :term:`full listing` reports this, so the object isn't fetched again.
    """


#: Any one of the three.
Change = Changed | Removed | Unchanged
