# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Integration tests for the CalDAV adapter, against running servers.

Radicale and Xandikos run embedded. Cyrus, Stalwart and Baïkal are tested
when their Docker fixtures answer, and skipped otherwise; ``record_caldav``
says how to start them. Every test runs the synchronous and the
asynchronous adapter, and each server-specific row of the server
compatibility page has a test here or a scenario run against every server.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import radicale.config
from caldav.collection import Calendar
from caldav.elements import dav
from caldav.lib import error as dav_error

from calendaring_sync import RecordState
from calendaring_sync.tests._caldav_scenarios import (
    SCENARIOS,
    SERVERS,
    Env,
    Login,
    Server,
    adapter_class,
    caldav_client,
    edit_a,
    environment,
    started,
)
from calendaring_sync.tests._caldav_tape import Tape

# Set to run the scenario that needs Stalwart's result limit lowered to 2,
# as record_caldav's docstring says; the other Stalwart tests need the default.
_LIMITED_STALWART = "CALENDARING_SYNC_STALWART_LIMITED"
# How long the Radicale of the expiry test keeps a sync token, in seconds.
_TOKEN_AGE = 1
# Where that Radicale listens, next to the one every other test uses.
_EXPIRY_PORT = 5233


def _answers(url: str) -> bool:
    parts = urlsplit(url)
    try:
        socket.create_connection((parts.hostname, parts.port), timeout=1).close()
    except OSError:
        return False
    return True


@pytest.fixture(scope="session", params=sorted(SERVERS))
def live(request: pytest.FixtureRequest) -> Iterator[tuple[Server, Login]]:
    """A running server and its login."""
    server = SERVERS[request.param]
    if server.login is not None and not _answers(server.login[0]):
        pytest.skip(f"{server.name} isn't running")
    with started(server) as login:
        yield server, login


@contextmanager
def _env(live: tuple[Server, Login], name: str, asynchronous: bool) -> Iterator[Env]:
    """A new calendar named after ``name`` on the server, and the adapter syncing it.

    The sync and async runs get calendars and UIDs of their own, so neither
    has to delete what the other made.
    """
    server, login = live
    name = f"{name}_{'async' if asynchronous else 'sync'}"
    with environment(server, name, Tape(), login=login, asynchronous=asynchronous) as env:
        yield env


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s != "listing_cut_short"])
def test_every_scenario(live, asynchronous, scenario):
    if (live[0].name, scenario, sys.platform) == ("xandikos", "scope_gone", "win32"):
        # Its git store leaves read-only files that the delete can't remove.
        pytest.skip("Xandikos can't delete a calendar holding objects on Windows")
    with _env(live, scenario, asynchronous) as env:
        # DAV:limit holds DAV:nresults; a server can lose changes over it.
        env.never("REPORT", "nresults", "A report asked for a limited number of results")
        SCENARIOS[scenario](env)


def test_a_listing_cut_short_raises(live, asynchronous):
    server, _ = live
    if server.name != "stalwart" or not os.environ.get(_LIMITED_STALWART):
        pytest.skip(
            "Needs Stalwart with WebDav.maxResults at 2 (the evidence branch's"
            f" servers/stalwart_admin.py max-results 2) and {_LIMITED_STALWART} set"
        )
    with _env(live, "listing_cut_short", asynchronous) as env:
        SCENARIOS["listing_cut_short"](env)


def test_a_listing_leaves_out_removed_members(live, asynchronous):
    with _env(live, "listing_after_removal", asynchronous) as env:
        env.put("a", "one")
        env.put("b", "one")
        env.remove("b")
        env.sync()
        assert [r.logical_key for r in env.replica.records(env.adapter.scope_id)] == [
            f"{env.scenario}-a"
        ]


def test_a_member_created_and_removed_between_syncs_leaves_nothing(live, asynchronous):
    with _env(live, "created_and_removed", asynchronous) as env:
        env.sync()
        env.put("a", "one")
        env.remove("a")
        env.sync()
        assert env.replica.records(env.adapter.scope_id) == []


def test_a_member_removed_and_created_again_keeps_its_record(live, asynchronous):
    with _env(live, "removed_and_created", asynchronous) as env:
        key = env.synced("a")
        env.remove("a")
        env.put("a", "two")
        env.sync()
        record = env.record("a")
        assert (record.local_key, record.state) == (key, RecordState.SYNCED)
        assert b"two" in (record.content or b"")


def test_an_identical_write_changes_nothing(live, asynchronous):
    with _env(live, "identical_write", asynchronous) as env:
        env.synced("a")
        head = env.replica.feed_head()
        env.put("a", "one")
        env.sync()
        assert env.replica.read_feed(head) == []


def test_calendar_data_keeps_its_characters(live, asynchronous):
    with _env(live, "characters", asynchronous) as env:
        env.synced("a", "Café ☕ 会議")
        assert "Café ☕ 会議".encode() in (env.record("a").content or b"")


def test_a_refused_login_is_raised_and_keeps_the_token(live, asynchronous):
    server, (base_url, username, _) = live
    if not server.checks_passwords:
        pytest.skip(f"{server.name} lets anyone in")
    with _env(live, "refused_login", asynchronous) as env:
        env.sync()
        client = caldav_client(
            asynchronous, url=base_url, username=username, password="not the password"
        )
        calendar = Calendar(client=client, url=str(env.calendar.url))
        adapter = adapter_class(asynchronous)(env.replica, calendar)
        token = env.replica.scope(adapter.scope_id).token
        try:
            with pytest.raises(dav_error.AuthorizationError):
                env.run(adapter.sync())
        finally:
            env.run(client.close())
        assert env.replica.scope(adapter.scope_id).token == token


def test_an_expired_token_lists_the_calendar_again(asynchronous):
    load = radicale.config.load

    def short_lived_tokens(*args: Any) -> Any:
        configuration = load(*args)
        configuration.update({"storage": {"max_sync_token_age": str(_TOKEN_AGE)}}, "test")
        return configuration

    server = SERVERS["radicale"]
    with (
        patch.object(radicale.config, "load", short_lived_tokens),
        started(server, {"port": _EXPIRY_PORT}) as login,
        _env((server, login), "token_expired", asynchronous) as env,
    ):
        key = edit_a(env)
        time.sleep(_TOKEN_AGE + 1)
        # Radicale drops old tokens when it writes a new one, which another
        # client's change and read of the token makes it do.
        env.put("b", "one")
        env.other.get_properties([dav.SyncToken()])
        env.sync()
        refused = [e for e in env.tape.exchanges if e.method == "REPORT" and e.status == 403]
        assert len(refused) == 1 and "valid-sync-token" in refused[0].body
        assert env.at(key).state is RecordState.SYNCED
