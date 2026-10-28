# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Calendar sync state: sync tokens, object versions, change tracking, and conflict detection.

An application starts from :class:`~calendaring_sync.replica.Replica`
and a :term:`store`; an :term:`adapter`, or code of your own, tells them
what changed.
Import every public name from ``calendaring_sync`` itself, such as
``from calendaring_sync import Replica, SQLiteStore``, except the
adapters, which each come from their own module.
"""

from calendaring_sync._version import __version__
from calendaring_sync.error import (
    CalendaringSyncError,
    DuplicateObjectError,
    EditRefusedError,
    FeedRangeError,
    IncompleteListingError,
    ListingRequiredError,
    LoneSurrogateError,
    ObjectIdAssignedError,
    OpenConflictError,
    OtherScopeError,
    PendingEditError,
    PushInFlightError,
    RecordValueError,
    RemovedRecordError,
    ScopeGoneError,
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
    UnknownVersionError,
)
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.conflict import Conflict, ConflictKind
from calendaring_sync.objects.dropped_scope import DroppedScope
from calendaring_sync.objects.edit import EditFields, EditKind, LocalEdit
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
    KeepLocal,
    KeepServer,
    Merge,
    Resolution,
    ResolvedConflicts,
    UpdateHolder,
)
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica
from calendaring_sync.session import Session, SessionState
from calendaring_sync.store.base import StateStore, Storage, StorageTransaction
from calendaring_sync.store.memory import MemoryData, MemoryKeeper, MemoryStore
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
    "EditFields",
    "EditKind",
    "EditRefusedError",
    "FeedEntry",
    "FeedEntryKind",
    "FeedRangeError",
    "Gone",
    "IncompleteListingError",
    "KeepLocal",
    "KeepServer",
    "ListingRequiredError",
    "LocalEdit",
    "LogicalKeyTaken",
    "LoneSurrogateError",
    "MemoryData",
    "MemoryKeeper",
    "MemoryStore",
    "Merge",
    "ObjectIdAssignedError",
    "OpenConflictError",
    "OtherScopeError",
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
    "ResolvedConflicts",
    "SQLiteStore",
    "Scope",
    "ScopeGoneError",
    "ScopeMismatchError",
    "Session",
    "SessionError",
    "SessionState",
    "StaleChangeSetError",
    "StaleConflictError",
    "StateStore",
    "Storage",
    "StorageTransaction",
    "StoreBusyError",
    "StoreError",
    "StoreVersionError",
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
    "UnknownVersionError",
    "UpdateHolder",
    "__version__",
]
