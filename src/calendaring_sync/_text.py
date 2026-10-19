# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The checks on what the core stores: strings every store can keep, and content as bytes."""

from __future__ import annotations

from collections.abc import Iterable


def check_text(**values: str | Iterable[str] | None) -> None:
    """Raise ``ValueError`` if a string holds a lone surrogate.

    Every store keeps any string exactly, empty strings and whitespace
    included, except one kind. Python allows a ``str`` to hold a lone
    surrogate, half of the pair UTF-16 uses for characters such as emoji,
    for example ``"\\ud800"``. It's not a character, and UTF-8 can't encode
    it, so a store that writes UTF-8 fails on it while one that keeps
    Python objects doesn't. Refusing it where the string comes in keeps
    every store the same.

    Args:
        values: Each value by its argument's name: a string, strings, or
            ``None``.
    """
    for name, value in values.items():
        if value is None:
            continue
        for text in [value] if isinstance(value, str) else value:
            try:
                text.encode()
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"{name} holds a lone surrogate at {exc.start}, which can't be stored"
                ) from exc


def check_content(content: object) -> None:
    """Raise ``TypeError`` if content isn't bytes.

    Content is stored exactly as the server sent it, so it's never decoded.
    A ``str`` would be kept as it is by one store and written differently by
    another, so it's refused.
    """
    if not isinstance(content, bytes):
        raise TypeError(f"content must be bytes, not {type(content).__name__}")
