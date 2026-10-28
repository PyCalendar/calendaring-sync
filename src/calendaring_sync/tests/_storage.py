# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A storage written against the storage interface alone, as a third party would."""

from __future__ import annotations

from calendaring_sync.store.base import Storage, StorageTransaction
from calendaring_sync.store.memory import _MemoryStorage


class OneAtATime(Storage):
    """A storage that keeps every default of :class:`Storage`.

    So every transaction waits for the one before it, nothing is released
    on close, and every exception is reported as a bug. It hands the data
    operations to a memory storage, so what's under test is what the store
    adds to a storage it knows nothing about.
    """

    def __init__(self) -> None:
        self._data = _MemoryStorage(None)

    def begin(self, *, write: bool) -> StorageTransaction:
        return self._data.begin(write=write)
