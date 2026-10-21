# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`store` that keeps everything in memory, and the base of stores that save it."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from calendaring_sync._checks import check_argument_types
from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import StateStore


@dataclass
class _Data:
    """Everything a memory store holds: plain containers of the library's frozen dataclasses."""

    scopes: dict[str, Scope] = field(default_factory=dict)
    records: dict[int, Record] = field(default_factory=dict)
    entries: list[FeedEntry] = field(default_factory=list)
    last_key: int = 0
    feed_head: int = 0
    feed_trimmed: int = 0

    def copy(self) -> _Data:
        # Scopes, records and entries are frozen, so copying the containers
        # is enough for a snapshot.
        return replace(
            self, scopes=dict(self.scopes), records=dict(self.records), entries=list(self.entries)
        )


@check_argument_types
class MemoryStore(StateStore):
    """A store held in memory and lost when the process ends.

    Each transaction works on its own view of the data: a read on the data
    the last write committed, a write on a copy of it, which becomes the
    committed data only when the transaction ends normally. Lookups scan
    every record, so it suits test data, not large stores. Meant for tests;
    use :class:`~calendaring_sync.store.sqlite.SQLiteStore` to keep state.

    To keep the data somewhere else, such as a file written with
    :mod:`pickle` or an object database, subclass it and override two
    methods: ``_load()`` returns the data the last write committed, and
    ``_save(data)`` keeps the data a write commits. ``_load`` runs at the
    start of every transaction, reads included, so a store that reads a
    file there keeps what it last loaded or saved and reads the file again
    only when it changed. The data is one object of plain containers
    holding the library's frozen dataclasses, so it pickles. The checks,
    the transactions and every lookup come from the base classes.

    If ``_save`` fails, the transaction fails and the last saved data
    stays, as long as ``_save`` doesn't stop halfway: write to a new file
    and then replace the old one, for example. Turn the errors of the
    storage underneath into :class:`~calendaring_sync.error.StoreError` by
    overriding ``_store_error``, as
    :class:`~calendaring_sync.store.base.StateStore` describes; anything
    else is reported as a bug.

    Examples:
        A store that keeps its data as pickled bytes, read back by a second
        store:

        >>> import pickle
        >>> from calendaring_sync import Changed, ChangeSet, MemoryStore, Replica
        >>> class PickledStore(MemoryStore):
        ...     def __init__(self, saved=None):
        ...         super().__init__()
        ...         self.saved = saved
        ...     def _load(self):
        ...         return pickle.loads(self.saved) if self.saved else super()._load()
        ...     def _save(self, data):
        ...         self.saved = pickle.dumps(data)
        >>> first = PickledStore()
        >>> listing = ChangeSet(
        ...     "work", None, "t1", {"a": Changed("v1", "fp-a", b"Team meeting")}, full_listing=True
        ... )
        >>> with Replica(first).session(
        ...     "work", fingerprint_scheme="sha256", media_type="text/plain"
        ... ) as session:
        ...     session.ingest(listing)
        >>> Replica(PickledStore(first.saved)).find_record("work", "a").content
        b'Team meeting'
    """

    def __init__(self) -> None:
        super().__init__()
        self._committed = _Data()
        # The open transaction's view of the data.
        self._data = self._committed

    def _load(self) -> _Data:
        """Return the data the last committed write saved."""
        return self._committed

    def _save(self, data: _Data) -> None:
        """Keep the data a write transaction commits, for later transactions to load."""
        self._committed = data

    def _begin(self, *, write: bool) -> None:
        data = self._load()
        self._data = data.copy() if write else data

    def _commit(self) -> None:
        if self._writable:
            self._save(self._data)

    def _rollback(self) -> None:
        # A write worked on a copy that's never saved, so there's nothing to undo.
        pass

    def _get_scope(self, scope_id: str) -> Scope | None:
        return self._data.scopes.get(scope_id)

    def _put_scope(self, scope: Scope) -> None:
        self._data.scopes[scope.id] = scope

    def _delete_scope(self, scope_id: str) -> None:
        self._data.scopes.pop(scope_id, None)
        self._data.records = {k: r for k, r in self._data.records.items() if r.scope != scope_id}

    def _scopes(self) -> list[Scope]:
        return sorted(self._data.scopes.values(), key=lambda s: s.id)

    def _insert_record(self, record: Record) -> int:
        self._data.last_key += 1
        key = self._data.last_key
        self._data.records[key] = replace(record, local_key=key)
        return key

    def _update_record(self, record: Record) -> None:
        self._data.records[record.local_key] = record

    def _delete_record(self, local_key: int) -> None:
        self._data.records.pop(local_key, None)

    def _get_record(self, local_key: int) -> Record | None:
        return self._data.records.get(local_key)

    def _find_record(self, scope_id: str, object_id: str) -> Record | None:
        return next(
            (
                r
                for r in self._data.records.values()
                if r.scope == scope_id and r.object_id == object_id
            ),
            None,
        )

    def _find_by_logical_key(self, scope_id: str, logical_key: str) -> list[Record]:
        return [
            r
            for r in self._scope_records(scope_id, None)
            if logical_key in (r.logical_key, r.edit and r.edit.logical_key)
        ]

    def _scope_records(self, scope_id: str, states: frozenset[RecordState] | None) -> list[Record]:
        return [
            r
            for r in self._ordered_records()
            if r.scope == scope_id and (states is None or r.state in states)
        ]

    def _removed_records(self, through: int) -> list[Record]:
        return [
            r
            for r in self._ordered_records()
            if r.state is RecordState.REMOVED and r.last_entry <= through
        ]

    def _ordered_records(self) -> list[Record]:
        return [record for _, record in sorted(self._data.records.items())]

    def _append_entry(
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

    def _entries(self, after: int, limit: int) -> list[FeedEntry]:
        return [e for e in self._data.entries if e.number > after][:limit]

    def _feed_bounds(self) -> tuple[int, int]:
        return self._data.feed_trimmed, self._data.feed_head

    def _trim_entries(self, through: int) -> None:
        self._data.entries = [e for e in self._data.entries if e.number > through]
        self._data.feed_trimmed = max(self._data.feed_trimmed, through)
