# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Benchmarks for the work a sync does most: full listings, local edits, pushes, and reads during them.

Run ``python -m calendaring_sync.tests.benchmark`` on ``main`` and on your
branch, on the same machine, and compare the two. Times differ between
machines, so only a comparison on one machine means anything. Single edits
each write to disk on their own, so their time swings with whatever else
the machine is doing; run them a few times before reading anything into it.

Each scenario runs on a fresh ``SQLiteStore`` in a temporary directory. The
unit tests run every scenario at a small size, so they keep working as the
library changes.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import Event

from calendaring_sync import (
    Applied,
    Changed,
    ChangeSet,
    FeedEntryKind,
    RecordState,
    Replica,
    Session,
    SQLiteStore,
)

#: The number of objects in the scope: a large calendar.
DEFAULT_SIZE = 20_000
#: How many times each scenario runs; the fastest run counts, as the least disturbed.
DEFAULT_RUNS = 3
#: One in this many objects is edited locally.
EDITED_SHARE = 10
#: One in this many edited objects is also changed on the server, and becomes a conflict.
CONFLICTED_SHARE = 10

_SCOPE = "calendar"
_SCHEME = "benchmark"
_MEDIA = "text/plain"
# The token of the first listing, and of the second.
_FIRST = "T1"
_SECOND = "T2"
# What every local edit writes.
_LOCAL_CONTENT = b"local"
_LOCAL_FINGERPRINT = "local"


@contextmanager
def _replica() -> Iterator[Replica]:
    """A replica on a fresh SQLite store, removed afterwards."""
    with tempfile.TemporaryDirectory() as directory, SQLiteStore(Path(directory) / "s.db") as store:
        yield Replica(store)


def _listing(size: int, since: str | None, token: str, changed: int = 0) -> ChangeSet:
    """A full listing of ``size`` objects at version ``token``.

    The first ``changed`` objects hold content the server changed.
    """
    objects = {}
    for i in range(size):
        fingerprint = f"server {i}" if i < changed else str(i)
        objects[f"o{i}"] = Changed(token, fingerprint, f"content {fingerprint}".encode())
    return ChangeSet(_SCOPE, since, token, objects, full_listing=True)


def _session(replica: Replica) -> AbstractContextManager[Session]:
    """A session for the benchmark's scope."""
    return replica.session(_SCOPE, fingerprint_scheme=_SCHEME, media_type=_MEDIA)


def _ingest(replica: Replica, change_set: ChangeSet) -> float:
    """Ingest a change set in one session, and return how long it took."""
    started = time.perf_counter()
    with _session(replica) as session:
        session.ingest(change_set)
    return time.perf_counter() - started


def _list_and_edit(replica: Replica, size: int) -> float:
    """List the scope, then change one in ``EDITED_SHARE`` objects one edit at a time.

    Returns how long the edits took.
    """
    _ingest(replica, _listing(size, None, _FIRST))
    keys = [record.local_key for record in replica.records(_SCOPE)[: size // EDITED_SHARE]]
    started = time.perf_counter()
    for key in keys:
        replica.change(key, _LOCAL_CONTENT, fingerprint=_LOCAL_FINGERPRINT)
    return time.perf_counter() - started


def _push_edits(replica: Replica) -> float:
    """Ingest an empty change set, then push every edit, and return how long it took.

    Each push is claimed and settled as applied in its own transaction, as an
    adapter does around each request.
    """
    started = time.perf_counter()
    with _session(replica) as session:
        session.ingest(ChangeSet(_SCOPE, _FIRST, _SECOND, {}))
        for record in session.to_push():
            claimed = session.claim(record.local_key)
            assert claimed is not None
            session.settle(claimed, Applied())
    return time.perf_counter() - started


def _relist_over_edits(replica: Replica, size: int) -> float:
    """List the scope, edit some objects, then list it again with new versions; time the second.

    Most objects come back with only a new version. The edited ones the
    server also changed become conflicts.
    """
    _list_and_edit(replica, size)
    conflicted = size // EDITED_SHARE // CONFLICTED_SHARE
    return _ingest(replica, _listing(size, _FIRST, _SECOND, changed=conflicted))


def full_listing(size: int) -> float:
    """The first sync of a scope: a full listing of ``size`` new objects."""
    with _replica() as replica:
        return _ingest(replica, _listing(size, None, _FIRST))


def listing_over_edits(size: int) -> float:
    """A second full listing while some objects hold local edits, a few of them conflicting."""
    with _replica() as replica:
        return _relist_over_edits(replica, size)


def single_edits(size: int) -> float:
    """Local edits to one in ``EDITED_SHARE`` of ``size`` objects, each its own transaction."""
    with _replica() as replica:
        return _list_and_edit(replica, size)


def pushes(size: int) -> float:
    """Pushing local edits to one in ``EDITED_SHARE`` of ``size`` objects, each applied."""
    with _replica() as replica:
        _list_and_edit(replica, size)
        return _push_edits(replica)


def read_during_listing(size: int) -> float:
    """The longest one read in another thread takes while a listing of ``size`` objects ingests."""
    with _replica() as replica:
        _ingest(replica, _listing(size, None, _FIRST))
        key = replica.records(_SCOPE)[0].local_key
        reading, done = Event(), Event()

        def read() -> float:
            longest = 0.0
            reading.set()
            while not done.is_set():
                started = time.perf_counter()
                replica.record(key)
                longest = max(longest, time.perf_counter() - started)
            return longest

        with ThreadPoolExecutor(1) as pool:
            reader = pool.submit(read)
            reading.wait()
            try:
                _ingest(replica, _listing(size, _FIRST, _SECOND, changed=size))
            finally:
                done.set()
            return reader.result()


def states_over_edits(size: int) -> Counter[RecordState]:
    """How many records end in each state in ``listing_over_edits``, to check what it measures."""
    with _replica() as replica:
        _relist_over_edits(replica, size)
        return Counter(record.state for record in replica.records(_SCOPE))


def entries_after_pushes(size: int) -> Counter[FeedEntryKind]:
    """The change feed entries ``pushes`` writes, to check what it measures."""
    with _replica() as replica:
        _list_and_edit(replica, size)
        head = replica.feed_head()
        _push_edits(replica)
        return Counter(entry.kind for entry in replica.read_feed(head))


#: Each scenario by name. Each takes the scope's size and returns the seconds it measured.
SCENARIOS: dict[str, Callable[[int], float]] = {
    "full listing": full_listing,
    "listing over edits and conflicts": listing_over_edits,
    f"single edits to one in {EDITED_SHARE} objects": single_edits,
    f"pushes of one in {EDITED_SHARE} objects": pushes,
    "longest read during a full listing": read_during_listing,
}


def main() -> None:
    """Run every scenario and print the fastest time of each."""
    parser = argparse.ArgumentParser(description="Benchmark calendaring-sync's common work.")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE, help="objects in the scope")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="runs of each scenario")
    args = parser.parse_args()
    for name, scenario in SCENARIOS.items():
        seconds = min(scenario(args.size) for _ in range(args.runs))
        print(f"{name}, in a scope of {args.size:,}: {seconds:.2f} s")


if __name__ == "__main__":
    main()
