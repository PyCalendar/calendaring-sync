# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The adapter base: its drivers, and every recording of every protocol."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import threading
from datetime import datetime

import pytest

from calendaring_sync import (
    MemoryStore,
    Replica,
)
from calendaring_sync.adapters.base import Adapter, InStore, Steps, _arun, _run
from calendaring_sync.tests import _caldav_scenarios, _examples, _jmap_scenarios
from calendaring_sync.tests._helpers import (
    _WAIT,
)
from calendaring_sync.tests._scenarios import run
from calendaring_sync.tests._tape import Tape


def _timeless(value: object) -> object:
    """``value`` with every time left out, to compare two runs that can't share a clock."""
    if isinstance(value, datetime):
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(_timeless(getattr(value, f.name)) for f in dataclasses.fields(value))
    if isinstance(value, (list, tuple)):
        return [_timeless(item) for item in value]
    return value


# Each protocol's scenario module, and each recording of each.
_PROTOCOLS = (_caldav_scenarios, _jmap_scenarios)


_RECORDED = [
    pytest.param(module, server, scenario, id=f"{module.PROTOCOL}-{server}-{scenario}")
    for module in _PROTOCOLS
    for server, scenario in module.RECORDINGS
]


def _close_mid_step(steps: Steps[None], held: threading.Event | None = None) -> None:
    """Start the asynchronous driver on ``steps`` and close it at its first step.

    A store call that waits for ``held`` is still running when the driver
    suspends on it, so the driver can't finish on its own before the close.
    """

    async def close() -> None:
        driver = _arun(steps)
        driver.send(None)
        if held is not None:
            held.set()
        driver.close()

    asyncio.run(close())


class TestDriver:
    """The asynchronous driver, closed in the middle of a step."""

    def test_closing_while_a_request_runs_closes_the_steps(self):
        seen: list[str] = []

        def steps() -> Steps[None]:
            try:
                yield asyncio.sleep(_WAIT)
            except GeneratorExit:
                seen.append("closed")
                raise

        _close_mid_step(steps())
        assert seen == ["closed"]

    def test_closing_while_a_store_call_runs_hands_its_outcome_over(self):
        seen: list[object] = []
        later = asyncio.sleep(_WAIT)
        held = threading.Event()

        def returns(value: str) -> str:
            held.wait(_WAIT)
            return value

        def fails() -> None:
            held.wait(_WAIT)
            raise RuntimeError("the store call failed")

        def ends() -> Steps[None]:
            seen.append((yield InStore(returns, ("done",))))

        def is_told() -> Steps[None]:
            try:
                yield InStore(fails)
            except RuntimeError as exc:
                seen.append(str(exc))

        def goes_on() -> Steps[None]:
            seen.append((yield InStore(returns, ("on",))))
            yield later

        for steps in (ends(), is_told(), goes_on()):
            held.clear()
            _close_mid_step(steps, held)
        assert seen == ["done", "the store call failed", "on"]
        # The request it yielded after the store call is closed, never left unawaited.
        assert inspect.getcoroutinestate(later) == inspect.CORO_CLOSED


class TestAsk:
    """A request about the scope, made through the base's ``_ask``."""

    def test_an_error_that_isnt_a_gone_scope_is_raised_unchanged(self):
        adapters = _examples.load("memory_adapter")
        adapter = adapters.MemoryAdapter(Replica(MemoryStore()), adapters.MemoryServer(), "cal")
        error = RuntimeError("refused")

        def request() -> None:
            raise error

        with pytest.raises(RuntimeError) as raised:
            _run(adapter._ask(request))
        assert raised.value is error
        # An adapter that names no error as a gone scope gets every one back unchanged.
        assert Adapter._gone(adapter, error) is None


class TestRecordings:
    """Every recording of every protocol, replayed."""

    @pytest.mark.parametrize("scenarios, server, scenario", _RECORDED)
    def test_every_recording_replays(self, scenarios, server, scenario, store, asynchronous):
        tape = Tape.load(scenarios.PROTOCOL, f"{server}-{scenario}")
        run(
            scenarios,
            scenarios.SERVERS[server],
            scenario,
            tape,
            store=store,
            asynchronous=asynchronous,
        )
        assert tape.finished

    @pytest.mark.parametrize("scenarios, server, scenario", _RECORDED)
    def test_sync_and_async_adapters_leave_the_same_store(self, scenarios, server, scenario):
        def replayed(asynchronous: bool) -> object:
            store = MemoryStore()
            tape = Tape.load(scenarios.PROTOCOL, f"{server}-{scenario}")
            run(
                scenarios,
                scenarios.SERVERS[server],
                scenario,
                tape,
                store=store,
                asynchronous=asynchronous,
            )
            replica = Replica(store)
            scopes = replica.scopes()
            held = (scopes, [replica.records(s.id) for s in scopes], replica.read_feed(0))
            return _timeless(held)

        assert replayed(asynchronous=True) == replayed(asynchronous=False)

    @pytest.mark.parametrize("scenarios", _PROTOCOLS, ids=lambda module: module.PROTOCOL)
    def test_every_scenario_is_recorded(self, scenarios):
        assert {scenario for _, scenario in scenarios.RECORDINGS} == set(scenarios.SCENARIOS)
