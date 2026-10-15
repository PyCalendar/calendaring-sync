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
from contextlib import contextmanager
from datetime import datetime
from threading import RLock, get_ident
from types import TracebackType
from typing import Self

from calendaring_sync.error import StoreError, TransactionError
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record
from calendaring_sync.objects.scope import Scope


class StateStore(ABC):
    """Where the core keeps each :term:`scope`, its records, and the :term:`change feed`.

    Every operation runs inside :meth:`transaction`. One thread's
    transaction runs to the end before another's starts, so a store is safe
    to share between threads. Any operation raises
    :class:`~calendaring_sync.error.StoreError` if the store can't read or
    write its data, and never an exception of the storage underneath it.

    The public methods check that they run inside a transaction of the right
    kind, then call a method of the same name with a leading underscore. A
    store implements those, along with :meth:`_begin`, :meth:`_commit` and
    :meth:`_rollback`, and needs no checks of its own. Its ``__init__``
    calls ``super().__init__()`` first.
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
            StoreError: If the store was closed.
            TransactionError: If a transaction is already open in this thread.
            StoreBusyError: If another process kept the store busy for too long.
            StoreError: If the store can't start or keep the transaction.
        """
        with self._lock:
            if self._closed:
                raise StoreError("The store is closed")
            if self._owner == get_ident():
                raise TransactionError("A store transaction is already open in this thread")
            self._begin(write=write)
            self._owner, self._writable = get_ident(), write
            try:
                yield
            except BaseException:
                self._owner = None
                self._rollback()
                raise
            self._owner = None
            try:
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def _require(self, *, write: bool) -> None:
        """Raise unless this thread holds a transaction that allows the operation."""
        if self._owner != get_ident():
            raise TransactionError("Store operations run inside store.transaction()")
        if write and not self._writable:
            raise TransactionError("A transaction opened with write=False can't change the store")

    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with this id, or ``None``."""
        self._require(write=False)
        return self._get_scope(scope_id)

    def put_scope(self, scope: Scope) -> None:
        """Insert the scope, or replace the one with its id."""
        self._require(write=True)
        self._put_scope(scope)

    def delete_scope(self, scope_id: str) -> None:
        """Delete the scope and every record in it."""
        self._require(write=True)
        self._delete_scope(scope_id)

    def scopes(self) -> list[Scope]:
        """Return every scope, ordered by id."""
        self._require(write=False)
        return self._scopes()

    def insert_record(self, record: Record) -> int:
        """Insert a new record and return the :term:`local key` it got.

        The record's own ``local_key`` is ignored. A key is never given out
        twice, even after the record it went to was deleted.

        Raises:
            ValueError: If the record's scope doesn't exist, or another
                record in it has the same object id.
        """
        self._require(write=True)
        if self._get_scope(record.scope) is None:
            raise ValueError(f"No scope {record.scope!r}")
        self._check_unique(record)
        return self._insert_record(record)

    def update_record(self, record: Record) -> None:
        """Replace the record with the same local key.

        Raises:
            ValueError: If no record has that key, or another record in its
                scope has the same object id.
        """
        self._require(write=True)
        stored = self._get_record(record.local_key)
        if stored is None:
            raise ValueError(f"No record with local key {record.local_key}")
        if (stored.scope, stored.object_id) != (record.scope, record.object_id):
            self._check_unique(record)
        self._update_record(record)

    def _check_unique(self, record: Record) -> None:
        """Raise if another record in the record's scope has its object id."""
        if record.object_id is None:
            return
        other = self._find_record(record.scope, record.object_id)
        if other is not None and other.local_key != record.local_key:
            raise ValueError(f"Scope {record.scope!r} already has object {record.object_id!r}")

    def delete_record(self, local_key: int) -> None:
        """Delete the record with this local key."""
        self._require(write=True)
        self._delete_record(local_key)

    def get_record(self, local_key: int) -> Record | None:
        """Return the record with this local key, or ``None``."""
        self._require(write=False)
        return self._get_record(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Return the scope's record with this :term:`object id`, or ``None``."""
        self._require(write=False)
        return self._find_record(scope_id, object_id)

    def scope_records(self, scope_id: str) -> list[Record]:
        """Return every record in the scope, ordered by local key."""
        self._require(write=False)
        return self._scope_records(scope_id)

    def removed_records(self, through: int) -> list[Record]:
        """Return every removed record whose last feed entry is at or below ``through``."""
        self._require(write=False)
        return self._removed_records(through)

    def append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        """Write the next feed entry and return it."""
        self._require(write=True)
        return self._append_entry(scope_id, kind, local_key, object_id, at)

    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        """Return up to ``limit`` kept entries numbered above ``after``, in order."""
        self._require(write=False)
        return self._entries(after, limit)

    def feed_bounds(self) -> tuple[int, int]:
        """Return the number the feed is trimmed through, and the last number written."""
        self._require(write=False)
        return self._feed_bounds()

    def trim_entries(self, through: int) -> None:
        """Delete every entry numbered at or below ``through``.

        :meth:`feed_bounds` reports the highest ``through`` trimmed so far.
        """
        self._require(write=True)
        self._trim_entries(through)

    def close(self) -> None:
        """Close the store. Every later transaction raises ``StoreError``.

        Raises:
            TransactionError: If this thread has a transaction open.
        """
        with self._lock:
            if self._owner == get_ident():
                raise TransactionError("Close the store after its transaction ends")
            if not self._closed:
                self._closed = True
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
    def _scope_records(self, scope_id: str) -> list[Record]: ...

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
