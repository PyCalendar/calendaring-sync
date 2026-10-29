# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every docs example that needs no server, run as the pages show it."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from calendaring_sync import (
    ConflictKind,
    EditKind,
    FeedEntry,
    FeedEntryKind,
    KeepServer,
    MemoryStore,
    RecordState,
    Replica,
    ScopeGoneError,
    SQLiteStore,
    StaleChangeSetError,
    StoreBusyError,
    StoreError,
    TrimmedFeedError,
)
from calendaring_sync.adapters.base import SyncAdapter
from calendaring_sync.tests import _examples
from calendaring_sync.tests._helpers import (
    _SCOPE,
    _conflicted,
    _key,
    _pending,
    _summary,
)
from calendaring_sync.tests._scenarios import done

# The scope and event the docs' examples start from, in their start.py.
_EXAMPLE_SCOPE = "work"


_EXAMPLE_EVENT = "meeting"


def _sync_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception, failing: frozenset[int] = frozenset({1})
) -> list[int]:
    """Make every adapter's sync raise ``error`` on the calls numbered in ``failing``.

    Returns the list of calls, which grows by one with each.
    """
    sync = SyncAdapter.sync
    calls: list[int] = []

    def fails(adapter: SyncAdapter, **kwargs: bool) -> None:
        calls.append(1)
        if len(calls) in failing:
            raise error
        sync(adapter, **kwargs)

    monkeypatch.setattr(SyncAdapter, "sync", fails)
    return calls


def _memory_adapter(asynchronous: bool) -> tuple[Any, Replica, Any]:
    """The example adapter, synced with a server holding a; return the server, replica, adapter."""
    memory = _examples.load("memory_adapter")
    server = memory.MemoryServer()
    server.put("a", b"theirs", if_version=None)
    replica = Replica(MemoryStore())
    adapter = (
        memory.AsyncMemoryAdapter(replica, memory.AsyncMemoryServer(server), _SCOPE)
        if asynchronous
        else memory.MemoryAdapter(replica, server, _SCOPE)
    )
    done(adapter.sync())
    return server, replica, adapter


class TestExamples:
    """The examples' runner, and the quickstart the README shows."""

    def test_every_example_is_run_and_shown(self):
        tested = {
            name.removeprefix("test_")
            for cls in globals().values()
            if isinstance(cls, type) and cls.__name__.startswith("Test")
            for name in vars(cls)
        }
        assert set(_examples.names()) - _examples.LIVE <= tested
        pages = "".join(
            p.read_text(encoding="utf-8") for p in _examples.EXAMPLES.parent.rglob("*.rst")
        )
        shown = {name for name in _examples.names() if f"examples/{name}.py" in pages}
        assert set(_examples.names()) - _examples.SHARED <= shown

    def test_readme_shows_the_quickstart(self):
        quickstart = (_examples.EXAMPLES / "quickstart.py").read_text(encoding="utf-8")
        shown = quickstart.split("# start: sync\n")[1].split("# end: sync\n")[0]
        readme = (_examples.EXAMPLES.parents[1] / "README.md").read_text(encoding="utf-8")
        assert f"```python\n{shown}```\n" in readme

    def test_run_replaces_a_setting(self, tmp_path, monkeypatch):
        (tmp_path / "setting.py").write_text("answer = 1\ndoubled = answer * 2\n")
        monkeypatch.setattr(_examples, "EXAMPLES", tmp_path)
        monkeypatch.setattr(_examples, "load", lambda name: None)
        assert _examples.run("setting", monkeypatch, tmp_path, answer=21)["doubled"] == 42
        with pytest.raises(AssertionError, match=r"^setting has no missing to replace$"):
            _examples.run("setting", monkeypatch, tmp_path, missing=1)

    def test_start(self, tmp_path, monkeypatch):
        ran = _examples.run("start", monkeypatch, tmp_path)
        assert [r.content for r in ran["replica"].records(_EXAMPLE_SCOPE)] == [b"Team meeting"]


