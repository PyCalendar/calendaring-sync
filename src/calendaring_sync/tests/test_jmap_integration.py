# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Integration tests for the JMAP adapter, against running servers.

Cyrus and Stalwart are tested when their Docker fixtures answer, and skipped
otherwise; start them with ``tests/docker/start.sh``. A test that syncs runs
the synchronous and the asynchronous adapter, except the docs example, which
runs as its page shows it. Each row of the server compatibility page's JMAP
table has a test here or a scenario run against both servers. Every scenario
deletes the account's events first.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from calendaring_jmap import JMAPAuthError, JMAPClient, JMAPMethodError

from calendaring_sync import RecordState
from calendaring_sync.adapters import jmap as adapter_module
from calendaring_sync.adapters.jmap import PAGE_SIZE
from calendaring_sync.tests import _examples
from calendaring_sync.tests import _jmap_scenarios as scenarios
from calendaring_sync.tests._content import event
from calendaring_sync.tests._jmap_scenarios import (
    SCENARIOS,
    SERVERS,
    Env,
    Server,
    adapter_class,
    jmap_client,
)
from calendaring_sync.tests._live import (
    EveryProtocol,
    answers,
    created_and_removed,
    live_environment,
    refused_login,
    removed_and_created,
)
from calendaring_sync.tests._scenarios import (
    Login,
    edit_a,
)
from calendaring_sync.tests._tape import Exchange

# Set to run the test that makes Stalwart forget its change history, which
# turns every later incremental sync on that server into a full listing
# until the setting is back; the test puts it back when it ends.
_EXPIRING_STALWART = "CALENDARING_SYNC_STALWART_EXPIRY"


# This repo's server fixtures, whose setup scripts the expiry tests reuse.
_FIXTURES = Path(__file__).parents[3] / "tests/docker"


# How long Stalwart's purge task takes to truncate the change log.
_PURGE_SECONDS = 10


@pytest.fixture(scope="session", params=sorted(SERVERS))
def live(request: pytest.FixtureRequest) -> tuple[Server, Login]:
    """A running server and its login."""
    server = SERVERS[request.param]
    if not answers(server.login[0]):
        pytest.skip(f"{server.name} isn't running")
    return server, server.login


_env = partial(live_environment, scenarios)


@contextmanager
def _refused_if(refused: bool, error_type: str) -> Iterator[None]:
    """Expect the block to raise the server's ``error_type`` when ``refused``, and nothing else."""
    if not refused:
        yield
        return
    with pytest.raises(JMAPMethodError) as caught:
        yield
    assert caught.value.error_type == error_type


def _calls(env: Env, method: str, after: int = 0) -> list[Exchange]:
    """The exchanges from the ``after``-th on whose request calls ``method``."""
    return [e for e in env.tape.exchanges[after:] if f'"{method}"' in e.request]


def _expire_cyrus() -> None:
    """Make Cyrus forget the user's change history, as its own expiry job does."""
    subprocess.run(
        ["docker", "exec", _setup("cyrus").CONTAINER, "/usr/cyrus/sbin/cyr_expire"]
        + ["-X", "0", "-D", "0", "-E", "0", "-u", "user1"],
        check=True,
        capture_output=True,
    )


def _setup(server: str) -> SimpleNamespace:
    """What ``server``'s fixture setup defines, run without leaving a bytecode cache beside it."""
    return SimpleNamespace(**runpy.run_path(str(_FIXTURES / server / f"setup_{server}.py")))


def _stalwart_retention(setup: SimpleNamespace, history: int) -> None:
    """Set how many changes Stalwart keeps, and reload so it applies."""
    setup.set_singleton("x:DataRetention/set", {"maxChangesHistory": history})
    setup.reload_settings()


@contextmanager
def _expiring_stalwart() -> Iterator[None]:
    """Stalwart keeping one change for the block; leaving it, its purge runs and history is back."""
    setup = _setup("stalwart")
    kept = setup.admin("x:DataRetention/get", {"ids": ["singleton"]})["list"][0]
    _stalwart_retention(setup, 1)
    try:
        yield
        accounts = setup.admin("x:Account/query", {})["ids"]
        names = setup.admin("x:Account/get", {"ids": accounts, "properties": ["name"]})
        user = next(a["id"] for a in names["list"] if a["name"] == setup.USER[0])
        setup.admin(
            "x:Task/set",
            {
                "create": {
                    "t": {
                        "@type": "AccountMaintenance",
                        "accountId": user,
                        "maintenanceType": "purge",
                    }
                }
            },
        )
        time.sleep(_PURGE_SECONDS)
    finally:
        _stalwart_retention(setup, kept["maxChangesHistory"])


class TestScenarios(EveryProtocol):
    """Every scenario, and what every protocol shows, on every server."""

    scenarios = scenarios

    @pytest.mark.parametrize("scenario", list(SCENARIOS))
    def test_every_scenario_runs(self, live, asynchronous, scenario):
        with _env(live, scenario, asynchronous) as env:
            SCENARIOS[scenario](env)


