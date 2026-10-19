# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a remote change meets a local edit, worked out from the three sides alone."""

from __future__ import annotations

from enum import StrEnum

from calendaring_sync.objects.change import Changed, Removed
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.record import Record

#: What "the same data" compares, in every copy of an object. A conflict
#: holds the server's copy under the same names with ``remote_`` in front.
_DATA_FIELDS = ("fingerprint", "content", "containers", "logical_key")

#: A copy of an object: the synced copy, a server's change, or a local edit.
Copy = Record | Changed | LocalEdit


class ChangeClass(StrEnum):
    """What an :term:`ingest` does with one object."""

    #: Nothing to do: no change, a version-only change, or both sides
    #: arrived at the same content.
    UNCHANGED = "unchanged"
    #: The server changed or created the object, with no local edit.
    REMOTE_ONLY = "remote_only"
    #: There's a local edit, and the server didn't change the object.
    LOCAL_ONLY = "local_only"
    #: The server removed the object with no local edit, or both sides
    #: deleted it.
    DELETED_REMOTE = "deleted_remote"
    #: The object was deleted locally, and the server didn't change it.
    DELETED_LOCAL = "deleted_local"
    #: Both sides acted on the object.
    CONFLICT = "conflict"


def _data(copy: object, prefix: str = "") -> tuple[object, ...]:
    return tuple(getattr(copy, prefix + name) for name in _DATA_FIELDS)


def _same(a: tuple[object, ...], b: tuple[object, ...]) -> bool:
    """Whether two copies' data match.

    Fingerprints decide when both copies have one; after a fingerprint
    scheme change one of them has none, and the content is compared instead.
    """
    (fingerprint_a, content_a, *rest_a), (fingerprint_b, content_b, *rest_b) = a, b
    if fingerprint_a is None or fingerprint_b is None:
        same_content = content_a == content_b
    else:
        same_content = fingerprint_a == fingerprint_b
    return same_content and rest_a == rest_b


def same_data(a: Copy, b: Copy) -> bool:
    """Whether two copies of an object hold the same data."""
    return _same(_data(a), _data(b))


def same_remote(conflict: Conflict, change: Changed | Removed) -> bool:
    """Whether the server reports again what an open conflict already holds."""
    if conflict.remote_content is None or isinstance(change, Removed):
        return conflict.remote_content is None and isinstance(change, Removed)
    return _same(_data(conflict, "remote_"), _data(change))


def classify(
    synced: Record | None, remote: Changed | Removed | None, local: LocalEdit | None
) -> tuple[ChangeClass, ConflictKind | None]:
    """Classify one object from its synced side, the server's report and the local edit.

    Args:
        synced: The record's :term:`synced content`, ``None`` for an object
            the store doesn't hold, one it holds as removed, or a create.
        remote: The server's new copy, ``Removed`` if the server removed it,
            or ``None`` if the server reported nothing new.
        local: The record's :term:`pending edit`, or ``None``.

    Returns:
        The class, and the conflict's kind when the class is ``CONFLICT``.
    """
    new = None if remote is None or isinstance(remote, Removed) else remote
    if local is None:
        if new is None:
            if remote is not None and synced is not None:
                return ChangeClass.DELETED_REMOTE, None
            return ChangeClass.UNCHANGED, None
        if synced is not None and same_data(new, synced):
            return ChangeClass.UNCHANGED, None
        return ChangeClass.REMOTE_ONLY, None
    if new is not None and local.kind is not EditKind.DELETE and same_data(new, local):
        return ChangeClass.UNCHANGED, None
    if local.kind is EditKind.CREATE:
        if new is not None:
            return ChangeClass.CONFLICT, ConflictKind.BOTH_CREATED
        return ChangeClass.LOCAL_ONLY, None
    if remote is None or (new is not None and synced is not None and same_data(new, synced)):
        if local.kind is EditKind.DELETE:
            return ChangeClass.DELETED_LOCAL, None
        return ChangeClass.LOCAL_ONLY, None
    if local.kind is EditKind.DELETE:
        if new is not None:
            return ChangeClass.CONFLICT, ConflictKind.DELETED_LOCALLY
        return ChangeClass.DELETED_REMOTE, None
    if new is not None:
        return ChangeClass.CONFLICT, ConflictKind.BOTH_CHANGED
    return ChangeClass.CONFLICT, ConflictKind.REMOVED_REMOTELY
