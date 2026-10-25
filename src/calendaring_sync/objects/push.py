# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a :term:`push` ended, as the :term:`adapter` reports it."""

from __future__ import annotations

from dataclasses import dataclass

from calendaring_sync._checks import check_text, checked_when_built


@checked_when_built
@dataclass(frozen=True)
class Applied:
    """The server applied the edit.

    Attributes:
        object_id: The :term:`object id` the server gave the object, for a
            create whose id the server picks, or when the server moved the
            object to another id. ``None`` keeps the record's id.

    Raises:
        TypeError: If the id isn't a string or ``None``.
        ValueError: If the id holds a :ref:`lone surrogate <lone-surrogates>`.

    Examples:
        An :term:`adapter` reports a create the server stored under an id it
        picked:

        >>> from calendaring_sync import Applied
        >>> Applied("event-17").object_id
        'event-17'
    """

    object_id: str | None = None

    def __post_init__(self) -> None:
        check_text(object_id=self.object_id)


@dataclass(frozen=True)
class Rejected:
    """The server refused the write because something changed since your last sync.

    The edit stays pending. The next :term:`ingest` makes it a
    :term:`conflict` only if this object changed, and otherwise it's pushed
    again.
    """


@dataclass(frozen=True)
class Gone:
    """The object no longer exists on the server.

    A delete is then done; a change becomes a :term:`conflict`.
    """


@checked_when_built
@dataclass(frozen=True)
class LogicalKeyTaken:
    """The server refused a create or a change because another object holds its :term:`logical key`.

    Attributes:
        holder: The :term:`object id` of the object that holds the key, when
            the server names it; ``None`` when it doesn't.

    Raises:
        TypeError: If the id isn't a string or ``None``.
        ValueError: If the id holds a :ref:`lone surrogate <lone-surrogates>`.
    """

    holder: str | None = None

    def __post_init__(self) -> None:
        check_text(holder=self.holder)


@dataclass(frozen=True)
class Unknown:
    """The request may or may not have reached the server.

    The push stays :term:`in flight`, and the next complete :term:`ingest`
    settles it from what the server then holds.
    """


#: Any one of the five.
PushOutcome = Applied | Rejected | Gone | LogicalKeyTaken | Unknown