class TestWorkWithSyncedData:
    """The examples in the how-tos on working with synced data."""

    @pytest.mark.parametrize(
        "found, content", [(True, b"Team meeting, room 3"), (False, b"Team meeting")]
    )
    def test_local_edits(self, tmp_path, monkeypatch, found, content):
        if not found:
            monkeypatch.setattr(Replica, "find_record", lambda *args: None)
        ran = _examples.run("local_edits", monkeypatch, tmp_path)
        replica = ran["replica"]
        meeting = replica.record(_examples.load("start").meeting.local_key)
        assert (meeting.state, meeting.content) == (RecordState.SYNCED, content)
        assert replica.record(ran["lunch"]).state is RecordState.REMOVED
        assert list(_examples.load("start").server.objects) == [_EXAMPLE_EVENT]

    def test_change_feed(self, tmp_path, monkeypatch):
        ran = _examples.run("change_feed", monkeypatch, tmp_path)
        assert ran["handled"] == [(FeedEntryKind.CHANGED, _EXAMPLE_EVENT)]
        assert (tmp_path / "feed-cursor.txt").read_text() == "1"
        replica = ran["replica"]
        # The meeting the server deleted is purged, and the feed trimmed.
        assert replica.records(_EXAMPLE_SCOPE) == []
        with pytest.raises(TrimmedFeedError):
            replica.read_feed(0)

    def test_change_feed_after_a_trim(self, tmp_path, monkeypatch):
        def trimmed(*args: object, **kwargs: object) -> list[FeedEntry]:
            raise TrimmedFeedError("trimmed")

        monkeypatch.setattr(Replica, "read_feed", trimmed)
        ran = _examples.run("change_feed", monkeypatch, tmp_path)
        assert ran["handled"] == []
        assert (tmp_path / "feed-cursor.txt").read_text() == "1"

    def test_mirror(self, tmp_path, monkeypatch):
        ran = _examples.run("mirror", monkeypatch, tmp_path)
        assert ran["moved"] == [(b"Team meeting, moved",)]
        assert ran["after_delete"] == []

    def test_mirror_after_a_drop(self, tmp_path, monkeypatch):
        ran = _examples.run("mirror", monkeypatch, tmp_path)
        replica = ran["replica"]
        db = sqlite3.connect(tmp_path / "app.db")
        _examples.load("start").server.put("lunch", b"Lunch", if_version=None)
        ran["adapter"].sync()
        ran["mirror"](replica, db)
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)
        replica.drop_scope(_EXAMPLE_SCOPE)
        ran["mirror"](replica, db)
        assert db.execute("SELECT count(*) FROM events").fetchone() == (0,)
        db.close()

    def test_conflicts(self, tmp_path, monkeypatch):
        ran = _examples.run("conflicts", monkeypatch, tmp_path)
        merged = b"Team sync, room 3 / Team sync, at 10"
        meeting = ran["replica"].find_record(_EXAMPLE_SCOPE, _EXAMPLE_EVENT)
        assert (meeting.state, meeting.content) == (RecordState.SYNCED, merged)
        assert _examples.load("start").server.objects[_EXAMPLE_EVENT][1] == merged

    @pytest.mark.parametrize(
        "kind, resolution",
        [(ConflictKind.REMOVED_REMOTELY, KeepServer()), (ConflictKind.DELETED_LOCALLY, None)],
    )
    def test_conflicts_resolver(self, tmp_path, monkeypatch, kind, resolution):
        ran = _examples.run("conflicts", monkeypatch, tmp_path)
        record = SimpleNamespace(conflict=SimpleNamespace(kind=kind))
        assert ran["resolver"](record) == resolution