class TestServers:
    """What a server does that a scenario can't show on every server."""

    def test_a_changed_uid_stays_the_servers(self, live, asynchronous):
        # Both servers keep an event's uid when an update sends another one.
        with _env(live, "uid_kept", asynchronous) as env:
            key = env.synced("a")
            env.change(key, env.content("b", "renamed"))
            env.sync()
            env.sync()
            assert env.at(key).logical_key == env.uid("a")

    def test_a_page_fits_in_one_get(self, live):
        _, (url, username, password) = live
        client = JMAPClient(url=url, username=username, password=password)
        try:
            core = client._get_session().server_capabilities["urn:ietf:params:jmap:core"]
        finally:
            client.close()
        assert core["maxObjectsInGet"] >= PAGE_SIZE

    def test_an_event_created_and_removed_while_paging_never_arrives(self, live, asynchronous):
        # Paged, Cyrus lists it as destroyed: the removal of an unknown id does nothing.
        with (
            _env(live, "created_and_removed_paged", asynchronous) as env,
            mock.patch.object(adapter_module, "PAGE_SIZE", 1),
        ):
            created_and_removed(env)

    def test_an_event_removed_and_created_again_is_a_record_of_its_own(self, live, asynchronous):
        # The new event gets a new id, so its uid comes back on a new record.
        with _env(live, "removed_and_created", asynchronous) as env:
            key = removed_and_created(env)
            records = env.replica.find_by_logical_key(env.adapter.scope_id, env.uid("a"))
            assert [(r.local_key == key, r.state) for r in records] == [
                (True, RecordState.REMOVED),
                (False, RecordState.SYNCED),
            ]

    def test_an_event_in_two_calendars_keeps_both_where_allowed(self, live, asynchronous):
        server, _ = live
        with _env(live, "two_calendars", asynchronous) as env:
            second = env.other.create_calendar(f"calendaring-sync-{env.scenario}")
            try:
                env.sync()
                both = {env.calendar: True, second: True}
                key = env.create(env.content("a", "one", calendarIds=both))
                with _refused_if(not server.keeps_two_calendars, "invalidProperties"):
                    env.sync()
                if server.keeps_two_calendars:
                    # The next sync stores the server's copy, which keeps both.
                    env.sync()
                    record = env.at(key)
                    assert (record.state, record.containers) == (
                        RecordState.SYNCED,
                        frozenset(both),
                    )
                else:
                    assert env.at(key).state is RecordState.IN_FLIGHT
            finally:
                env.other.delete_calendar(
                    second, on_destroy_remove_events=True, account_id=env.account
                )

    def test_a_create_without_a_version_is_refused_where_needed(self, live, asynchronous):
        server, _ = live
        with _env(live, "no_version", asynchronous) as env:
            env.sync()
            data = event(env.uid("a"), "one", env.calendar)
            del data["version"]
            key = env.create(json.dumps(data).encode())
            with _refused_if(server.needs_version, "invalidProperties"):
                env.sync()
            expected = RecordState.IN_FLIGHT if server.needs_version else RecordState.SYNCED
            assert env.at(key).state is expected

    def test_a_refused_login_is_raised_and_keeps_the_token(self, live, asynchronous):
        def wrong_login(env: Env, url: str, username: str | None) -> tuple[Any, Any]:
            client = jmap_client(asynchronous, url=url, username=username, password="not it")
            adapter = adapter_class(asynchronous)(
                env.replica, client, account_id=env.account, scope_id=env.adapter.scope_id
            )
            return adapter, client.aclose if asynchronous else client.close

        refused_login(scenarios, live, asynchronous, wrong_login, JMAPAuthError)

    def test_a_state_past_the_newest_resets_where_the_server_tells(self, live, asynchronous):
        # Cyrus echoes it and reports no changes, which looks like a real
        # answer, so nothing tells the adapter to list again.
        server, _ = live
        with _env(live, "unissued_state", asynchronous) as env:
            key = edit_a(env)
            env.forget_token(f"{env.replica.scope(env.adapter.scope_id).token}0")
            sent = len(env.tape.exchanges)
            env.sync()
            listed = bool(_calls(env, "CalendarEvent/query", sent))
            assert listed is not server.answers_unissued_state
            assert env.at(key).state is RecordState.SYNCED

    def test_an_expired_state_lists_the_account_again(self, live, asynchronous):
        server, _ = live
        if server.name == "stalwart" and not os.environ.get(_EXPIRING_STALWART):
            pytest.skip(
                f"Changes Stalwart's change history for the whole server; set {_EXPIRING_STALWART}"
            )
        with _env(live, "state_expired", asynchronous) as env:
            expiring = _expiring_stalwart() if server.name == "stalwart" else nullcontext()
            with expiring:
                key = edit_a(env)
                # A server forgets the history of what was destroyed, so one event is.
                env.put("b", "one")
                env.put("c", "one")
                env.remove("c")
            if server.name == "cyrus":
                _expire_cyrus()
            env.sync()
            refused = _calls(env, "CalendarEvent/changes")
            assert any(
                "cannotCalculateChanges" in e.body or "invalidArguments" in e.body for e in refused
            )
            assert env.at(key).state is RecordState.SYNCED
            assert env.record("b").state is RecordState.SYNCED


class TestDocsExamples:
    """The docs example that needs a JMAP server."""

    def test_the_docs_example_runs(self, live, tmp_path, monkeypatch):
        _, (url, username, password) = live
        ran = _examples.run(
            "jmap_sync", monkeypatch, tmp_path, URL=url, USERNAME=username, PASSWORD=password
        )
        assert ran["replica"].scope(ran["adapter"].scope_id).last_sync_at is not None
