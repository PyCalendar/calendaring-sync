# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What every test file runs on: the stores, and the sync and async adapters.

The stores are the two that ship and one over a storage written as a third
party would write it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from calendaring_sync import MemoryStore, SQLiteStore, StateStore
from calendaring_sync.tests._storage import OneAtATime


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Where a test's SQLite store keeps its file."""
    return tmp_path / "s.db"


@pytest.fixture(params=[MemoryStore, SQLiteStore, OneAtATime], ids=lambda cls: cls.__name__)
def make_store(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Callable[[], StateStore]]:
    """Open handles on one store, as separate processes would: a new
    connection to one SQLite file each time, or one shared store in memory."""
    opened: list[StateStore] = []
    shared: list[StateStore] = []

    def make() -> StateStore:
        if request.param is SQLiteStore:
            store: StateStore = SQLiteStore(db_path)
        else:
            if not shared:
                shared.append(
                    MemoryStore() if request.param is MemoryStore else StateStore(OneAtATime())
                )
            store = shared[0]
        opened.append(store)
        return store

    yield make
    for store in opened:
        store.close()


@pytest.fixture
def store(make_store: Callable[[], StateStore]) -> StateStore:
    return make_store()


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    """Whether a test uses an adapter's asynchronous variant."""
    return bool(request.param)
