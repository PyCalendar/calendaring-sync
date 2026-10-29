# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The :term:`store` interface, and the :term:`storage` interface a store keeps its data in.

A store only keeps data. Every sync rule lives in
:class:`~calendaring_sync.replica.Replica`, and every rule about how a store
behaves, such as its transactions, its checks and its errors, lives in
:class:`StateStore`. Where the data goes is a :class:`Storage`, which
implements the same small set of typed operations and nothing more.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from threading import Condition, Lock, local
from types import TracebackType
from typing import Self

from calendaring_sync._checks import check_argument_types
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

# The edits each kind of conflict is about, and whether the conflict holds a
# copy of the server's object. A refused edit holds none: the server sent
# nothing back but the refusal.
_CONFLICT_SIDES: dict[ConflictKind, tuple[frozenset[EditKind], bool]] = {
    ConflictKind.BOTH_CHANGED: (frozenset({EditKind.CHANGE}), True),
    ConflictKind.REMOVED_REMOTELY: (frozenset({EditKind.CHANGE}), False),
    ConflictKind.DELETED_LOCALLY: (frozenset({EditKind.DELETE}), True),
    ConflictKind.BOTH_CREATED: (frozenset({EditKind.CREATE}), True),
    ConflictKind.LOGICAL_KEY_TAKEN: (frozenset({EditKind.CREATE, EditKind.CHANGE}), False),
}


def _unexpected(cls: type, exc: Exception) -> UnexpectedStoreError:
    """The error for an exception ``cls`` didn't expect: a bug, reported to whoever wrote it."""
    name = f"{cls.__module__}.{cls.__qualname__}"
    ours = cls.__module__.startswith(f"{__name__.partition('.')[0]}.")
    where = f"; please report it at {ISSUES_URL}" if ours else f" in {name}"
    return UnexpectedStoreError(f"{name} failed unexpectedly with {exc!r}. This is a bug{where}")


class _Transaction(local):
    """This thread's open transaction."""

    #: The storage's transaction, or ``None`` when the thread has none open.
    current: StorageTransaction | None = None


def _check_record(record: Record) -> None:
    """Raise :class:`RecordValueError` if the record holds what its state can't."""
    state, edit, conflict = record.state, record.edit, record.conflict
    # A pending edit in exactly the states that keep one.
    if (edit is not None) != (state in _EDIT_STATES):
        need = "needs" if state in _EDIT_STATES else "can't hold"
        raise RecordValueError(f"A {state} record {need} a local edit")
    # The server's copy in a conflict in exactly the conflict state.
    if (conflict is not None) != (state is RecordState.CONFLICT):
        need = "needs" if state is RecordState.CONFLICT else "can't hold"
        raise RecordValueError(f"A {state} record {need} a conflict")
    # No synced copy for exactly an object the server doesn't have yet.
    if (record.content is None) != record.is_create:
        raise RecordValueError("A record has no synced copy exactly when its edit is a create")
    # A delete holds no copy of the object; a change or a create holds one.
    if edit is not None:
        if edit.kind is EditKind.DELETE and data(edit) != NO_DATA:
            raise RecordValueError("A delete edit can't hold a copy of the object")
        if edit.kind is not EditKind.DELETE and edit.content is None:
            raise RecordValueError(f"A {edit.kind} edit needs the object's content")
    if conflict is None:
        return
    # Each kind of conflict is about some kinds of edit.
    edit_kinds, server_has_it = _CONFLICT_SIDES[conflict.kind]
    if edit is None or edit.kind not in edit_kinds:
        kinds = " or ".join(sorted(edit_kinds))
        raise RecordValueError(f"A {conflict.kind} conflict needs a {kinds} edit")
    # The server's copy is there exactly when the server still has the object.
    removed = data(conflict, REMOTE) == NO_DATA and conflict.remote_version is None
    if conflict.remote_content is None and not removed:
        raise RecordValueError("A conflict without the server's content can't hold the rest of it")
    if server_has_it == removed:
        need = "needs" if server_has_it else "can't hold"
        raise RecordValueError(f"A {conflict.kind} conflict {need} the server's copy")
    # Only a refused edit names the object holding its logical key.
    if conflict.holder is not None and conflict.kind is not ConflictKind.LOGICAL_KEY_TAKEN:
        raise RecordValueError(f"A {conflict.kind} conflict can't name a holder")


def _check_unique(current: StorageTransaction, record: Record) -> None:
    """Raise if another record in the record's scope has its object id."""
    if record.object_id is None:
        return
    other = current.find_record(record.scope, record.object_id)
    if other is not None and other.local_key != record.local_key:
        raise DuplicateObjectError(
            f"Scope {record.scope!r} already has object {record.object_id!r}"
        )


