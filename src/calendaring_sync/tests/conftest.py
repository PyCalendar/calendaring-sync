# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What every test file runs on: both stores, and the sync and async adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from calendaring_sync import MemoryStore, SQLiteStore, StateStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Where a test's SQLite store keeps its file."""
    return tmp_path / "s.db"


@pytest.fixture(params=[MemoryStore, SQLiteStore], ids=lambda cls: cls.__name__)
def make_store(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Callable[[], StateStore]]:
    """Open handles on one store, as separate processes would: one shared
    ``MemoryStore``, or a new connection to one SQLite file each time."""
    opened: list[StateStore] = []
    memory = MemoryStore()

    def make() -> StateStore:
        store: StateStore = memory if request.param is MemoryStore else SQLiteStore(db_path)
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
