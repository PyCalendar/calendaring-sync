# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for the classifier, over every combination of its three inputs.

Rule: zero network calls, zero Docker dependency, all tests are fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

import pytest

from calendaring_sync import (
    Changed,
    ConflictKind,
    EditKind,
    LocalEdit,
    Record,
    RecordState,
    Removed,
)
from calendaring_sync._classify import ChangeClass, classify

_AT = datetime(2026, 10, 1, tzinfo=UTC)

# The inputs, by name. The synced copy holds "fa", the edit "fl" (none for
# a delete), and the server reports nothing, a removal, or one of those.
_SYNCED = {
    "none": None,
    "fa": Record(
        1, "cal", "a", RecordState.SYNCED, "v1", "fa", b"text fa", frozenset(), None, _AT, 0
    ),
}
_REMOTES = {
    "none": None,
    "removed": Removed(),
    "fa": Changed("v2", "fa", b"text fa"),
    "fl": Changed("v2", "fl", b"text fl"),
    "fx": Changed("v2", "fx", b"text fx"),
}
_LOCALS = {
    "none": None,
    "change": LocalEdit(EditKind.CHANGE, b"text fl", "fl", frozenset(), None, _AT),
    "delete": LocalEdit(EditKind.DELETE, None, None, frozenset(), None, _AT),
    "create": LocalEdit(EditKind.CREATE, b"text fl", "fl", frozenset(), None, _AT),
}

_CONFLICT = ChangeClass.CONFLICT

# What each combination classifies as, keyed by (synced, remote, local).
# Combinations missing here have no object, so they raise.
_EXPECTED: dict[tuple[str, str, str], tuple[ChangeClass, ConflictKind | None]] = {
    # No edit: the server's report decides.
    ("none", "removed", "none"): (ChangeClass.UNCHANGED, None),
    ("none", "fa", "none"): (ChangeClass.REMOTE_ONLY, None),
    ("none", "fl", "none"): (ChangeClass.REMOTE_ONLY, None),
    ("none", "fx", "none"): (ChangeClass.REMOTE_ONLY, None),
    ("fa", "none", "none"): (ChangeClass.UNCHANGED, None),
    ("fa", "removed", "none"): (ChangeClass.DELETED_REMOTE, None),
    ("fa", "fa", "none"): (ChangeClass.UNCHANGED, None),
    ("fa", "fl", "none"): (ChangeClass.REMOTE_ONLY, None),
    ("fa", "fx", "none"): (ChangeClass.REMOTE_ONLY, None),
    # A change.
    ("fa", "none", "change"): (ChangeClass.LOCAL_ONLY, None),
    ("fa", "removed", "change"): (_CONFLICT, ConflictKind.REMOVED_REMOTELY),
    ("fa", "fa", "change"): (ChangeClass.LOCAL_ONLY, None),
    ("fa", "fl", "change"): (ChangeClass.UNCHANGED, None),
    ("fa", "fx", "change"): (_CONFLICT, ConflictKind.BOTH_CHANGED),
    # A delete.
    ("fa", "none", "delete"): (ChangeClass.DELETED_LOCAL, None),
    ("fa", "removed", "delete"): (ChangeClass.DELETED_REMOTE, None),
    ("fa", "fa", "delete"): (ChangeClass.DELETED_LOCAL, None),
    ("fa", "fl", "delete"): (_CONFLICT, ConflictKind.DELETED_LOCALLY),
    ("fa", "fx", "delete"): (_CONFLICT, ConflictKind.DELETED_LOCALLY),
    # A create.
    ("none", "none", "create"): (ChangeClass.LOCAL_ONLY, None),
    ("none", "removed", "create"): (ChangeClass.LOCAL_ONLY, None),
    ("none", "fa", "create"): (_CONFLICT, ConflictKind.BOTH_CREATED),
    ("none", "fl", "create"): (ChangeClass.UNCHANGED, None),
    ("none", "fx", "create"): (_CONFLICT, ConflictKind.BOTH_CREATED),
}

# A push in flight whose outcome never arrived classifies the same, except
# that a create coming back at its own id is your own write, whatever the
# server made of its content.
_IN_FLIGHT = _EXPECTED | {
    ("none", "fa", "create"): (ChangeClass.UNCHANGED, None),
    ("none", "fx", "create"): (ChangeClass.UNCHANGED, None),
}

_EVERY = list(product(_SYNCED, _REMOTES, _LOCALS))
_IMPOSSIBLE = [sides for sides in _EVERY if sides not in _EXPECTED]


def _classify(
    synced: str, remote: str, local: str, *, in_flight: bool = False
) -> tuple[ChangeClass, ConflictKind | None]:
    return classify(_SYNCED[synced], _REMOTES[remote], _LOCALS[local], in_flight=in_flight)


@pytest.mark.parametrize("in_flight, expected", [(False, _EXPECTED), (True, _IN_FLIGHT)])
@pytest.mark.parametrize("sides", _EXPECTED, ids="-".join)
def test_every_possible_combination(sides, in_flight, expected):
    assert _classify(*sides, in_flight=in_flight) == expected[sides]


@pytest.mark.parametrize("in_flight", [False, True])
@pytest.mark.parametrize("sides", _IMPOSSIBLE, ids="-".join)
def test_combinations_no_object_can_have_raise(sides, in_flight):
    synced, _, local = sides
    with pytest.raises(ValueError):
        _classify(*sides, in_flight=in_flight)
    # Only these: nothing at all, a create of an object with a synced copy,
    # or a change or delete of one without.
    assert sides == ("none", "none", "none") or (local == "create") == (synced != "none")


def test_every_combination_is_covered():
    assert len(_EXPECTED) + len(_IMPOSSIBLE) == len(_EVERY) == 40
    assert len(_IMPOSSIBLE) == 16
