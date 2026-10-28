# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An object's data: what every copy of it holds, whoever made the copy."""

from __future__ import annotations

from typing import TypeAlias

#: The fields that hold an object's data, in every copy of it: a record's
#: synced copy, a change from the server, a local edit, and a conflict's
#: server side. Two copies hold the same data when these match, and a record
#: synced to a copy takes these from it.
DATA_FIELDS = ("fingerprint", "content", "containers", "logical_key")

#: What a conflict puts in front of each field name for the server's copy.
REMOTE = "remote_"

#: A copy's data: its fingerprint, content, containers and logical key.
Data: TypeAlias = tuple[str | None, bytes | None, frozenset[str], str | None]

#: The data of a copy that isn't there, such as an object the server removed.
NO_DATA: Data = (None, None, frozenset(), None)


def data(copy: object, prefix: str = "") -> Data:
    """Return a copy's data, one value for each name in :data:`DATA_FIELDS`."""
    fingerprint, content, containers, logical_key = (
        getattr(copy, prefix + name) for name in DATA_FIELDS
    )
    return fingerprint, content, containers, logical_key
