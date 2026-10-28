# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the CalDAV adapter does against a server, written once for every way it runs.

Each scenario syncs a calendar with the adapter while a second client
changes the server behind its back, and asserts what the store holds.
``record_caldav`` runs them against real servers and saves each exchange,
the unit tests replay the recordings, and the integration tests run them
against running servers; all three use the synchronous and the
asynchronous adapter, except recording, which uses the synchronous one.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import caldav.aio
import pytest
from caldav.calendarobjectresource import CalendarObjectResource
from caldav.collection import Calendar
from caldav.davclient import DAVClient
from caldav.testing import RadicaleServer, XandikosServer

from calendaring_sync import MemoryStore, RecordState, Replica, StateStore
from calendaring_sync.adapters import caldav as adapter_module
from calendaring_sync.adapters.caldav import AsyncCalDAVAdapter, CalDAVAdapter
from calendaring_sync.error import IncompleteListingError, ScopeGoneError
from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.tests import _scenarios
from calendaring_sync.tests._content import ics
from calendaring_sync.tests._scenarios import (
    REPLAY,
    Login,
    by_name,
    edit_a,
    invalid_token,
    pushing,
    reading,
    rejected_then_conflict,
    stepper,
)
from calendaring_sync.tests._tape import Tape

#: The protocol the recordings speak, which names their folder.
PROTOCOL = "caldav"


@dataclass(frozen=True)
class Server:
    """A tested server: where it runs, and what differs about it."""

    name: str
    #: The path of the user's calendar home, where each scenario makes its calendar.
    home: str
    #: A token the server never issued and refuses with ``valid-sync-token``.
    unknown_token: str
    #: Whether deleting an object that's gone, with ``If-Match``, gets 404 (else 412).
    delete_gone_is_404: bool = True
    #: Where the server listens when started as its Docker fixture's README
    #: says, and the account the fixture creates; ``None`` for a server that
    #: runs embedded.
    login: Login | None = None
    #: Whether the server refuses a wrong password; embedded ones and
    #: Cyrus' fixture let anyone in.
    checks_passwords: bool = False


SERVERS = by_name(
    Server("radicale", "/user1/", "http://example.invalid/never-issued"),
    # Xandikos answers a token that isn't hex with a 500 and no precondition.
    Server("xandikos", "/sometestuser/calendars/", "0" * 40),
    Server(
        "cyrus",
        "/dav/calendars/user/user1/",
        "data:,0-0",
        login=("http://localhost:8802", "user1", "x"),
    ),
    Server(
        "stalwart",
        "/dav/cal/testuser%40example.org/",
        "http://example.invalid/never-issued",
        login=("http://localhost:8809", "testuser@example.org", "testcaldav"),
        checks_passwords=True,
    ),
    # Baïkal takes a well-formed token past its newest as valid, and
    # answers it with no changes; only another form is refused.
    Server(
        "baikal",
        "/dav.php/calendars/test/",
        "http://example.invalid/never-issued",
        delete_gone_is_404=False,
        login=("http://127.0.0.1:8811", "test", "test"),
        checks_passwords=True,
    ),
)
_EMBEDDED = {"radicale": RadicaleServer, "xandikos": XandikosServer}


@contextmanager
def started(server: Server, config: dict[str, Any] | None = None) -> Iterator[Login]:
    """The login of ``server``: its Docker fixture's, or an embedded one's, started for the block.

    ``config`` configures an embedded server, as ``caldav.testing`` takes it.
    """
    if server.login is not None:
        yield server.login
        return
    embedded = _EMBEDDED[server.name](config)
    embedded.start()
    try:
        parts = urlsplit(embedded.url)
        yield f"{parts.scheme}://{parts.netloc}", embedded.username, embedded.password
    finally:
        embedded.stop()


@dataclass
class Env(_scenarios.Env):
    """One scenario's adapter, store, tape, and the calendar through a second client."""

    calendar: Calendar
    other: Calendar
    versioned = True
    pushes_mine = ("PUT", "SUMMARY:mine")

    def content(self, name: str, summary: str) -> bytes:
        return ics(self.uid(name), summary)

    def summary(self, uid: str) -> str:
        # Calendar data from a report comes back with LF line endings.
        lines = (self.record(uid).content or b"").decode().splitlines()
        return next(line.removeprefix("SUMMARY:") for line in lines if line.startswith("SUMMARY:"))

    def _member(self, uid: str) -> CalendarObjectResource:
        """``<uid>.ics`` in the calendar, through the second client."""
        path = f"{calendar_path(self.server, self.scenario)}{uid}.ics"
        return CalendarObjectResource(client=self.other.client, url=path, parent=self.other)

    def put(self, uid: str, summary: str) -> None:
        """Store an object on the server from the second client, at ``<uid>.ics``."""
        self._member(uid).save(raw_data=self.content(uid, summary))

    def remove(self, uid: str) -> None:
        """Delete ``<uid>.ics`` from the server from the second client."""
        self._member(uid).delete()


def edit_a_changed_behind(env: Env) -> int:
    """:func:`edit_a`, with the second client changing ``a`` right before the push."""
    key = edit_a(env)
    env.tape.before(*env.pushes_mine, lambda: env.put("a", "theirs"))
    return key


def rejected(env: Env) -> None:
    """A change whose object changed since the ingest is rejected, then becomes a conflict."""
    rejected_then_conflict(env, edit_a_changed_behind(env), ConflictKind.BOTH_CHANGED)


