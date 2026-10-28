# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`store` that keeps everything in memory, and can hand its data to a keeper to save."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime

from calendaring_sync._checks import check_argument_types
from calendaring_sync.error import StoreError, UnexpectedStoreError
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore, Storage, StorageTransaction, _unexpected


@check_argument_types
class MemoryStore(StateStore):
    """A store held in memory, lost when the process ends unless a keeper saves it.

    Each transaction works on its own view of the data: a read on the data
    the last write committed, a write on a copy of it, which becomes the
    committed data only when the transaction ends normally. So a read in
    one thread runs while another thread writes, and sees the data as it
    was when the read started. Lookups scan every record, so it suits test
    data and small stores; use
    :class:`~calendaring_sync.store.sqlite.SQLiteStore` for large ones.

    Args:
        keeper: Where to save the data, such as a file, so the next process
            can load it. ``None`` keeps it only in memory.

    Raises:
        StoreError: If the keeper can't load the data, as its
            :meth:`~MemoryKeeper.error` says.

    Examples:
        A keeper that saves the data as pickled bytes, and a second store
        loaded from them:

        >>> import pickle
        >>> from calendaring_sync import Changed, ChangeSet, MemoryKeeper, MemoryStore, Replica
        >>> class Pickled(MemoryKeeper):
        ...     def __init__(self, saved=None):
        ...         self.saved = saved
        ...     def load(self):
        ...         return pickle.loads(self.saved) if self.saved else None
        ...     def save(self, data):
        ...         self.saved = pickle.dumps(data)
        >>> keeper = Pickled()
        >>> listing = ChangeSet(
        ...     "work", None, "t1", {"a": Changed("v1", "fp-a", b"Team meeting")}, full_listing=True
        ... )
        >>> with Replica(MemoryStore(keeper=keeper)).session(
        ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
        ... ) as session:
        ...     session.ingest(listing)
        >>> store = MemoryStore(keeper=Pickled(keeper.saved))
        >>> Replica(store).find_record("work", "a").content
        b'Team meeting'
    """

    def __init__(self, *, keeper: MemoryKeeper | None = None) -> None:
        storage = _MemoryStorage(keeper)
        super().__init__(storage)
        with self._errors():
            storage.load()


class MemoryKeeper(ABC):
    """Keeps a :class:`MemoryStore`'s data somewhere else, such as in a file.

    Pass one to :class:`MemoryStore`. It loads the data once, when the
    store opens, and saves it each time a write commits; reads come from
    memory. If :meth:`save` raises, the write fails and the store keeps
    the data it saved last, as long as :meth:`save` doesn't stop halfway:
    write to a new file and then replace the old one, for example.

    What :class:`MemoryData` holds can change between releases, so data a
    keeper saved with one release may not load in another. Keep sync state
    that has to outlive an upgrade in
    :class:`~calendaring_sync.store.sqlite.SQLiteStore`, whose file a newer
    release reads.
    """

    @abstractmethod
    def load(self) -> MemoryData | None:
        """Return the data saved last, or ``None`` for a store with nothing saved yet."""

    @abstractmethod
    def save(self, data: MemoryData) -> None:
        """Keep the data a write commits, in place of what was saved before."""

    def error(self, exc: Exception) -> StoreError | None:
        """Return the store error for an exception :meth:`load` or :meth:`save` raised.

        Turn the failures of the storage underneath, such as a full disk,
        into :class:`~calendaring_sync.error.StoreError`; anything this
        returns ``None`` for is reported as a bug.
        """
        return None


@dataclass
class MemoryData:
    """Everything a :class:`MemoryStore` holds, as one object.

    A :class:`MemoryKeeper` saves and loads it whole. It's made of plain
    containers holding the library's frozen dataclasses, so it pickles;
    what's inside isn't part of the API.
    """

    scopes: dict[str, Scope] = field(default_factory=dict)
    records: dict[int, Record] = field(default_factory=dict)
    entries: list[FeedEntry] = field(default_factory=list)
    last_key: int = 0
    feed_head: int = 0
    feed_trimmed: int = 0

    def _copy(self) -> MemoryData:
        # Scopes, records and entries are frozen, so copying the containers
        # is enough for a snapshot.
        return replace(
            self, scopes=dict(self.scopes), records=dict(self.records), entries=list(self.entries)
        )


