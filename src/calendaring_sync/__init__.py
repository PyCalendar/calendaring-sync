# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Calendar sync state.

Tracks sync tokens and per-object versions across sync cycles, keeps a local
copy of synced calendar objects, and detects conflicts between local and remote
changes.
"""

from calendaring_sync._version import __version__
from calendaring_sync.error import (
    CalendaringSyncError,
    ScopeMismatchError,
    SessionError,
    StaleChangeSetError,
    StoreVersionError,
    TrimmedFeedError,
    UnknownScopeError,
)
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica, Session, SessionState
from calendaring_sync.store.base import StateStore
from calendaring_sync.store.memory import MemoryStore
from calendaring_sync.store.sqlite import SQLiteStore

__all__ = [
    "CalendaringSyncError",
    "Change",
    "ChangeSet",
    "Changed",
    "DroppedScope",
    "FeedEntry",
    "FeedEntryKind",
    "MemoryStore",
    "Record",
    "RecordState",
    "Removed",
    "Replica",
    "SQLiteStore",
    "Scope",
    "ScopeMismatchError",
    "Session",
    "SessionError",
    "SessionState",
    "StaleChangeSetError",
    "StateStore",
    "StoreVersionError",
    "TrimmedFeedError",
    "Unchanged",
    "UnknownScopeError",
    "__version__",
]
