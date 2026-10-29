# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What every adapter's scenarios share, whatever the protocol.

A protocol's scenario module defines its servers, its environment and its
scenarios; this module gives each one the replay login, the steps and
scenarios every protocol shares, the event loop an asynchronous adapter
runs in, running one scenario, and the recorder's command line.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol, cast

from calendaring_sync import Record, RecordState, Replica, StateStore
from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.tests._tape import Tape

#: The host a replay talks to; recordings keep only paths.
REPLAY_URL = "http://recorded.invalid"
#: The login a replay sends where a recording asked for one; the tape never checks it.
REPLAY_LOGIN = "recorded"

#: A server's URL, user name and password.
Login = tuple[str, str | None, str | None]
#: What a replay logs in to.
REPLAY: Login = (REPLAY_URL, REPLAY_LOGIN, REPLAY_LOGIN)


class Server(Protocol):
    """What the shared steps need to know about a tested server."""

    name: str
    #: A token the server never issued, which it refuses.
    unknown_token: str
    #: Where its Docker fixture listens, or ``None`` for an embedded server.
    login: Login | None


@dataclass
class Env(ABC):
    """One scenario's adapter, store and tape; a protocol adds its second client.

    A protocol's environment writes :meth:`content`, :meth:`summary`, and
    :meth:`put` and :meth:`remove`, which change the server behind the
    adapter's back.
    """

    #: Whether the protocol gives each object a version.
    versioned: ClassVar[bool]
    #: The method and body marker of the request pushing an edit whose
    #: summary starts with ``mine``, such as :func:`edit_a`'s.
    pushes_mine: ClassVar[tuple[str, str]]

    server: Any
    scenario: str
    tape: Tape
    adapter: Any
    run: Callable[[Any], Any]

    @property
    def replica(self) -> Replica:
        replica: Replica = self.adapter._replica
        return replica

    def sync(self, *, full_listing: bool = False) -> None:
        self.run(self.adapter.sync(full_listing=full_listing))

    def create(self, content: bytes) -> int:
        key: int = self.run(self.adapter.create(content))
        return key

    def change(self, local_key: int, content: bytes) -> None:
        self.run(self.adapter.change(local_key, content))

    @abstractmethod
    def content(self, name: str, summary: str) -> bytes:
        """An object whose UID is ``name`` within this scenario, as the adapter takes it."""

    @abstractmethod
    def summary(self, uid: str) -> str:
        """The summary of the object ``uid`` as the store holds it."""

    @abstractmethod
    def put(self, uid: str, summary: str) -> None:
        """Store an object named ``uid`` on the server from the second client."""

    @abstractmethod
    def remove(self, uid: str) -> None:
        """Delete the object named ``uid`` from the server from the second client."""

    def uid(self, name: str) -> str:
        """The UID of the object ``name`` in this scenario.

        UIDs carry the scenario's name, since a server can refuse a UID
        another of the user's calendars holds.
        """
        return f"{self.scenario}-{name}"

    def never(self, method: str, marker: str, what: str) -> None:
        """Fail with ``what`` if a ``method`` request's body ever holds ``marker``."""

        def refuse() -> None:
            raise AssertionError(what)

        self.tape.before(method, marker, refuse)

    def synced(self, uid: str, summary: str = "one") -> int:
        """Put ``uid`` on the server from the second client, sync, and return its key."""
        self.put(uid, summary)
        self.sync()
        return self.record(uid).local_key

    def at(self, local_key: int) -> Record:
        """The record with this local key."""
        record = self.replica.record(local_key)
        assert record is not None
        return record

    def conflict_kind(self, local_key: int) -> ConflictKind | None:
        """The kind of the conflict the record with this local key holds, if it holds one."""
        conflict = self.at(local_key).conflict
        return None if conflict is None else conflict.kind

    def record(self, uid: str) -> Record:
        """The one record whose logical key is the UID of ``uid``."""
        (found,) = self.replica.find_by_logical_key(self.adapter.scope_id, self.uid(uid))
        return found

    def forget_token(self, token: str | None = None) -> None:
        """Give the scope ``token``, by default one the server never issued, as if it expired."""
        store = self.replica._store
        with store.transaction():
            scope = store.get_scope(self.adapter.scope_id)
            assert scope is not None
            store.put_scope(
                replace(scope, token=self.server.unknown_token if token is None else token)
            )


