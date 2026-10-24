# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How your application resolves a :term:`conflict`."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

from calendaring_sync._checks import check_data, checked_when_built


@dataclass(frozen=True)
class TakeServer:
    """Keep the server's side and drop your edit.

    For a create the server refused because another object holds its
    :term:`logical key`, the server has nothing of yours, so the create is
    discarded; for a refused change, the object stays as it was synced.
    """


@dataclass(frozen=True)
class KeepEdit:
    """Push your edit as it is, over the server's current copy.

    Where the server removed the object, a change is pushed as a create.
    """


@checked_when_built
@dataclass(frozen=True)
class Merge:
    """Push new content you made from both sides, over the server's current copy.

    Build ``fingerprint``, ``containers`` and ``logical_key`` with the
    :term:`adapter`'s function for them, as for
    :meth:`~calendaring_sync.replica.Replica.change`.

    Attributes:
        content: The merged :term:`content`.
        fingerprint: Its :term:`fingerprint`.
        containers: The :term:`containers <container>` it belongs to.
        logical_key: Its :term:`logical key`, if it has one.

    Raises:
        TypeError: If a field has the wrong type.
        ValueError: If a string holds a :ref:`lone surrogate <lone-surrogates>`.

    Examples:
        Your application keeps what both sides changed in one copy:

        >>> from calendaring_sync import Merge
        >>> merged = Merge(b"Team meeting, room 3, moved", fingerprint="fp-merged")
        >>> merged.containers, merged.logical_key
        (frozenset(), None)
    """

    content: bytes
    _: KW_ONLY
    fingerprint: str
    containers: frozenset[str] = frozenset()
    logical_key: str | None = None

    def __post_init__(self) -> None:
        check_data(self.fingerprint, self.containers, self.logical_key)


@dataclass(frozen=True)
class Delete:
    """Delete the object, whatever the server's side holds."""


@dataclass(frozen=True)
class UpdateHolder:
    """Push a refused edit as a change to the object that holds its logical key.

    Only for a create or a change refused because another object holds its
    :term:`logical key`. A refused create then leaves nothing, and the object
    a refused change was for stays as it was synced.
    """


#: Any one of the five.
Resolution = TakeServer | KeepEdit | Merge | Delete | UpdateHolder
