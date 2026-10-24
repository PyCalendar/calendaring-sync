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

import asyncio
import itertools
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import caldav.aio
import pytest
from caldav.calendarobjectresource import CalendarObjectResource
from caldav.collection import Calendar
from caldav.davclient import DAVClient
from caldav.testing import RadicaleServer, XandikosServer

from calendaring_sync import MemoryStore, Record, RecordState, Replica, StateStore
from calendaring_sync.adapters import caldav as adapter_module
from calendaring_sync.adapters.caldav import AsyncCalDAVAdapter, CalDAVAdapter
from calendaring_sync.error import IncompleteListingError, ScopeGoneError
from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.tests._caldav_tape import Tape

#: The host a replay talks to; recordings keep only paths.
REPLAY_URL = "http://recorded.invalid"
#: The login a replay sends where a recording asked for one; the tape never checks it.
REPLAY_LOGIN = "recorded"


#: A server's base URL, user name and password.
Login = tuple[str, str | None, str | None]
#: What a replay logs in to.
REPLAY: Login = (REPLAY_URL, REPLAY_LOGIN, REPLAY_LOGIN)


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


SERVERS = {
    server.name: server
    for server in (
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
}
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


def ics(uid: str, summary: str) -> bytes:
    """A small event in iCalendar form."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendaring-sync tests//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261001T000000Z\r\n"
        f"DTSTART:20261101T100000Z\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


@dataclass
class Env:
    """One scenario's adapter, store, second client and tape."""

    server: Server
    scenario: str
    tape: Tape
    calendar: Calendar
    other: Calendar
    adapter: CalDAVAdapter | AsyncCalDAVAdapter
    run: Callable[[Any], Any]

    @property
    def replica(self) -> Replica:
        return self.adapter._replica

    def sync(self, *, full_listing: bool = False) -> None:
        self.run(self.adapter.sync(full_listing=full_listing))

    def create(self, content: bytes) -> int:
        key: int = self.run(self.adapter.create(content))
        return key

    def change(self, local_key: int, content: bytes) -> None:
        self.run(self.adapter.change(local_key, content))

    def ics(self, name: str, summary: str) -> bytes:
        """An event whose UID is ``name`` within this scenario.

        UIDs carry the scenario's name, since Cyrus refuses a UID that
        another of the user's calendars holds.
        """
        return ics(f"{self.scenario}-{name}", summary)

    def _member(self, uid: str) -> CalendarObjectResource:
        """``<uid>.ics`` in the calendar, through the second client."""
        path = f"{calendar_path(self.server, self.scenario)}{uid}.ics"
        return CalendarObjectResource(client=self.other.client, url=path, parent=self.other)

    def put(self, uid: str, summary: str) -> None:
        """Store an object on the server from the second client, at ``<uid>.ics``."""
        self._member(uid).save(raw_data=self.ics(uid, summary))

    def remove(self, uid: str) -> None:
        """Delete ``<uid>.ics`` from the server from the second client."""
        self._member(uid).delete()

    def never(self, method: str, marker: str, what: str) -> None:
        """Fail with ``what`` if a ``method`` request's body ever holds ``marker``."""

        def refuse() -> None:
            raise AssertionError(what)

        self.tape.before(method, marker, refuse)

    def synced(self, uid: str, summary: str = "one") -> int:
        """Put ``<uid>.ics`` on the server from the second client, sync, and return its key."""
        self.put(uid, summary)
        self.sync()
        return self.record(uid).local_key

    def at(self, local_key: int) -> Record:
        """The record with this local key."""
        record = self.replica.record(local_key)
        assert record is not None
        return record

    def record(self, uid: str) -> Record:
        """The one record whose logical key is ``uid``."""
        (found,) = self.replica.find_by_logical_key(self.adapter.scope_id, f"{self.scenario}-{uid}")
        return found

    def forget_token(self) -> None:
        """Give the scope a token the server never issued, as an expired one looks."""
        store = self.replica._store
        with store.transaction():
            scope = store.get_scope(self.adapter.scope_id)
            assert scope is not None
            store.put_scope(replace(scope, token=self.server.unknown_token))


def reading(env: Env) -> None:
    """A first sync lists the calendar; later syncs ingest changes and removals."""
    env.sync()
    assert env.replica.records(env.adapter.scope_id) == []
    sync_a_and_b(env)
    env.put("a", "second a")
    env.remove("b")
    env.sync()
    assert b"second a" in (env.record("a").content or b"")
    assert env.record("b").state is RecordState.REMOVED


def put_a_and_b(env: Env) -> None:
    """Put ``a`` and ``b`` on the server from the second client."""
    env.put("a", "first a")
    env.put("b", "first b")


def sync_a_and_b(env: Env) -> None:
    """Put ``a`` and ``b`` on the server from the second client, sync, and check both came in."""
    put_a_and_b(env)
    env.sync()
    for uid in ("a", "b"):
        record = env.record(uid)
        assert record.state is RecordState.SYNCED and record.version is not None


def create_c(env: Env) -> int:
    """Sync the empty calendar, record a create of ``c`` here, and return its local key."""
    env.sync()
    return env.create(env.ics("c", "made here"))


def pushing(env: Env) -> None:
    """A create, a change and a delete each go out and are applied."""
    key = create_c(env)
    env.sync()
    assert env.at(key).state is RecordState.SYNCED
    assert env.at(key).version is None
    env.sync()
    assert env.at(key).version is not None
    env.change(key, env.ics("c", "changed here"))
    env.sync()
    env.sync()
    assert b"changed here" in (env.at(key).content or b"")
    env.replica.delete(key)
    env.sync()
    assert env.at(key).state is RecordState.REMOVED


def edit_a(env: Env) -> int:
    """Sync ``a`` from the server, change it here, and return its local key."""
    key = env.synced("a")
    env.change(key, env.ics("a", "mine"))
    return key


def edit_a_changed_behind(env: Env) -> int:
    """:func:`edit_a`, with the second client changing ``a`` right before the push."""
    key = edit_a(env)
    env.tape.before("PUT", "SUMMARY:mine", lambda: env.put("a", "theirs"))
    return key


def rejected(env: Env) -> None:
    """A change whose object changed since the ingest is rejected, then becomes a conflict."""
    key = edit_a_changed_behind(env)
    env.sync()
    assert env.at(key).state is RecordState.PENDING
    env.sync()
    conflict = env.at(key).conflict
    assert conflict is not None and conflict.kind is ConflictKind.BOTH_CHANGED


def delete_gone(env: Env) -> None:
    """A delete of an object someone else deleted ends with the object removed."""
    key = env.synced("a")
    env.replica.delete(key)
    env.tape.before("DELETE", "", lambda: env.remove("a"))
    env.sync()
    expected = RecordState.REMOVED if env.server.delete_gone_is_404 else RecordState.PENDING
    assert env.at(key).state is expected
    env.sync()
    assert env.at(key).state is RecordState.REMOVED


def uid_taken(env: Env) -> None:
    """A create whose UID another object took meanwhile becomes a conflict."""
    env.sync()
    key = env.create(env.ics("dup", "mine"))
    env.tape.before("REPORT", "calendar-query", lambda: env.put("dup", "theirs"))
    env.sync()
    conflict = env.at(key).conflict
    assert conflict is not None and conflict.kind is ConflictKind.LOGICAL_KEY_TAKEN


def invalid_token(env: Env) -> None:
    """A refused token resets the scope and lists it; a pending edit survives and goes out."""
    key = edit_a(env)
    env.forget_token()
    env.sync()
    assert env.replica.scope(env.adapter.scope_id).token not in (None, env.server.unknown_token)
    assert env.at(key).state is RecordState.SYNCED


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
    env.put("a", "one")
    env.put("b", "one")
    env.sync()
    env.put("b", "two")
    env.sync(full_listing=True)
    assert b"two" in (env.record("b").content or b"")
    assert env.record("a").state is RecordState.SYNCED


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


SCENARIOS: dict[str, Callable[[Env], None]] = {
    scenario.__name__: scenario
    for scenario in (
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
    )
}

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


def run(
    server: Server,
    scenario: str,
    tape: Tape,
    *,
    login: Login = REPLAY,
    asynchronous: bool = False,
    store: StateStore | None = None,
) -> None:
    """Run one scenario through ``tape``, recording from the server at ``login`` or replaying."""
    with environment(
        server, scenario, tape, login=login, asynchronous=asynchronous, store=store
    ) as env:
        SCENARIOS[scenario](env)


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
    tape.attach(other_client)
    other = Calendar(client=other_client, url=url)
    # Start from an empty calendar; one left from an earlier recording goes.
    other_client.request(url, "DELETE")
    other_client.request(url, "MKCALENDAR")
    replica = Replica(MemoryStore() if store is None else store)
    client = caldav_client(asynchronous, url=base_url, username=username, password=password)
    tape.attach(client, asynchronous=asynchronous)
    calendar = Calendar(client=client, url=url)
    adapter = adapter_class(asynchronous)(replica, calendar)
    loop = asyncio.new_event_loop()
    # A step of the asynchronous adapter is a coroutine to run.
    run_step: Callable[[Any], Any] = loop.run_until_complete if asynchronous else lambda step: step
    try:
        with _fixed_hrefs():
            yield Env(server, scenario, tape, calendar, other, adapter, run_step)
    finally:
        run_step(client.close())
        other_client.close()
        loop.close()