def put_a_and_b(env: Env, *, a: str = "first a", b: str = "first b") -> None:
    """Put ``a`` and ``b`` on the server from the second client, with these summaries."""
    env.put("a", a)
    env.put("b", b)


def sync_a_and_b(env: Env) -> None:
    """Put ``a`` and ``b`` on the server from the second client, sync, and check both came in."""
    put_a_and_b(env)
    env.sync()
    for uid in ("a", "b"):
        record = env.record(uid)
        assert record.state is RecordState.SYNCED
        assert (record.version is not None) is env.versioned


def create_c(env: Env) -> int:
    """Sync the empty scope, record a create of ``c`` here, and return its local key."""
    env.sync()
    return env.create(env.content("c", "made here"))


def edit_a(env: Env) -> int:
    """Sync ``a`` from the server, change it here, and return its local key."""
    key = env.synced("a")
    env.change(key, env.content("a", "mine"))
    return key


def forgotten_edit(env: Env) -> int:
    """:func:`edit_a`, then make the scope's token one the server never issued."""
    key = edit_a(env)
    env.forget_token()
    return key


def rejected_then_conflict(env: Env, local_key: int, kind: ConflictKind) -> None:
    """Sync with the push rejected and its edit pending, then sync into a conflict of ``kind``."""
    env.sync()
    assert env.at(local_key).state is RecordState.PENDING
    env.sync()
    assert env.conflict_kind(local_key) is kind


def reading(env: Env) -> None:
    """A first sync lists the scope; later syncs ingest changes and removals."""
    env.sync()
    assert env.replica.records(env.adapter.scope_id) == []
    sync_a_and_b(env)
    env.put("a", "second a")
    env.remove("b")
    env.sync()
    assert env.summary("a") == "second a"
    assert env.record("b").state is RecordState.REMOVED


def invalid_token(env: Env) -> None:
    """A refused token resets the scope and lists it; a pending edit survives and goes out."""
    key = forgotten_edit(env)
    env.sync()
    assert env.replica.scope(env.adapter.scope_id).token not in (None, env.server.unknown_token)
    assert env.at(key).state is RecordState.SYNCED


def pushing(env: Env) -> None:
    """A create, a change and a delete each go out and are applied.

    A push clears the version, and the next sync stores the server's.
    """
    key = create_c(env)
    env.sync()
    pushed = env.at(key)
    assert pushed.state is RecordState.SYNCED and pushed.object_id is not None
    assert pushed.version is None
    env.sync()
    assert (env.at(key).version is not None) is env.versioned
    env.change(key, env.content("c", "changed here"))
    env.sync()
    env.sync()
    assert env.summary("c") == "changed here"
    env.replica.delete(key)
    env.sync()
    assert env.at(key).state is RecordState.REMOVED


def delete_gone(env: Env, request: tuple[str, str], first: RecordState) -> None:
    """A delete of an object someone else deleted ends with the record removed.

    The second client deletes it right before the first request whose method
    and body marker are ``request``; the sync sending it leaves the record
    ``first``, and the next one removes it.
    """
    key = env.synced("a")
    env.replica.delete(key)
    env.tape.before(*request, lambda: env.remove("a"))
    env.sync()
    assert env.at(key).state is first
    env.sync()
    assert env.at(key).state is RecordState.REMOVED


def listing(env: Env) -> None:
    """A full listing ingests what changed since the last sync."""
    put_a_and_b(env, a="one", b="one")
    env.sync()
    env.put("b", "two")
    env.sync(full_listing=True)
    assert env.summary("b") == "two"
    assert env.record("a").state is RecordState.SYNCED


@contextmanager
def stepper(asynchronous: bool) -> Iterator[Callable[[Any], Any]]:
    """How to run an adapter's step: as it is, or a coroutine in one event loop for the block."""
    if not asynchronous:
        yield lambda step: step
        return
    loop = asyncio.new_event_loop()
    try:
        yield loop.run_until_complete
    finally:
        loop.close()