@check_argument_types
class StateStore:
    """Where the core keeps each :term:`scope`, its records, and the :term:`change feed`.

    It keeps them in a :class:`Storage`, and adds every rule a store
    follows, whichever storage it uses. Every operation runs inside
    :meth:`transaction`: one that only reads runs inside any transaction,
    and one that writes needs a transaction opened with ``write=True``. A
    store is safe to share between threads: one write transaction runs at
    a time, and where the storage sets
    :attr:`~Storage.reads_beside_writes`, transactions that only read run in
    other threads meanwhile instead of waiting for it.

    Besides the errors each method lists, every operation, :meth:`transaction`
    and :meth:`close` can raise:

    - :class:`TypeError`, for an argument of the wrong type, before anything
      is read or written; a record or scope to write is checked field by
      field
    - :class:`~calendaring_sync.error.StoreError`, if the store can't read or
      write its data
    - :class:`~calendaring_sync.error.UnexpectedStoreError`, for any other
      failure inside the storage, which is a bug

    No exception of the storage underneath reaches the caller.

    Args:
        storage: Where the store keeps its data.
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        # Guards the count of open transactions and closing; close() waits on it.
        self._state = Condition()
        # Held by the write transaction, or by every transaction of a storage
        # that doesn't read beside a write.
        self._turn = Lock()
        self._thread = _Transaction()
        self._open = 0
        self._closed = False

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[None]:
        """Run the operations inside the block as one transaction.

        Everything the block wrote is kept if it ends normally, and
        nothing is kept if it raises.

        Args:
            write: ``False`` for a block that only reads. Such a block
                can't write, and a store may run it beside a write in
                another thread or process.

        Raises:
            TransactionError: If a transaction is already open in this thread.
            StoreBusyError: If another process kept the store busy for too long.
            StoreError: If the store was closed, or can't start or keep the
                transaction.
        """
        with self._state:
            if self._closed:
                raise StoreError("The store is closed")
            if self._in_transaction():
                raise TransactionError("A store transaction is already open in this thread")
            self._open += 1
        try:
            waits = write or not self._storage.reads_beside_writes
            if waits:
                self._turn.acquire()
            try:
                with self._errors():
                    current = self._storage.begin(write=write)
                self._thread.current = current
                try:
                    yield
                    with self._errors():
                        current.commit()
                except BaseException as exc:
                    # What ended the block stays the error, interrupts included;
                    # a rollback that fails too is noted on it.
                    try:
                        with self._errors():
                            current.rollback()
                    except CalendaringSyncError as failed:
                        exc.add_note(f"Rolling the transaction back failed too: {failed}")
                    raise
                finally:
                    self._thread.current = None
            finally:
                if waits:
                    self._turn.release()
        finally:
            with self._state:
                self._open -= 1
                self._state.notify_all()

    def _in_transaction(self) -> bool:
        """Whether this thread has a transaction open."""
        return self._thread.current is not None

    @contextmanager
    def _operation(self, *, write: bool) -> Iterator[StorageTransaction]:
        """Check this thread's transaction allows the operation, then yield it, errors guarded."""
        current = self._thread.current
        if current is None:
            raise TransactionRequiredError("Store operations run inside store.transaction()")
        if write and not current.write:
            raise TransactionPermissionError(
                "A transaction opened with write=False can't change the store"
            )
        with self._errors():
            yield current

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
        """Return the error to raise for an exception from inside the storage."""
        return self._storage.error(exc) or _unexpected(type(self._storage), exc)

    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with this id, or ``None``.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.get_scope(scope_id)

    def put_scope(self, scope: Scope) -> None:
        """Insert the scope, or replace the one with its id.

        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True) as current:
            current.put_scope(scope)

    def delete_scope(self, scope_id: str) -> None:
        """Delete the scope and every record in it.

        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True) as current:
            current.delete_scope(scope_id)

    def scopes(self) -> list[Scope]:
        """Return every scope, ordered by id.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.scopes()

    def insert_record(self, record: Record) -> int:
        """Insert a new record and return the :term:`local key` it got.

        The record's own ``local_key`` is ignored. A key is never given out
        twice, even after the record it went to was deleted. Runs inside a
        write transaction.

        Raises:
            RecordValueError: If the record holds what its state can't. A
                record holds an edit exactly when it's pending, in flight or
                in conflict, the server's copy in a conflict exactly when
                it's in conflict, and no synced copy exactly when its
                edit is a create. An edit holds a copy of the object unless
                it's a delete, a conflict's edit and server's copy match its
                kind, and only a refused create's conflict names a holder.
                The record is checked first, so this is raised outside a
                transaction too.
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
            UnknownScopeError: If the record's scope doesn't exist.
            DuplicateObjectError: If another record in the scope has the
                record's object id.
        """
        with self._record_write(record) as current:
            if current.get_scope(record.scope) is None:
                raise UnknownScopeError(f"No scope {record.scope!r}")
            _check_unique(current, record)
            return current.insert_record(record)

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
        with self._record_write(record) as current:
            stored = current.get_record(record.local_key)
            if stored is None:
                raise UnknownRecordError(f"No record with local key {record.local_key}")
            if (stored.scope, stored.object_id) != (record.scope, record.object_id):
                _check_unique(current, record)
            current.update_record(record)

    def _record_write(self, record: Record) -> AbstractContextManager[StorageTransaction]:
        """Check the record, then the transaction, and guard the write that follows.

        The record comes first: checking it costs less than anything the
        store does, and a bad record is wrong in any transaction.
        """
        _check_record(record)
        return self._operation(write=True)

    def delete_record(self, local_key: int) -> None:
        """Delete the record with this local key.

        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True) as current:
            current.delete_record(local_key)

    def get_record(self, local_key: int) -> Record | None:
        """Return the record with this local key, or ``None``.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.get_record(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Return the scope's record with this :term:`object id`, or ``None``.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.find_record(scope_id, object_id)

    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        """Return the scope's records with this :term:`logical key`, ordered by local key.

        Matches as :meth:`Replica.find_by_logical_key
        <calendaring_sync.replica.Replica.find_by_logical_key>` describes.
        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.find_by_logical_key(scope_id, logical_key)

    def scope_records(
        self, scope_id: str, *, states: frozenset[RecordState] | None = None
    ) -> list[Record]:
        """Return the scope's records, ordered by local key.

        Runs inside any transaction.

        Args:
            scope_id: The scope to read.
            states: Only the records in one of these states; ``None`` for
                every record. An empty set always returns an empty list.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            if states is None:
                return current.scope_records(scope_id)
            return current.scope_records_in(scope_id, states) if states else []

    def removed_records(self, through: int) -> list[Record]:
        """Return the removed records no feed entry after ``through`` is about.

        ``through`` is the number of a :term:`change feed` entry. Each
        record keeps the number of the last entry about it, so these are
        the records removed by that entry or an earlier one, ordered by
        local key. Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.removed_records(through)

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
        with self._operation(write=True) as current:
            return current.append_entry(scope_id, kind, local_key, object_id, at)

    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        """Return up to ``limit`` kept entries numbered above ``after``, in order.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.entries(after, limit)

    def feed_bounds(self) -> tuple[int, int]:
        """Return the number the feed is trimmed through, and the last number written.

        Runs inside any transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
        """
        with self._operation(write=False) as current:
            return current.feed_bounds()

    def trim_entries(self, through: int) -> None:
        """Delete every entry numbered at or below ``through``.

        :meth:`feed_bounds` reports the highest ``through`` trimmed so far.
        Runs inside a write transaction.

        Raises:
            TransactionRequiredError: If no transaction is open in this thread.
            TransactionPermissionError: If the transaction only reads.
        """
        with self._operation(write=True) as current:
            current.trim_entries(through)

    def close(self) -> None:
        """Close the store. Every later transaction raises ``StoreError``.

        Transactions open in other threads end first: closing waits for
        them. Closing a closed store does nothing.

        Raises:
            TransactionError: If this thread has a transaction open.
            StoreError: If the store couldn't release what it holds open. It
                counts as closed anyway.
        """
        with self._state:
            if self._in_transaction():
                raise TransactionError("Close the store after its transaction ends")
            if self._closed:
                return
            # New transactions are refused from here; the open ones end first.
            self._closed = True
            self._state.wait_for(lambda: self._open == 0)
            with self._errors():
                self._storage.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Storage(ABC):
    """Where a :class:`StateStore` keeps its data.

    To keep sync state somewhere new, such as in another database,
    implement this and a :class:`StorageTransaction`, and pass the storage
    to :class:`StateStore`. The store adds every check, transaction rule,
    thread rule and error a store needs, so a storage only reads and writes.
    """

    #: Whether a transaction that only reads can run while another thread's
    #: write transaction is open. With ``False``, every transaction waits for
    #: the one before it to end. Set it to ``True`` only where a read sees the
    #: data as the last commit left it until it ends, whatever a write does
    #: meanwhile; reads in other threads then run at the same time too, so
    #: :meth:`begin` and the transactions it returns must be safe for that.
    reads_beside_writes: bool = False

    @abstractmethod
    def begin(self, *, write: bool) -> StorageTransaction:
        """Start a transaction and return it.

        The store runs one write transaction at a time, and transactions
        that only read beside it only if :attr:`reads_beside_writes` is
        set. If this raises, it leaves nothing open.
        """

    def close(self) -> None:  # noqa: B027, a storage may hold nothing to release
        """Release what the storage holds open.

        The store calls it once, after every transaction ended.
        """

    def error(self, exc: Exception) -> StoreError | None:
        """Return the store error for an exception the storage raised, or ``None``.

        Turn the failures of whatever the storage uses, such as a file it
        can't open, into :class:`~calendaring_sync.error.StoreError` or one
        of its subclasses. The store reports any exception this returns
        ``None`` for as a bug in the storage.
        """
        return None


class StorageTransaction(ABC):
    """One transaction on a :class:`Storage`, which :meth:`Storage.begin` returns.

    It holds whatever its transaction needs, such as a connection or a view
    of the data, so a storage keeps no state per thread. Its data operations
    are the ones :class:`StateStore` offers, without the checks:
    :class:`StateStore` calls them only inside this transaction, after it
    checked the arguments, the records and whether the transaction may
    write, and turns every exception they raise into a store error. Each
    returns what the :class:`StateStore` method of the same name returns.

    Args:
        write: Whether the transaction may write.
    """

    def __init__(self, *, write: bool) -> None:
        #: Whether the transaction may write.
        self.write = write

    @abstractmethod
    def commit(self) -> None:
        """Keep what the transaction wrote, and end it."""

    @abstractmethod
    def rollback(self) -> None:
        """Discard what the transaction wrote, and end it.

        It's also called after :meth:`commit` raised, so it ends whatever
        the transaction still holds open.
        """

    @abstractmethod
    def get_scope(self, scope_id: str) -> Scope | None:
        """Do what :meth:`StateStore.get_scope` does, without its checks."""

    @abstractmethod
    def put_scope(self, scope: Scope) -> None:
        """Do what :meth:`StateStore.put_scope` does, without its checks."""

    @abstractmethod
    def delete_scope(self, scope_id: str) -> None:
        """Do what :meth:`StateStore.delete_scope` does, without its checks."""

    @abstractmethod
    def scopes(self) -> list[Scope]:
        """Do what :meth:`StateStore.scopes` does, without its checks."""

    @abstractmethod
    def insert_record(self, record: Record) -> int:
        """Do what :meth:`StateStore.insert_record` does, without its checks."""

    @abstractmethod
    def update_record(self, record: Record) -> None:
        """Do what :meth:`StateStore.update_record` does, without its checks."""

    @abstractmethod
    def delete_record(self, local_key: int) -> None:
        """Do what :meth:`StateStore.delete_record` does, without its checks."""

    @abstractmethod
    def get_record(self, local_key: int) -> Record | None:
        """Do what :meth:`StateStore.get_record` does, without its checks."""

    @abstractmethod
    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        """Do what :meth:`StateStore.find_record` does, without its checks."""

    @abstractmethod
    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        """Do what :meth:`StateStore.find_by_logical_key` does, without its checks."""

    @abstractmethod
    def scope_records(self, scope_id: str) -> list[Record]:
        """Return every record of the scope, ordered by local key."""

    def scope_records_in(self, scope_id: str, states: frozenset[RecordState]) -> list[Record]:
        """Return the scope's records in one of ``states``, ordered by local key.

        This filters what :meth:`scope_records` returns. A storage that can
        filter where it keeps its records, such as with an index, overrides
        it. It never gets an empty set.
        """
        return [record for record in self.scope_records(scope_id) if record.state in states]

    @abstractmethod
    def removed_records(self, through: int) -> list[Record]:
        """Do what :meth:`StateStore.removed_records` does, without its checks."""

    @abstractmethod
    def append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        """Do what :meth:`StateStore.append_entry` does, without its checks."""

    @abstractmethod
    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        """Do what :meth:`StateStore.entries` does, without its checks."""

    @abstractmethod
    def feed_bounds(self) -> tuple[int, int]:
        """Do what :meth:`StateStore.feed_bounds` does, without its checks."""

    @abstractmethod
    def trim_entries(self, through: int) -> None:
        """Do what :meth:`StateStore.trim_entries` does, without its checks."""
