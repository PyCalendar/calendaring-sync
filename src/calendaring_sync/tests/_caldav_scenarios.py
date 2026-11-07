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

from calendaring_sync import (
    FeedEntryKind,
    KeepServer,
    MemoryStore,
    RecordState,
    Replica,
    StateStore,
)
from calendaring_sync.adapters.caldav import AsyncCalDAVAdapter, CalDAVAdapter
from calendaring_sync.error import IncompleteListingError, ScopeGoneError
from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.tests import _scenarios
from calendaring_sync.tests._content import ICS_START, ics
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

#: The login scheme of every server that checks logins. Naming it spares
#: python-caldav's first request without one, whose 401 Stalwart sometimes
#: sends and then closes the connection without ``Connection: close``
#: (RFC 9112 section 9.6), so the retry on it fails.
AUTH_TYPE = "basic"


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
    #: Where the server listens when ``tests/docker/start.sh`` starts it, and
    #: the account its fixture creates; ``None`` for a server that runs
    #: embedded.
    login: Login | None = None
    #: Whether the server refuses a wrong password; embedded ones and
    #: Cyrus' fixture let anyone in.
    checks_passwords: bool = False
    #: The account's own calendar address, which a server schedules from.
    address: str = "user1@example.com"
    #: Whether the server refuses a task in a calendar made for events only;
    #: one that stores it refuses an event without a start instead.
    refuses_tasks: bool = True


SERVERS = by_name(
    Server("radicale", "/user1/", "http://example.invalid/never-issued", refuses_tasks=False),
    Server(
        "xandikos",
        "/sometestuser/calendars/",
        "http://example.invalid/never-issued",
        address="sometestuser@example.com",
    ),
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
        address="testuser@example.org",
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
        address="test@example.org",
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


# The method and body marker of a delete's push, which has no body.
_DELETE = ("DELETE", "")


def edit_a_changed_behind(env: Env) -> int:
    """:func:`edit_a`, with the second client changing ``a`` right before the push."""
    key = edit_a(env)
    env.tape.before(*env.pushes_mine, lambda: env.put("a", "theirs"))
    return key


def rejected(env: Env) -> None:
    """A change whose object changed since the ingest is rejected, then becomes a conflict."""
    rejected_then_conflict(env, edit_a_changed_behind(env), ConflictKind.BOTH_CHANGED)


def delete_a_removed_behind(env: Env) -> int:
    """:func:`_scenarios.delete_a_removed_behind`, their delete right before the push."""
    return _scenarios.delete_a_removed_behind(env, _DELETE)


def delete_gone(env: Env) -> None:
    """:func:`_scenarios.delete_gone`; a server that answers 404 has the record removed at once."""
    first = RecordState.REMOVED if env.server.delete_gone_is_404 else RecordState.PENDING
    _scenarios.delete_gone(env, _DELETE, first)


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


# The summary of content a server won't store, which marks its push.
_NOT_STORED = "not stored"


def _not_stored(env: Env, name: str) -> bytes:
    """Content the server won't store in a calendar for events.

    That's a task, or an event without a start where the server stores tasks.
    """
    content = env.content(name, _NOT_STORED)
    if env.server.refuses_tasks:
        return content.replace(b"VEVENT", b"VTODO")
    return content.replace(ICS_START.encode(), b"")


def not_allowed(env: Env) -> None:
    """A change and a create the server won't store become conflicts, and aren't sent again."""
    changed = env.synced("a")
    env.change(changed, _not_stored(env, "a"))
    created = env.create(_not_stored(env, "c"))
    env.sync()
    for key in (changed, created):
        conflict = env.at(key).conflict
        assert conflict is not None and conflict.kind is ConflictKind.NOT_ALLOWED
        assert conflict.reason
    env.never("PUT", f"SUMMARY:{_NOT_STORED}", "An edit the server didn't allow went out again")
    env.sync()
    assert env.conflict_kind(changed) is env.conflict_kind(created) is ConflictKind.NOT_ALLOWED
    env.replica.resolve(env.at(changed), KeepServer())
    env.replica.resolve(env.at(created), KeepServer())
    assert env.summary("a") == "one"
    assert env.replica.record(created) is None


def own_write_with_attendees(env: Env) -> None:
    """An event you organize comes back as your own write.

    That holds whatever the server adds to it to schedule it.
    """
    env.sync()
    address = env.server.address
    people = (
        f"ORGANIZER:mailto:{address}",
        f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{address}",
        "ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:someone@example.net",
    )
    key = env.create(ics(env.uid("m"), "meeting", *people))
    head = env.replica.feed_head()
    # Each push, then the sync that reads the server's copy of it.
    env.sync()
    env.sync()
    env.change(key, ics(env.uid("m"), "meeting moved", *people))
    env.sync()
    env.sync()
    kinds = [entry.kind for entry in env.replica.read_feed(head) if entry.local_key == key]
    assert kinds == [FeedEntryKind.PUSHED, FeedEntryKind.PUSHED]
    assert env.at(key).state is RecordState.SYNCED


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
    not_allowed,
    own_write_with_attendees,
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
    ("radicale", "not_allowed"),
    ("xandikos", "reading"),
    ("xandikos", "invalid_token"),
    ("xandikos", "fetch_after_delete"),
    ("cyrus", "reading"),
    ("cyrus", "invalid_token"),
    ("cyrus", "not_allowed"),
    ("cyrus", "own_write_with_attendees"),
    ("stalwart", "reading"),
    ("stalwart", "listing_cut_short"),
    ("baikal", "delete_gone"),
    ("baikal", "fetch_after_delete"),
]


@contextmanager
def _fixed_hrefs() -> Iterator[None]:
    """Give creates the same hrefs in a recording and its replays."""
    numbers = itertools.count(1)
    # The adapter picks a create's href with uuid.uuid4, patched for the block.
    with mock.patch.object(uuid, "uuid4", lambda: uuid.UUID(int=next(numbers))):
        yield


def caldav_client(asynchronous: bool, **settings: Any) -> Any:
    """A python-caldav client made with ``settings``, asynchronous or not."""
    return (caldav.aio.AsyncDAVClient if asynchronous else DAVClient)(**settings)


def adapter_class(asynchronous: bool) -> type[CalDAVAdapter] | type[AsyncCalDAVAdapter]:
    """The CalDAV adapter for an asynchronous client, or for a synchronous one."""
    return AsyncCalDAVAdapter if asynchronous else CalDAVAdapter


# What every scenario's calendar is made for: events only (RFC 4791 section 5.3.1).
_EVENTS_ONLY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:set><D:prop>'
    "<C:supported-calendar-component-set>"
    '<C:comp name="VEVENT"/>'
    "</C:supported-calendar-component-set>"
    "</D:prop></D:set></C:mkcalendar>"
)


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
    # A replay answers with what was recorded, the 401 that asked for a login
    # included, and a login without a password has no scheme to name.
    auth = {} if login == REPLAY or password is None else {"auth_type": AUTH_TYPE}
    other_client = caldav_client(False, url=base_url, username=username, password=password, **auth)
    other_client.session = tape.session(other_client.session)
    other = Calendar(client=other_client, url=url)
    # Start from an empty calendar; one left from an earlier recording goes.
    other_client.request(url, "DELETE")
    other_client.request(url, "MKCALENDAR", _EVENTS_ONLY, {"Content-Type": "application/xml"})
    replica = Replica(MemoryStore() if store is None else store)
    client = caldav_client(asynchronous, url=base_url, username=username, password=password, **auth)
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