def by_name(*items: Any, key: str = "name") -> dict[str, Any]:
    """Tested servers by name, or with ``key="__name__"``, scenarios by name."""
    return {getattr(item, key): item for item in items}


def recorded(protocol: str, name: str, keep: int | None = None) -> Tape:
    """The recording ``name`` to replay, cut after its first ``keep`` exchanges."""
    tape = Tape.load(protocol, name)
    if keep is not None:
        del tape.exchanges[keep:]
    return tape


def edit_answer(
    tape: Tape,
    index: int,
    *,
    status: int | None = None,
    old: str | None = None,
    new: str = "",
    body: str | None = None,
) -> None:
    """Change one recorded answer: its status, its whole body, or ``old`` in it to ``new``."""
    exchange = tape.exchanges[index]
    text = exchange.body if body is None else body
    if old is not None:
        assert old in text, f"{old!r} isn't in exchange {index}"
        text = text.replace(old, new)
    tape.exchanges[index] = replace(
        exchange, status=exchange.status if status is None else status, body=text
    )


@contextmanager
def replaying(scenarios: Any, tape: Tape, store: StateStore, asynchronous: bool) -> Iterator[Any]:
    """The environment of the recording ``tape`` replays, which must be played to the end.

    ``scenarios`` is the protocol's scenario module, whose own environment it yields.
    """
    server, scenario = cast(str, tape.name).split("-", 1)
    with scenarios.environment(
        scenarios.SERVERS[server], scenario, tape, store=store, asynchronous=asynchronous
    ) as env:
        yield env
    assert tape.finished


def run(
    scenarios: Any,
    server: Any,
    scenario: str,
    tape: Tape,
    *,
    login: Login | None = None,
    asynchronous: bool = False,
    store: StateStore | None = None,
) -> None:
    """Run one scenario of the protocol's ``scenarios`` module through ``tape``.

    It records from the server at ``login``, or replays at the module's ``REPLAY``.
    """
    with scenarios.environment(
        server,
        scenario,
        tape,
        login=scenarios.REPLAY if login is None else login,
        asynchronous=asynchronous,
        store=store,
    ) as env:
        scenarios.SCENARIOS[scenario](env)


def done(step: Any) -> Any:
    """The result of an adapter call, awaited in a new event loop if it's a coroutine."""
    return asyncio.run(step) if asyncio.iscoroutine(step) else step


def fields_of(held: Any) -> tuple[Any, Any, Any]:
    """The fingerprint, containers and logical key an edit or a read of content holds."""
    return held.fingerprint, held.containers, held.logical_key


def record(scenarios: Any, library: tuple[str, str]) -> None:
    """The recorder's command line: record one server's scenarios, or all its recordings.

    ``scenarios`` is the protocol's scenario module, and ``library`` the
    client library's name and version, kept with each recording.
    """
    protocol, servers = scenarios.PROTOCOL, scenarios.SERVERS
    parser = argparse.ArgumentParser(description=f"Record the {protocol} adapter's exchanges.")
    parser.add_argument("server", choices=sorted(servers))
    parser.add_argument("scenarios", nargs="*", help="scenarios to record; all by default")
    parser.add_argument("--url", help="the server's URL")
    parser.add_argument("--username")
    parser.add_argument("--password")
    args = parser.parse_args()
    wanted = args.scenarios or [s for server, s in scenarios.RECORDINGS if server == args.server]
    server = servers[args.server]
    running: AbstractContextManager[Login] = (
        nullcontext((args.url, args.username, args.password))
        if args.url
        else scenarios.started(server)
    )
    with running as login:
        for scenario in wanted:
            tape = Tape(protocol)
            run(scenarios, server, scenario, tape, login=login)
            tape.save(
                f"{args.server}-{scenario}",
                {
                    "server": args.server,
                    "scenario": scenario,
                    library[0]: library[1],
                    "recorded": dt.date.today().isoformat(),
                },
            )
            print(f"{args.server}-{scenario}: {len(tape.exchanges)} exchanges")