class _MemoryStorage(Storage):
    """The data of a memory store, and the keeper it's saved with."""

    # A read works on the data the last write committed, which nothing changes.
    reads_beside_writes = True

    def __init__(self, keeper: MemoryKeeper | None) -> None:
        self._keeper = keeper
        self._committed = MemoryData()

    def load(self) -> None:
        """Take the data the keeper saved last."""
        if self._keeper is not None:
            try:
                self._committed = self._keeper.load() or MemoryData()
            except Exception as exc:
                raise self._keeper_error(self._keeper, exc) from exc

    def begin(self, *, write: bool) -> StorageTransaction:
        data = self._committed
        return _MemoryTransaction(self, data._copy() if write else data, write=write)

    def keep(self, data: MemoryData) -> None:
        """Make a write's data the committed data, once the keeper saved it."""
        if self._keeper is not None:
            try:
                self._keeper.save(data)
            except Exception as exc:
                raise self._keeper_error(self._keeper, exc) from exc
        self._committed = data

    @staticmethod
    def _keeper_error(keeper: MemoryKeeper, exc: Exception) -> StoreError | UnexpectedStoreError:
        """The error for a keeper's failure: its own store error, or a bug in the keeper."""
        return keeper.error(exc) or _unexpected(type(keeper), exc)


class _MemoryTransaction(StorageTransaction):
    """A transaction over the committed data for a read, or over a copy of it for a write."""

    def __init__(self, storage: _MemoryStorage, data: MemoryData, *, write: bool) -> None:
        super().__init__(write=write)
        self._storage = storage
        self._data = data

    def commit(self) -> None:
        if self.write:
            self._storage.keep(self._data)

    def rollback(self) -> None:
        # A write worked on a copy that's never kept, so dropping it undoes everything.
        pass

    def get_scope(self, scope_id: str) -> Scope | None:
        return self._data.scopes.get(scope_id)

    def put_scope(self, scope: Scope) -> None:
        self._data.scopes[scope.id] = scope

    def delete_scope(self, scope_id: str) -> None:
        self._data.scopes.pop(scope_id, None)
        self._data.records = {k: r for k, r in self._data.records.items() if r.scope != scope_id}

    def scopes(self) -> list[Scope]:
        return sorted(self._data.scopes.values(), key=lambda s: s.id)

    def insert_record(self, record: Record) -> int:
        self._data.last_key += 1
        key = self._data.last_key
        self._data.records[key] = replace(record, local_key=key)
        return key

    def update_record(self, record: Record) -> None:
        self._data.records[record.local_key] = record

    def delete_record(self, local_key: int) -> None:
        self._data.records.pop(local_key, None)

    def get_record(self, local_key: int) -> Record | None:
        return self._data.records.get(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        return next(
            (
                r
                for r in self._data.records.values()
                if r.scope == scope_id and r.object_id == object_id
            ),
            None,
        )

    def find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        return [
            r
            for r in self.scope_records(scope_id)
            if logical_key in (r.logical_key, r.edit and r.edit.logical_key)
        ]

    def scope_records(self, scope_id: str) -> list[Record]:
        return [r for r in self._ordered_records() if r.scope == scope_id]

    def removed_records(self, through: int) -> list[Record]:
        return [
            r
            for r in self._ordered_records()
            if r.state is RecordState.REMOVED and r.last_entry <= through
        ]

    def _ordered_records(self) -> list[Record]:
        return [record for _, record in sorted(self._data.records.items())]

    def append_entry(
        self,
        scope_id: str,
        kind: FeedEntryKind,
        local_key: int | None,
        object_id: str | None,
        at: datetime,
    ) -> FeedEntry:
        self._data.feed_head += 1
        entry = FeedEntry(self._data.feed_head, scope_id, kind, local_key, object_id, at)
        self._data.entries.append(entry)
        return entry

    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        return [e for e in self._data.entries if e.number > after][:limit]

    def feed_bounds(self) -> tuple[int, int]:
        return self._data.feed_trimmed, self._data.feed_head

    def trim_entries(self, through: int) -> None:
        self._data.entries = [e for e in self._data.entries if e.number > through]
        self._data.feed_trimmed = max(self._data.feed_trimmed, through)
