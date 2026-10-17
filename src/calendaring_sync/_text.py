# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The one check every string the core stores goes through."""

from __future__ import annotations

from collections.abc import Iterable


def check_text(name: str, values: Iterable[str | None]) -> None:
    """Raise ``ValueError`` unless every value can be stored as text.

    A string holding a lone surrogate can't be encoded as UTF-8, so one
    store would keep it and another would fail on it. Checking where the
    text enters keeps every store's behavior the same.
    """
    for value in values:
        if value is None:
            continue
        try:
            value.encode()
        except UnicodeEncodeError as exc:
            raise ValueError(f"{name} isn't valid text: {exc.reason} at {exc.start}") from exc
