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

from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record
from calendaring_sync.objects.scope import Scope


class StateStore(ABC):
    """Where the core keeps each :term:`scope`, its records, and the :term:`change feed`.

    An implementation runs every operation inside :meth:`transaction` and
    raises :class:`RuntimeError` outside one, so nothing is ever half
    written. It must be safe to call from several threads, running one
    thread's transaction to the end before another's starts.
    :class:`LockedStore` provides both.
    """

    @abstractmethod
    def transaction(self, *, write: bool = True) -> AbstractContextManager[None]:
        """Run the operations inside the block as one transaction.

        Everything the block wrote is kept if it ends normally, and
        nothing is kept if it raises.

        Args:
            write: ``False`` for a block that only reads, which a store
                may run without blocking other processes' writes.

        Raises:
            RuntimeError: If a transaction is already open in this thread.
        """

    @abstractmethod
    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with this id, or ``None``."""

    @abstractmethod
    def put_scope(self, scope: Scope) -> None:
        """Insert the scope, or replace the one with its id."""

    @abstractmethod
    def delete_scope(self, scope_id: str) -> None:
        """Delete the scope and every record in it."""

    @abstractmethod
    def scopes(self) -> list[Scope]:
        """Return every scope, ordered by id."""

    @abstractmethod
    def insert_record(self, record: Record) -> int:
        """Insert a new record and return the :term:`local key` it got.

        The record's own ``local_key`` is ignored. A key is never given out
        twice, even after the record it went to was deleted.

        Raises:
            ValueError: If the record's scope doesn't exist, or another
                record in it has the same object id.
        """

    @abstractmethod
    def update_record(self, record: Record) -> None:
        """Replace the record with the same local key.

        Raises:
            ValueError: If no record has that key.
        """

    @abstractmethod
    def delete_record(self, local_key: int) -> None:
        """Delete the record with this local key."""

    @abstractmethod
    def get_record(self, local_key: int) -> Record | None:
        """Return the record with this local key, or ``None``."""

    @abstractmethod
    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Return the scope's record with this :term:`object id`, or ``None``."""

    @abstractmethod
    def scope_records(self, scope_id: str) -> list[Record]:
        """Return every record in the scope, ordered by local key."""

    @abstractmethod
    def removed_records(self, through: int) -> list[Record]:
        """Return every removed record whose last feed entry is at or below ``through``."""

    @abstractmethod
    def append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        """Write the next feed entry and return it."""

    @abstractmethod
    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        """Return up to ``limit`` kept entries numbered above ``after``, in order."""

    @abstractmethod
    def feed_bounds(self) -> tuple[int, int]:
        """Return the number the feed is trimmed through, and the last number written."""

    @abstractmethod
    def trim_entries(self, through: int) -> None:
        """Delete every entry numbered at or below ``through``.

        :meth:`feed_bounds` reports the highest ``through`` trimmed so far.
        """

    def close(self) -> None:  # noqa: B027, most stores hold nothing to release
        """Release what the store holds open. Does nothing unless a store needs it."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class LockedStore(StateStore):
    """A store whose transactions run one at a time, under one lock.

    Both built-in stores derive from it, so they share how a transaction
    starts, ends and checks that operations run inside it. A subclass only
    says how to begin, commit and roll back, and calls :meth:`_check` at the
    start of every operation.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._owner: int | None = None

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[None]:
        with self._lock:
            if self._owner == get_ident():
                raise RuntimeError("A store transaction is already open in this thread")
            self._begin(write=write)
            self._owner = get_ident()
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

    def _check(self) -> None:
        """Raise unless the calling thread holds the open transaction."""
        if self._owner != get_ident():
            raise RuntimeError("Store operations run inside store.transaction()")

    @abstractmethod
    def _begin(self, *, write: bool) -> None:
        """Start a transaction."""

    @abstractmethod
    def _commit(self) -> None:
        """Keep what the transaction wrote."""

    @abstractmethod
    def _rollback(self) -> None:
        """Discard what the transaction wrote."""
