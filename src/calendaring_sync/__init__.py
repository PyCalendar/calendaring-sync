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
    DuplicateObjectError,
    EditRefusedError,
    OpenConflictError,
    PendingEditError,
    PushInFlightError,
    RecordValueError,
    RemovedRecordError,
    ScopeMismatchError,
    SessionError,
    StaleChangeSetError,
    StaleConflictError,
    StoreBusyError,
    StoreError,
    StoreVersionError,
    TransactionError,
    TransactionPermissionError,
    TransactionRequiredError,
    TrimmedFeedError,
    UnexpectedStoreError,
    UnknownHolderError,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.push import (
    Applied,
    Gone,
    LogicalKeyTaken,
    PushOutcome,
    Rejected,
    Unknown,
)
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.resolution import (
    Delete,
    KeepEdit,
    Merge,
    Resolution,
    TakeServer,
    UpdateHolder,
)
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica, Session, SessionState
from calendaring_sync.store.base import StateStore
from calendaring_sync.store.memory import MemoryStore
from calendaring_sync.store.sqlite import SQLiteStore

__all__ = [
    "Applied",
    "CalendaringSyncError",
    "Change",
    "ChangeSet",
    "Changed",
    "Conflict",
    "ConflictKind",
    "Delete",
    "DroppedScope",
    "DuplicateObjectError",
    "EditKind",
    "EditRefusedError",
    "FeedEntry",
    "FeedEntryKind",
    "Gone",
    "KeepEdit",
    "LocalEdit",
    "LogicalKeyTaken",
    "MemoryStore",
    "Merge",
    "OpenConflictError",
    "PendingEditError",
    "PushInFlightError",
    "PushOutcome",
    "Record",
    "RecordState",
    "RecordValueError",
    "Rejected",
    "Removed",
    "RemovedRecordError",
    "Replica",
    "Resolution",
    "SQLiteStore",
    "Scope",
    "ScopeMismatchError",
    "Session",
    "SessionError",
    "SessionState",
    "StaleChangeSetError",
    "StaleConflictError",
    "StateStore",
    "StoreBusyError",
    "StoreError",
    "StoreVersionError",
    "TakeServer",
    "TransactionError",
    "TransactionPermissionError",
    "TransactionRequiredError",
    "TrimmedFeedError",
    "Unchanged",
    "UnexpectedStoreError",
    "Unknown",
    "UnknownHolderError",
    "UnknownRecordError",
    "UnknownScopeError",
    "UpdateHolder",
    "__version__",
]