def delete_gone(env: Env) -> None:
    """:func:`_scenarios.delete_gone`; a server that answers 404 has the record removed at once."""
    first = RecordState.REMOVED if env.server.delete_gone_is_404 else RecordState.PENDING
    _scenarios.delete_gone(env, ("DELETE", ""), first)


def uid_taken(env: Env) -> None:
    """A create whose UID another object took meanwhile becomes a conflict."""
    env.sync()
    key = env.create(env.content("dup", "mine"))
    env.tape.before("REPORT", "calendar-query", lambda: env.put("dup", "theirs"))
    env.sync()
    assert env.conflict_kind(key) is ConflictKind.LOGICAL_KEY_TAKEN


def scope_gone(env: Env) -> None:
    """A calendar deleted on the server raises, and the store keeps what it had."""
    env.synced("a")
    env.other.delete()
    with pytest.raises(ScopeGoneError):
        env.sync()
    assert env.record("a").state is RecordState.SYNCED


def listing(env: Env) -> None:
    """A full listing fetches only the objects whose ETag changed, and never the calendar."""
    env.never(
        "REPORT",
        f"{calendar_path(env.server, env.scenario)}<",
        "A multiget asked for the calendar itself",
    )
    _scenarios.listing(env)


def listing_cut_short(env: Env) -> None:
    """A listing the server cuts short raises, and nothing is ingested."""
    for uid in ("a", "b", "c"):
        env.put(uid, "one")
    with pytest.raises(IncompleteListingError):
        env.sync()
    assert env.replica.records(env.adapter.scope_id) == []


def fetch_after_delete(env: Env) -> None:
    """An object deleted between the report and the fetch waits for the next report."""
    env.synced("a")
    env.put("a", "two")
    env.tape.before("REPORT", "calendar-multiget", lambda: env.remove("a"))
    env.sync()
    assert b"one" in (env.record("a").content or b"")
    env.sync()
    assert env.record("a").state is RecordState.REMOVED


SCENARIOS: dict[str, Callable[[Env], None]] = by_name(
    reading,
    pushing,
    rejected,
    delete_gone,
    uid_taken,
    invalid_token,
    scope_gone,
    listing,
    listing_cut_short,
    fetch_after_delete,
    key="__name__",
)

#: Each recording: the server and the scenario.
RECORDINGS = [
    ("radicale", "reading"),
    ("radicale", "pushing"),
    ("radicale", "rejected"),
    ("radicale", "delete_gone"),
    ("radicale", "uid_taken"),
    ("radicale", "invalid_token"),
    ("radicale", "scope_gone"),
    ("radicale", "listing"),
    ("xandikos", "reading"),
    ("xandikos", "invalid_token"),
    ("xandikos", "fetch_after_delete"),
    ("cyrus", "reading"),
    ("cyrus", "invalid_token"),
    ("stalwart", "reading"),
    ("stalwart", "listing_cut_short"),
    ("baikal", "delete_gone"),
    ("baikal", "fetch_after_delete"),
]


@contextmanager
def _fixed_hrefs() -> Iterator[None]:
    """Give creates the same hrefs in a recording and its replays."""
    numbers = itertools.count(1)
    with mock.patch.object(adapter_module.uuid, "uuid4", lambda: uuid.UUID(int=next(numbers))):
        yield


def caldav_client(asynchronous: bool, **settings: Any) -> Any:
    """A python-caldav client made with ``settings``, asynchronous or not."""
    return (caldav.aio.AsyncDAVClient if asynchronous else DAVClient)(**settings)


def adapter_class(asynchronous: bool) -> type[CalDAVAdapter] | type[AsyncCalDAVAdapter]:
    """The CalDAV adapter for an asynchronous client, or for a synchronous one."""
    return AsyncCalDAVAdapter if asynchronous else CalDAVAdapter


def calendar_path(server: Server, scenario: str) -> str:
    return f"{server.home}calendaring-sync-{scenario.replace('_', '-')}/"


@contextmanager
def environment(
    server: Server,
    scenario: str,
    tape: Tape,
    *,
    login: Login = REPLAY,
    asynchronous: bool = False,
    store: StateStore | None = None,
) -> Iterator[Env]:
    """The adapter and second client of ``scenario``, on an empty calendar, through ``tape``.

    The adapter keeps its state in ``store``, a new ``MemoryStore`` by default.
    """
    base_url, username, password = login
    url = f"{base_url.rstrip('/')}{calendar_path(server, scenario)}"
    other_client = DAVClient(url=base_url, username=username, password=password)
    other_client.session = tape.session(other_client.session)
    other = Calendar(client=other_client, url=url)
    # Start from an empty calendar; one left from an earlier recording goes.
    other_client.request(url, "DELETE")
    other_client.request(url, "MKCALENDAR")
    replica = Replica(MemoryStore() if store is None else store)
    client = caldav_client(asynchronous, url=base_url, username=username, password=password)
    client.session = tape.session(client.session, asynchronous=asynchronous)
    calendar = Calendar(client=client, url=url)
    adapter = adapter_class(asynchronous)(replica, calendar)
    with stepper(asynchronous) as run_step:
        try:
            with _fixed_hrefs():
                yield Env(server, scenario, tape, adapter, run_step, calendar, other)
        finally:
            run_step(client.close())
            other_client.close()
