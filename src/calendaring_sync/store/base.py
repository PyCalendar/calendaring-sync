# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The storage interface every :term:`store` implements.

A store only keeps data. Every sync rule lives in
:class:`~calendaring_sync.replica.Replica`, so each store implements the
same small set of typed operations and nothing more.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from threading import RLock, get_ident
from types import TracebackType
from typing import Self

from calendaring_sync.constants import ISSUES_URL
from calendaring_sync.error import (
    CalendaringSyncError,
    DuplicateObjectError,
    RecordValueError,
    StoreError,
    TransactionError,
    TransactionPermissionError,
    TransactionRequiredError,
    UnexpectedStoreError,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.objects._data import NO_DATA, REMOTE, data
from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.objects.edit import EditKind
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope

# The states in which a record holds a pending edit.
_EDIT_STATES = frozenset({RecordState.PENDING, RecordState.IN_FLIGHT, RecordState.CONFLICT})

# The edit each kind of conflict is about, and whether the conflict holds a
# copy of the server's object. A refused create holds none: the server sent
# nothing back but the refusal.
_CONFLICT_SIDES: dict[ConflictKind, tuple[EditKind, bool]] = {
    ConflictKind.BOTH_CHANGED: (EditKind.CHANGE, True),
    ConflictKind.REMOVED_REMOTELY: (EditKind.CHANGE, False),
    ConflictKind.DELETED_LOCALLY: (EditKind.DELETE, True),
    ConflictKind.BOTH_CREATED: (EditKind.CREATE, True),
    ConflictKind.LOGICAL_KEY_TAKEN: (EditKind.CREATE, False),
}


def _check_record(record: Record) -> None:
    """Raise :class:`RecordValueError` if the record holds what its state can't."""
    state, edit, conflict = record.state, record.edit, record.conflict
    # A pending edit in exactly the states that keep one.
    if (edit is not None) != (state in _EDIT_STATES):
        need = "needs" if state in _EDIT_STATES else "can't hold"
        raise RecordValueError(f"A {state} record {need} a local edit")
    # The server's side of a conflict in exactly the conflict state.
    if (conflict is not None) != (state is RecordState.CONFLICT):
        need = "needs" if state is RecordState.CONFLICT else "can't hold"
        raise RecordValueError(f"A {state} record {need} a conflict")
    # No synced content for exactly an object the server doesn't have yet.
    if (record.content is None) != record.is_create:
        raise RecordValueError("A record has no synced content exactly when its edit is a create")
    # A delete holds no copy of the object; a change or a create holds one.
    if edit is not None:
        if edit.kind is EditKind.DELETE and data(edit) != NO_DATA:
            raise RecordValueError("A delete edit can't hold a copy of the object")
        if edit.kind is not EditKind.DELETE and edit.content is None:
            raise RecordValueError(f"A {edit.kind} edit needs the object's content")
    if conflict is None:
        return
    # Each kind of conflict is about one kind of edit.
    edit_kind, server_has_it = _CONFLICT_SIDES[conflict.kind]
    if edit is None or edit.kind is not edit_kind:
        raise RecordValueError(f"A {conflict.kind} conflict needs a {edit_kind} edit")
    # The server's side holds a copy exactly when the server still has the object.
    removed = data(conflict, REMOTE) == NO_DATA and conflict.remote_version is None
    if conflict.remote_content is None and not removed:
        raise RecordValueError("A conflict without the server's content can't hold the rest of it")
    if server_has_it == removed:
        need = "needs" if server_has_it else "can't hold"
        raise RecordValueError(f"A {conflict.kind} conflict {need} the server's copy")
    # Only a refused create names the object holding its logical key.
    if conflict.holder is not None and conflict.kind is not ConflictKind.LOGICAL_KEY_TAKEN:
        raise RecordValueError(f"A {conflict.kind} conflict can't name a holder")


class StateStore(ABC):
    """Where the core keeps each :term:`scope`, its records, and the :term:`change feed`.

    Every operation runs inside :meth:`transaction`: one that only reads runs
    inside any transaction, and one that writes needs a transaction opened
    with ``write=True``. One thread's transaction runs to the end before
    another's starts, so a store is safe to share between threads.

    Besides the errors each method lists, every operation, :meth:`transaction`
    and :meth:`close` can raise:

    - :class:`~calendaring_sync.error.StoreError`, if the store can't read or
      write its data
    - :class:`~calendaring_sync.error.UnexpectedStoreError`, for any other
      failure inside the store, which is a bug

    No exception of the storage underneath a store reaches the caller.

    The public methods check that they run inside a transaction of the right
    kind, then call a method of the same name with a leading underscore. A
    store implements those, along with :meth:`_begin`, :meth:`_commit` and
    :meth:`_rollback`, and needs no checks of its own. It turns the errors of
    the storage it uses into store errors by overriding :meth:`_store_error`.
    Its ``__init__`` calls ``super().__init__()`` first.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._owner: int | None = None
        self._writable = False
        self._closed = False

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[None]:
        """Run the operations inside the block as one transaction.

        Everything the block wrote is kept if it ends normally, and
        nothing is kept if it raises.

        Args:
            write: ``False`` for a block that only reads. Such a block
                can't write, and a store may run it without blocking other
                processes' writes.

        Raises:
            TransactionError: If a transaction is already open in this thread.
            StoreBusyError: If another process kept the store busy for too long.
            StoreError: If the store was closed, or can't start or keep the
                transaction.
        """
        with self._lock:
            if self._closed:
                raise StoreError("The store is closed")
            if self._in_transaction():
                raise TransactionError("A store transaction is already open in this thread")
            self._owner, self._writable = get_ident(), write
            try:
                with self._errors():
                    self._begin(write=write)
                yield
                with self._errors():
                    self._commit()
            except BaseException:
                with self._errors():
                    self._rollback()
                raise
            finally:
                self._owner = None

    def _in_transaction(self) -> bool:
        """Whether this thread holds the store's transaction."""
        return self._owner == get_ident()

    def _operation(self, *, write: bool) -> AbstractContextManager[None]:
        """Check that this thread's transaction allows the operation, then guard its errors."""
        if not self._in_transaction():
            raise TransactionRequiredError("Store operations run inside store.transaction()")
        if write and not self._writable:
            raise TransactionPermissionError(
                "A transaction opened with write=False can't change the store"
            )
        return self._errors()

    @contextmanager
    def _errors(self) -> Iterator[None]:
        """Let only calendaring-sync's own errors out of the block."""
        try:
            yield
        except CalendaringSyncError:
            raise
        except Exception as exc:
            raise self._store_error(exc) from exc

    def _store_error(self, exc: Exception) -> CalendaringSyncError:
        """Return the error to raise for an exception from inside the store.

        A store overrides this to turn the errors of the storage it uses
        into :class:`~calendaring_sync.error.StoreError`, and passes every
        other exception on to this method.
        """
        cls = type(self)
        return UnexpectedStoreError(
            f"{cls.__module__}.{cls.__qualname__} failed unexpectedly with {exc!r}. "
            f"This is a bug; please report it at {ISSUES_URL}"
        )

    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with this id, or ``None``.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._get_scope(scope_id)

    def put_scope(self, scope: Scope) -> None:
        """Insert the scope, or replace the one with its id.

        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True):
            self._put_scope(scope)

    def delete_scope(self, scope_id: str) -> None:
        """Delete the scope and every record in it.

        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True):
            self._delete_scope(scope_id)

    def scopes(self) -> list[Scope]:
        """Return every scope, ordered by id.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._scopes()

    def insert_record(self, record: Record) -> int:
        """Insert a new record and return the :term:`local key` it got.

        The record's own ``local_key`` is ignored. A key is never given out
        twice, even after the record it went to was deleted. Runs inside a
        write transaction.

        Raises:
            RecordValueError: If the record holds what its state can't. A
                record holds an edit exactly when it's pending, in flight or
                in conflict, the server's side of a conflict exactly when
                it's in conflict, and no synced content exactly when its
                edit is a create. An edit holds a copy of the object unless
                it's a delete, a conflict's edit and server's side match its
                kind, and only a refused create's conflict names a holder.
                The record is checked first, so this is raised outside a
                transaction too.
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
            UnknownScopeError: If the record's scope doesn't exist.
            DuplicateObjectError: If another record in the scope has the
                record's object id.
        """
        with self._record_write(record):
            if self._get_scope(record.scope) is None:
                raise UnknownScopeError(f"No scope {record.scope!r}")
            self._check_unique(record)
            return self._insert_record(record)

    def update_record(self, record: Record) -> None:
        """Replace the record with the same local key.

        Runs inside a write transaction.

        Raises:
            RecordValueError: If the record holds what its state can't, as
                for :meth:`insert_record`.
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
            UnknownRecordError: If no record has the record's local key.
            DuplicateObjectError: If another record in the scope has the
                record's object id.
        """
        with self._record_write(record):
            stored = self._get_record(record.local_key)
            if stored is None:
                raise UnknownRecordError(f"No record with local key {record.local_key}")
            if (stored.scope, stored.object_id) != (record.scope, record.object_id):
                self._check_unique(record)
            self._update_record(record)

    def _record_write(self, record: Record) -> AbstractContextManager[None]:
        """Check the record, then the transaction, and guard the write that follows.

        The record comes first: checking it costs less than anything the
        store does, and a bad record is wrong in any transaction.
        """
        _check_record(record)
        return self._operation(write=True)

    def _check_unique(self, record: Record) -> None:
        """Raise if another record in the record's scope has its object id."""
        if record.object_id is None:
            return
        other = self._find_record(record.scope, record.object_id)
        if other is not None and other.local_key != record.local_key:
            raise DuplicateObjectError(
                f"Scope {record.scope!r} already has object {record.object_id!r}"
            )

    def delete_record(self, local_key: int) -> None:
        """Delete the record with this local key.

        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True):
            self._delete_record(local_key)

    def get_record(self, local_key: int) -> Record | None:
        """Return the record with this local key, or ``None``.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._get_record(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Return the scope's record with this :term:`object id`, or ``None``.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._find_record(scope_id, object_id)

    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        """Return the scope's records with this :term:`logical key`, ordered by local key.

        A record matches if its synced side or its :term:`pending edit` has
        the key. Several records can share one. Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._find_by_logical_key(scope_id, logical_key)

    def scope_records(
        self, scope_id: str, *, states: frozenset[RecordState] | None = None
    ) -> list[Record]:
        """Return the scope's records, ordered by local key.

        Runs inside any transaction.

        Args:
            scope_id: The scope to read.
            states: Only the records in one of these states; ``None`` for
                every record. An empty set matches none, without asking the
                store, since not every database takes an empty list.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            if states is not None and not states:
                return []
            return self._scope_records(scope_id, states)

    def removed_records(self, through: int) -> list[Record]:
        """Return the removed records no feed entry after ``through`` is about.

        ``through`` is the number of a :term:`change feed` entry. Each
        record keeps the number of the last entry about it, so these are
        the records removed by that entry or an earlier one, ordered by
        local key. Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._removed_records(through)

    def append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        """Write the next :term:`change feed` entry and return it.

        Its number is one more than the last number written. Runs inside a
        write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True):
            return self._append_entry(scope_id, kind, local_key, object_id, at)

    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        """Return up to ``limit`` kept entries numbered above ``after``, in order.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._entries(after, limit)

    def feed_bounds(self) -> tuple[int, int]:
        """Return the number the feed is trimmed through, and the last number written.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False):
            return self._feed_bounds()

    def trim_entries(self, through: int) -> None:
        """Delete every entry numbered at or below ``through``.

        :meth:`feed_bounds` reports the highest ``through`` trimmed so far.
        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True):
            self._trim_entries(through)

    def close(self) -> None:
        """Close the store. Every later transaction raises ``StoreError``.

        Closing a closed store does nothing.

        Raises:
            TransactionError: If this thread has a transaction open.
            StoreError: If the store couldn't release what it holds open. It
                counts as closed anyway.
        """
        with self._lock:
            if self._in_transaction():
                raise TransactionError("Close the store after its transaction ends")
            if not self._closed:
                self._closed = True
                with self._errors():
                    self._close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _close(self) -> None:  # noqa: B027, most stores hold nothing to release
        """Release what the store holds open."""

    @abstractmethod
    def _begin(self, *, write: bool) -> None:
        """Start a transaction."""

    @abstractmethod
    def _commit(self) -> None:
        """Keep what the transaction wrote."""

    @abstractmethod
    def _rollback(self) -> None:
        """Discard what the transaction wrote."""

    @abstractmethod
    def _get_scope(self, scope_id: str) -> Scope | None: ...

    @abstractmethod
    def _put_scope(self, scope: Scope) -> None: ...

    @abstractmethod
    def _delete_scope(self, scope_id: str) -> None: ...

    @abstractmethod
    def _scopes(self) -> list[Scope]: ...

    @abstractmethod
    def _insert_record(self, record: Record) -> int: ...

    @abstractmethod
    def _update_record(self, record: Record) -> None: ...

    @abstractmethod
    def _delete_record(self, local_key: int) -> None: ...

    @abstractmethod
    def _get_record(self, local_key: int) -> Record | None: ...

    @abstractmethod
    def _find_record(self, scope_id: str, object_id: str) -> Record | None: ...

    @abstractmethod
    def _find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]: ...

    @abstractmethod
    def _scope_records(
        self, scope_id: str, states: frozenset[RecordState] | None
    ) -> list[Record]: ...

    @abstractmethod
    def _removed_records(self, through: int) -> list[Record]: ...

    @abstractmethod
    def _append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry: ...

    @abstractmethod
    def _entries(self, after: int, limit: int) -> list[FeedEntry]: ...

    @abstractmethod
    def _feed_bounds(self) -> tuple[int, int]: ...

    @abstractmethod
    def _trim_entries(self, through: int) -> None: ...
