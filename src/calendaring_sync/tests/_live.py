# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the integration tests of every protocol share, against running servers.

Only the integration tests import this module, so the unit tests' coverage
leaves it out.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import pytest

from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.tests._scenarios import Env, Login, edit_a
from calendaring_sync.tests._tape import Tape


def answers(url: str) -> bool:
    """Whether a server listens at the host and port of ``url``."""
    parts = urlsplit(url)
    try:
        socket.create_connection((parts.hostname, parts.port), timeout=1).close()
    except OSError:
        return False
    return True


def identical_write(env: Env) -> None:
    """An object written again as it was changes nothing the feed reports."""
    env.synced("a")
    head = env.replica.feed_head()
    env.put("a", "one")
    env.sync()
    assert env.replica.read_feed(head) == []


def characters(env: Env) -> None:
    """Text beyond ASCII comes back as it went."""
    env.synced("a", "Café ☕ 会議")
    assert env.summary("a") == "Café ☕ 会議"


def created_and_removed(env: Env) -> None:
    """An object created and removed between two syncs never reaches the store."""
    env.sync()
    env.put("a", "one")
    env.remove("a")
    env.sync()
    assert env.replica.records(env.adapter.scope_id) == []


def removed_and_created(env: Env) -> int:
    """Sync ``a``, have the server remove it and create it again, sync, and return its key.

    Each protocol checks what became of the record: the key returned is
    the one ``a`` had before.
    """
    key = env.synced("a")
    env.remove("a")
    env.put("a", "two")
    env.sync()
    return key


def removed_while_edited(env: Env) -> None:
    """An object the server removes while it's edited here ends as a conflict.

    Depending on the server, the push is refused for the moved state and
    the next sync ingests the removal, or the server answers that the
    object is gone.
    """
    key = edit_a(env)
    env.tape.before(*env.pushes_mine, lambda: env.remove("a"))
    env.sync()
    env.sync()
    assert env.conflict_kind(key) is ConflictKind.REMOVED_REMOTELY


#: What every protocol's integration tests check on every server.
CHECKS = (identical_write, characters, created_and_removed, removed_while_edited)


@contextmanager
def live_environment(
    scenarios: Any, live: tuple[Any, Login], name: str, asynchronous: bool
) -> Iterator[Any]:
    """The environment of a scenario named after ``name`` on a running server.

    The sync and async runs get names, and so objects, of their own, so
    neither has to delete what the other made. ``scenarios`` is the
    protocol's scenario module.
    """
    server, login = live
    name = f"{name}_{'async' if asynchronous else 'sync'}"
    tape = Tape(scenarios.PROTOCOL)
    with scenarios.environment(server, name, tape, login=login, asynchronous=asynchronous) as env:
        yield env


def refused_login(
    scenarios: Any,
    live: tuple[Any, Login],
    asynchronous: bool,
    wrong_login: Callable[[Any, str, str | None], tuple[Any, Callable[[], Any]]],
    error: type[Exception],
) -> None:
    """A sync whose login the server refuses raises ``error``, and the scope keeps its token.

    ``wrong_login`` makes an adapter on the scope that logs in with a wrong
    password, from the environment, the server's URL and the user name, and
    returns it with how to close its client.
    """
    server, (url, username, _) = live
    if not server.checks_passwords:
        pytest.skip(f"{server.name} lets anyone in")
    with live_environment(scenarios, live, "refused_login", asynchronous) as env:
        env.sync()
        adapter, close = wrong_login(env, url, username)
        token = env.replica.scope(adapter.scope_id).token
        try:
            with pytest.raises(error):
                env.run(adapter.sync())
        finally:
            env.run(close())
        assert env.replica.scope(adapter.scope_id).token == token


class EveryProtocol:
    """The integration tests every protocol runs, on each of its servers.

    A protocol's test class subclasses it and sets ``scenarios`` to its
    scenario module.
    """

    scenarios: Any

    @pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
    def test_what_every_protocol_shows(
        self, live: tuple[Any, Login], asynchronous: bool, check: Callable[[Env], object]
    ) -> None:
        with live_environment(self.scenarios, live, check.__name__, asynchronous) as env:
            check(env)