class TestRunInProduction:
    """The examples in the how-tos on running in production."""

    # Busy once, the sync goes through on the retry; busy every time, it gives up after three.
    @pytest.mark.parametrize(
        "failing, calls, scopes",
        [(frozenset({1}), 2, [_EXAMPLE_SCOPE]), (frozenset({1, 2, 3}), 3, [])],
    )
    def test_sqlite(self, tmp_path, monkeypatch, failing, calls, scopes):
        made = _sync_fails(monkeypatch, StoreBusyError("busy"), failing)
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        _examples.run("sqlite", monkeypatch, tmp_path)
        assert len(made) == calls
        with closing(sqlite3.connect(tmp_path / "sync.db")) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        with SQLiteStore(tmp_path / "sync.db") as store:
            assert [scope.id for scope in Replica(store).scopes()] == scopes

    def test_keeper(self, tmp_path, monkeypatch):
        ran = _examples.run("keeper", monkeypatch, tmp_path)
        assert ran["meeting"].content == b"Team meeting"
        keeper = ran["FileKeeper"](tmp_path / "sync.pickle")
        assert isinstance(keeper.error(OSError("disk full")), StoreError)
        assert keeper.error(ValueError("a bug")) is None

    def test_snapshots(self, tmp_path, monkeypatch):
        ran = _examples.run("snapshots", monkeypatch, tmp_path)
        feed = [(entry.kind, entry.object_id) for entry in ran["replica"].read_feed()]
        # The second, unchanged snapshot adds nothing.
        assert feed == [
            (FeedEntryKind.CHANGED, _EXAMPLE_EVENT),
            (FeedEntryKind.CHANGED, "lunch"),
            (FeedEntryKind.CHANGED, _EXAMPLE_EVENT),
            (FeedEntryKind.REMOVED, "lunch"),
        ]

    def test_parallel(self, tmp_path, monkeypatch):
        ran = _examples.run("parallel", monkeypatch, tmp_path)
        replica = ran["replica"]
        assert [scope.id for scope in replica.scopes()] == ["home", "team", "work"]

    @pytest.mark.parametrize("failing", [frozenset({1}), frozenset({1, 2})])
    def test_parallel_retry(self, tmp_path, monkeypatch, failing):
        ran = _examples.run("parallel", monkeypatch, tmp_path)
        calls = _sync_fails(monkeypatch, StaleChangeSetError("stale"), failing)
        ran["sync"](ran["adapters"][0])
        assert len(calls) == len(failing) + 1

    @pytest.mark.parametrize(
        "busy, unsent, scopes",
        [(False, [b"Team meeting, room 3"], []), (True, [], [_EXAMPLE_SCOPE])],
    )
    def test_errors(self, tmp_path, monkeypatch, busy, unsent, scopes):
        if busy:
            # The first sync is the starting state's, the second the example's.
            _sync_fails(monkeypatch, StoreBusyError("busy"), frozenset({2}))
        ran = _examples.run("errors", monkeypatch, tmp_path)
        assert ran["unsent"] == unsent
        assert [scope.id for scope in ran["replica"].scopes()] == scopes

    def test_inspect_scope(self, tmp_path, monkeypatch):
        ran = _examples.run("inspect_scope", monkeypatch, tmp_path)
        assert [r.edit.content for r in ran["unsent"]] == [b"Team meeting, room 3"]
        assert (ran["conflicts"], ran["blocked"]) == ([], [])
        assert ran["token"] is not None
        # The repairs sent the edit and left the scope listed again.
        meeting = ran["replica"].find_record(_EXAMPLE_SCOPE, _EXAMPLE_EVENT)
        assert (meeting.state, meeting.content) == (RecordState.SYNCED, b"Team meeting, room 3")


class TestBuildOn:
    """The examples in the how-tos on building on calendaring-sync."""

    def test_memory_adapter(self):
        def synced(asynchronous: bool) -> object:
            server, replica, adapter = _memory_adapter(asynchronous)
            created = done(adapter.create(b"new"))
            done(adapter.change(_key(replica), b"ours"))
            done(adapter.sync())
            replica.delete(created)
            # The token expires, so the sync that sends the delete lists the scope again.
            server.oldest = int(server.token()) + 1
            done(adapter.sync())
            records = [(r.local_key, r.state, r.content) for r in replica.records(_SCOPE)]
            return records, sorted(content for _, content in server.objects.values())

        assert synced(asynchronous=True) == synced(asynchronous=False)
        assert synced(asynchronous=False) == (
            [(1, RecordState.SYNCED, b"ours"), (2, RecordState.REMOVED, b"new")],
            [b"ours"],
        )

    @pytest.mark.parametrize("asynchronous", [False, True])
    def test_memory_adapter_refusals(self, asynchronous):
        server, replica, adapter = _memory_adapter(asynchronous)
        key = _key(replica)
        # Written past the server's log, so no sync reports it and only the push meets it.
        server.objects["a"] = ("99", b"theirs, again")
        done(adapter.change(key, b"ours"))
        done(adapter.sync())
        assert _summary(replica.record(key)) == _pending(EditKind.CHANGE)
        del server.objects["a"]
        done(adapter.sync())
        assert _summary(replica.record(key)) == _conflicted(
            EditKind.CHANGE, ConflictKind.REMOVED_REMOTELY
        )
        server.removed = True
        with pytest.raises(ScopeGoneError):
            done(adapter.sync(full_listing=True))

    def test_testing(self, tmp_path, monkeypatch):
        ran = _examples.run("testing", monkeypatch, tmp_path)
        assert ran["replica"].scope(_EXAMPLE_SCOPE).token == "t3"
