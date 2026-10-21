# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a remote change meets a local edit, worked out from the three sides alone."""

from __future__ import annotations

from enum import StrEnum

from calendaring_sync.objects._data import REMOTE, data
from calendaring_sync.objects.change import Changed, Removed
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.record import Record

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
    return _same(data(a), data(b))


def same_remote(conflict: Conflict, change: Changed | Removed) -> bool:
    """Whether the server reports again what an open conflict already holds."""
    if conflict.remote_content is None or isinstance(change, Removed):
        return conflict.remote_content is None and isinstance(change, Removed)
    return _same(data(conflict, REMOTE), data(change))


def same_conflict(a: Conflict, b: Conflict) -> bool:
    """Whether two copies of a conflict say the same thing, whatever the server's version."""
    if (a.kind, a.holder) != (b.kind, b.holder):
        return False
    return _same(data(a, REMOTE), data(b, REMOTE))


def classify(
    synced: Record | None,
    remote: Changed | Removed | None,
    local: LocalEdit | None,
) -> tuple[ChangeClass, ConflictKind | None]:
    """Classify one object from its synced side, the server's report and the local edit.

    Each case below is one kind of local edit meeting one kind of report
    from the server. A change that holds the synced data counts as no
    change, and one that holds the edit's data means both sides arrived at
    the same object.

    Args:
        synced: The record's :term:`synced content`, ``None`` for an object
            the store doesn't hold, one it holds as removed, or a create.
        remote: The server's new copy, ``Removed`` if the server removed it,
            or ``None`` if the server reported nothing new.
        local: The record's :term:`pending edit`, or ``None``.

    Returns:
        The class, and the conflict's kind when the class is ``CONFLICT``.

    Raises:
        ValueError: If no object could have these sides: nothing at all,
            a create of an object the server has, or a change or delete of
            one it doesn't.
    """
    if synced is None and remote is None and local is None:
        raise ValueError("Nothing to classify: no synced copy, remote change or local edit")
    if local is not None and (local.kind is EditKind.CREATE) != (synced is None):
        has = "without" if synced is None else "with"
        raise ValueError(f"A {local.kind} edit can't meet an object {has} a synced copy")
    kept = isinstance(remote, Changed) and synced is not None and same_data(remote, synced)
    match local and local.kind, remote:
        case None, None:
            return ChangeClass.UNCHANGED, None
        case None, Changed() if kept:
            return ChangeClass.UNCHANGED, None
        case None, Changed():
            return ChangeClass.REMOTE_ONLY, None
        case None, Removed():
            # Removing an object the store doesn't hold changes nothing.
            return (ChangeClass.UNCHANGED if synced is None else ChangeClass.DELETED_REMOTE), None
        case EditKind.CHANGE | EditKind.CREATE, Changed() if local and same_data(remote, local):
            return ChangeClass.UNCHANGED, None
        case EditKind.CHANGE, None | Changed() if remote is None or kept:
            return ChangeClass.LOCAL_ONLY, None
        case EditKind.DELETE, None | Changed() if remote is None or kept:
            return ChangeClass.DELETED_LOCAL, None
        case EditKind.CHANGE, Changed():
            return ChangeClass.CONFLICT, ConflictKind.BOTH_CHANGED
        case EditKind.CHANGE, Removed():
            return ChangeClass.CONFLICT, ConflictKind.REMOVED_REMOTELY
        case EditKind.DELETE, Changed():
            return ChangeClass.CONFLICT, ConflictKind.DELETED_LOCALLY
        case EditKind.DELETE, Removed():
            # Both sides deleted the object.
            return ChangeClass.DELETED_REMOTE, None
        case EditKind.CREATE, Changed():
            return ChangeClass.CONFLICT, ConflictKind.BOTH_CREATED
        case _:
            # A create the server has nothing at, or nothing anymore.
            return ChangeClass.LOCAL_ONLY, None
