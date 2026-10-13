# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`store` that keeps everything in memory, for tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from calendaring_sync.objects.feed import FeedEntry, FeedEntryKind
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.store.base import LockedStore


@dataclass
class _Data:
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


class MemoryStore(LockedStore):
    """A store held in memory and lost when the process ends.

    A write transaction works on a copy of the data, which replaces the data
    only when the transaction ends normally. Lookups scan every record, so
    it suits test data, not large stores. Meant for tests; use
    :class:`~calendaring_sync.store.sqlite.SQLiteStore` to keep state.
    """

    def __init__(self) -> None:
        super().__init__()
        self._data = _Data()
        self._saved = self._data

    def _begin(self, *, write: bool) -> None:
        self._saved = self._data
        if write:
            self._data = self._data.copy()

    def _commit(self) -> None:
        self._saved = self._data

    def _rollback(self) -> None:
        self._data = self._saved

    def get_scope(self, scope_id: str) -> Scope | None:
        self._check()
        return self._data.scopes.get(scope_id)

    def put_scope(self, scope: Scope) -> None:
        self._check()
        self._data.scopes[scope.id] = scope

    def delete_scope(self, scope_id: str) -> None:
        self._check()
        self._data.scopes.pop(scope_id, None)
        self._data.records = {k: r for k, r in self._data.records.items() if r.scope != scope_id}

    def scopes(self) -> list[Scope]:
        self._check()
        return sorted(self._data.scopes.values(), key=lambda s: s.id)

    def insert_record(self, record: Record) -> int:
        self._check()
        if record.scope not in self._data.scopes:
            raise ValueError(f"No scope {record.scope!r}")
        self._check_unique(record, local_key=None)
        self._data.last_key += 1
        key = self._data.last_key
        self._data.records[key] = replace(record, local_key=key)
        return key

    def update_record(self, record: Record) -> None:
        self._check()
        if record.local_key not in self._data.records:
            raise ValueError(f"No record with local key {record.local_key}")
        self._check_unique(record, local_key=record.local_key)
        self._data.records[record.local_key] = record

    def _check_unique(self, record: Record, local_key: int | None) -> None:
        # The same rule SQLiteStore's unique index enforces.
        if record.object_id is None:
            return
        other = self.find_record(record.scope, record.object_id)
        if other is not None and other.local_key != local_key:
            raise ValueError(f"Scope {record.scope!r} already has object {record.object_id!r}")

    def delete_record(self, local_key: int) -> None:
        self._check()
        self._data.records.pop(local_key, None)

    def get_record(self, local_key: int) -> Record | None:
        self._check()
        return self._data.records.get(local_key)

    def find_record(self, scope_id: str, object_id: str) -> Record | None:
        self._check()
        return next(
            (
                r
                for r in self._data.records.values()
                if r.scope == scope_id and r.object_id == object_id
            ),
            None,
        )

    def scope_records(self, scope_id: str) -> list[Record]:
        self._check()
        return [r for r in self._ordered_records() if r.scope == scope_id]

    def removed_records(self, through: int) -> list[Record]:
        self._check()
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
        self._check()
        self._data.feed_head += 1
        entry = FeedEntry(self._data.feed_head, scope_id, kind, local_key, object_id, at)
        self._data.entries.append(entry)
        return entry

    def entries(self, after: int, limit: int) -> list[FeedEntry]:
        self._check()
        return [e for e in self._data.entries if e.number > after][:limit]

    def feed_bounds(self) -> tuple[int, int]:
        self._check()
        return self._data.feed_trimmed, self._data.feed_head

    def trim_entries(self, through: int) -> None:
        self._check()
        self._data.entries = [e for e in self._data.entries if e.number > through]
        self._data.feed_trimmed = max(self._data.feed_trimmed, through)
