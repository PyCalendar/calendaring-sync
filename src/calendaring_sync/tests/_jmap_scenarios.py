# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the JMAP adapter does against a server, written once for every way it runs.

Each scenario syncs an account's events with the adapter while a second
client changes the server behind its back, and asserts what the store
holds. ``record_jmap`` runs them against real servers and saves each
exchange, the unit tests replay the recordings, and the integration tests
run them against running servers. A scope is a whole account, so every
scenario starts by deleting the account's events.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

import niquests
import pytest
from calendaring_jmap import AsyncJMAPClient, JMAPClient, JMAPMethodError
from calendaring_jmap.session import _parse_session_data
from niquests.auth import HTTPBasicAuth

from calendaring_sync import MemoryStore, RecordState, Replica, StateStore
from calendaring_sync.adapters import jmap as adapter_module
from calendaring_sync.adapters.jmap import AsyncJMAPAdapter, JMAPAdapter
from calendaring_sync.error import ScopeGoneError
from calendaring_sync.objects.conflict import ConflictKind
from calendaring_sync.tests import _scenarios
from calendaring_sync.tests._content import event
from calendaring_sync.tests._scenarios import (
    REPLAY_LOGIN,
    REPLAY_URL,
    Login,
    by_name,
    edit_a,
    invalid_token,
    listing,
    pushing,
    reading,
    rejected_then_conflict,
    stepper,
)
from calendaring_sync.tests._tape import Tape

#: The protocol the recordings speak, which names their folder.
PROTOCOL = "jmap"
#: Where a JMAP server's session is, below its base URL.
SESSION_PATH = "/.well-known/jmap"
#: What a replay logs in to.
REPLAY: Login = (f"{REPLAY_URL}{SESSION_PATH}", REPLAY_LOGIN, REPLAY_LOGIN)
# An account no tested server has.
_NO_ACCOUNT = "no-such-account"
# How many events the second client deletes at a time when emptying an account.
_CLEAR_PAGE = 100


@dataclass(frozen=True)
class Server:
    """A tested server: where it runs, and what differs about it."""

    name: str
    #: Its Docker fixture's session URL and account.
    login: Login
    #: The address the server makes the organizer of an event created with
    #: participants and none (draft-ietf-jmap-calendars-33 section 5.9).
    organizer: str
    #: A state the server never issued, which it refuses.
    unknown_token: str = "never-issued"
    #: Whether it keeps two events with one uid and different recurrence ids.
    instances_share_uid: bool = True
    #: Whether it refuses a wrong password; Cyrus' fixture lets anyone in.
    checks_passwords: bool = False
    #: Whether it echoes a state past its newest and answers it with no
    #: changes, rather than refusing it or answering from another state.
    answers_unissued_state: bool = False
    #: Whether it refuses a create without ``version``, which it MUST set
    #: itself (draft-ietf-jmap-calendars-33 section 5.9).
    needs_version: bool = False
    #: Whether it keeps an event in two calendars.
    keeps_two_calendars: bool = False


SERVERS = by_name(
    Server(
        "cyrus",
        (f"http://localhost:8802{SESSION_PATH}", "user1", "x"),
        "mailto:user1@example.com",
        answers_unissued_state=True,
        needs_version=True,
    ),
    Server(
        "stalwart",
        (f"http://localhost:8809{SESSION_PATH}", "testuser@example.org", "testcaldav"),
        "mailto:testuser@example.org",
        instances_share_uid=False,
        checks_passwords=True,
        keeps_two_calendars=True,
    ),
)


def started(server: Server) -> nullcontext[Login]:
    """The login of ``server``'s Docker fixture, which has to be running."""
    return nullcontext(server.login)


#: Members of an event that tested servers rewrite when they store it.
REWRITTEN: dict[str, Any] = {
    "description": "multi\nline ✓",
    "showWithoutTime": False,
    "status": "confirmed",
    "priority": 5,
    "locations": {"l1": {"@type": "Location", "name": "Room"}},
    "relatedTo": {"other-uid": {"@type": "Relation", "relation": {"parent": True}}},
    "recurrenceRule": {
        "@type": "RecurrenceRule",
        "frequency": "weekly",
        "interval": 1,
        "count": 6,
        "byDay": [{"@type": "NDay", "day": "mo"}],
    },
    "recurrenceOverrides": {"2026-11-09T10:00:00": {"title": "moved"}},
    "alerts": {
        "a1": {
            "@type": "Alert",
            "trigger": {"@type": "OffsetTrigger", "offset": "-PT15M", "relativeTo": "start"},
            "action": "display",
        }
    },
    "useDefaultAlerts": False,
}
#: A guest, invited with the role JSCalendar 2.0 no longer lists.
GUEST: dict[str, Any] = {
    "@type": "Participant",
    "name": "Guest",
    "calendarAddress": "mailto:guest@example.org",
    "roles": {"attendee": True},
    "participationStatus": "needs-action",
    "expectReply": False,
}


@dataclass
class Env(_scenarios.Env):
    """One scenario's adapter, store, tape, and the account through a second client."""

    other: JMAPClient
    account: str
    calendar: str
    #: The ids of the events the second client put, by name.
    ids: dict[str, str] = field(default_factory=dict)
    versioned = False
    pushes_mine = ("POST", '"title": "mine')

    def uid(self, name: str) -> str:
        """The UID of the event ``name`` in this scenario, marked as a JMAP one.

        A server that also speaks CalDAV holds one set of UIDs for both, and
        the CalDAV scenarios use the same names.
        """
        return f"{PROTOCOL}-{super().uid(name)}"

    def content(self, name: str, summary: str, **members: Any) -> bytes:
        """An event whose UID is ``name`` within this scenario, with ``members`` added."""
        return json.dumps(event(self.uid(name), summary, self.calendar, **members)).encode()

    def put(self, uid: str, summary: str) -> None:
        """Create or change the event ``uid`` on the server from the second client."""
        data = event(self.uid(uid), summary, self.calendar)
        if uid in self.ids:
            self.other.update_event(self.ids[uid], data, account_id=self.account)
        else:
            self.ids[uid] = self.other.create_event(None, data, account_id=self.account)[0]

    def remove(self, uid: str) -> None:
        """Delete the event ``uid`` from the server from the second client."""
        self.other.delete_event(self.ids.pop(uid), account_id=self.account)

    def summary(self, uid: str) -> str:
        title: str = json.loads(self.record(uid).content or b"{}")["title"]
        return title


def paging(env: Env) -> None:
    """Changes and listings come a page at a time, and end where an unpaged read would."""
    with mock.patch.object(adapter_module, "PAGE_SIZE", 1):
        env.sync()
        for uid in ("a", "b", "c"):
            env.put(uid, "one")
        env.sync()
        assert {r.logical_key for r in env.replica.records(env.adapter.scope_id)} == {
            env.uid(uid) for uid in ("a", "b", "c")
        }
        env.remove("b")
        env.put("c", "two")
        env.sync(full_listing=True)
    assert env.record("b").state is RecordState.REMOVED
    assert env.summary("c") == "two"


def own_writes(env: Env) -> None:
    """A server's copy of an event written here fingerprints the same, so it isn't news."""
    env.sync()
    organizer = {"@type": "Participant", "calendarAddress": env.server.organizer}
    members = {
        **REWRITTEN,
        "organizerCalendarAddress": env.server.organizer,
        "participants": {"p0": {**organizer, "roles": {"owner": True}}, "p1": GUEST},
    }
    key = env.create(env.content("rich", "rich", **members))
    env.sync()
    head = env.replica.feed_head()
    env.sync(full_listing=True)
    assert env.replica.read_feed(head) == []
    assert env.at(key).state is RecordState.SYNCED


def organizer_added(env: Env) -> None:
    """An event created with a guest and no organizer gets one from the server, which is news."""
    env.sync()
    key = env.create(env.content("invite", "invite", participants={"p1": GUEST}))
    env.sync()
    head = env.replica.feed_head()
    env.sync()
    assert [entry.local_key for entry in env.replica.read_feed(head)] == [key]
    added = json.loads(env.at(key).content or b"{}")
    assert added["organizerCalendarAddress"] == env.server.organizer


def rejected(env: Env) -> None:
    """A push after the account changed is rejected, and the edits after it wait unsent.

    Whichever edit goes first is the one rejected, since the second client's
    change moves the state of the whole account.
    """
    env.put("b", "one")
    key = edit_a(env)
    later = env.record("b").local_key
    env.change(later, env.content("b", "mine too"))

    def change_a_behind() -> None:
        env.put("a", "theirs")
        env.never(*env.pushes_mine, "A push went out after a rejected one")

    env.tape.before(*env.pushes_mine, change_a_behind)
    env.sync()
    assert (env.at(key).state, env.at(later).state) == (RecordState.PENDING, RecordState.PENDING)
    env.tape.forget_hooks()
    env.sync()
    assert env.conflict_kind(key) is ConflictKind.BOTH_CHANGED
    assert env.at(later).state is RecordState.SYNCED


def delete_gone(env: Env) -> None:
    """:func:`_scenarios.delete_gone`, the push rejected first.

    Their delete moves the account's state, so the push is rejected, and the
    next sync ingests the removal.
    """
    _scenarios.delete_gone(env, ("POST", '"destroy"'), RecordState.PENDING)


def uid_taken(env: Env) -> None:
    """A create whose uid another event took meanwhile becomes a conflict.

    The other event moves the account's state, so the first push is
    rejected; the next sync ingests that event and sends the create again,
    which the server refuses for its uid.
    """
    env.sync()
    key = env.create(env.content("dup", "mine"))
    env.tape.before(*env.pushes_mine, lambda: env.put("dup", "theirs"))
    rejected_then_conflict(env, key, ConflictKind.LOGICAL_KEY_TAKEN)


def instances(env: Env) -> None:
    """Two instances of one uid are two events where the server keeps both."""
    env.sync()
    keys = [
        env.create(env.content("series", "one instance", recurrenceId=rid))
        for rid in ("2026-11-02T10:00:00", "2026-11-09T10:00:00")
    ]
    env.sync()
    second = RecordState.SYNCED if env.server.instances_share_uid else RecordState.CONFLICT
    assert [env.at(key).state for key in keys] == [RecordState.SYNCED, second]


def scope_gone(env: Env) -> None:
    """An account the server doesn't have raises, and the store keeps what it had."""
    env.synced("a")
    adapter = type(env.adapter)(
        env.replica, env.adapter._client, account_id=_NO_ACCOUNT, scope_id=env.adapter.scope_id
    )
    with pytest.raises(ScopeGoneError):
        env.run(adapter.sync())
    assert env.record("a").state is RecordState.SYNCED


def listing_moved(env: Env) -> None:
    """A listing during which an event is destroyed ends early and removes nothing.

    Events created meanwhile join the listing instead, read a page at a time.
    """
    with mock.patch.object(adapter_module, "PAGE_SIZE", 1):
        sync_while_d_and_e_arrive(env)
        assert {env.record(uid).state for uid in ("d", "e")} == {RecordState.SYNCED}
        env.tape.before("POST", '"position": 1', lambda: env.remove("a"))
        env.sync(full_listing=True)
        assert {r.state for r in env.replica.records(env.adapter.scope_id)} == {RecordState.SYNCED}
        env.sync()
    assert env.record("a").state is RecordState.REMOVED


def sync_while_d_and_e_arrive(env: Env) -> None:
    """Put ``a``, ``b`` and ``c``, then sync, with ``d`` and ``e`` put midway through listing."""
    for uid in ("a", "b", "c"):
        env.put(uid, "one")

    def put_d_and_e() -> None:
        env.put("d", "one")
        env.put("e", "one")

    env.tape.before("POST", '"position": 1', put_d_and_e)
    env.sync()


SCENARIOS: dict[str, Callable[[Env], None]] = by_name(
    reading,
    paging,
    pushing,
    own_writes,
    organizer_added,
    rejected,
    delete_gone,
    uid_taken,
    instances,
    invalid_token,
    scope_gone,
    listing,
    listing_moved,
    key="__name__",
)

#: Each recording: the server and the scenario.
RECORDINGS = [(server, scenario) for server in SERVERS for scenario in SCENARIOS]


def jmap_client(asynchronous: bool, **settings: Any) -> Any:
    """A calendaring-jmap client made with ``settings``, asynchronous or not."""
    return (AsyncJMAPClient if asynchronous else JMAPClient)(**settings)


def adapter_class(asynchronous: bool) -> type[JMAPAdapter] | type[AsyncJMAPAdapter]:
    """The JMAP adapter for an asynchronous client, or for a synchronous one."""
    return AsyncJMAPAdapter if asynchronous else JMAPAdapter


def attach(tape: Tape, client: Any, *, asynchronous: bool = False) -> None:
    """Route a calendaring-jmap client's requests through the tape, its session's too."""
    through_tape = tape.session(niquests.Session())
    login = HTTPBasicAuth(client.username or "", client.password or "")
    answer = through_tape.get(client.url, auth=login, headers={"Accept": "application/json"})
    answer.raise_for_status()
    client._session_cache = _parse_session_data(client.url, answer.json())
    client._http_session = tape.session(client._get_http_session(), asynchronous=asynchronous)


def _clear(client: JMAPClient, account: str) -> None:
    """Delete every event in the account; one a search names twice is gone the second time."""
    deleted: set[str] = set()
    while (
        ids := {
            found.data["id"]
            for found in client.search_events(account_id=account, limit=_CLEAR_PAGE).events
        }
        - deleted
    ):
        for event_id in sorted(ids):
            try:
                client.delete_event(event_id, account_id=account)
            except JMAPMethodError as exc:
                if exc.error_type != "notFound":
                    raise
            deleted.add(event_id)


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
    """The adapter and second client of ``scenario``, on an emptied account, through ``tape``.

    The adapter keeps its state in ``store``, a new ``MemoryStore`` by default.
    """
    url, username, password = login
    other = JMAPClient(url=url, username=username, password=password)
    attach(tape, other)
    account = other.get_account_id()
    calendar = other.get_calendars(account_id=account)[0].id
    _clear(other, account)
    client = jmap_client(asynchronous, url=url, username=username, password=password)
    attach(tape, client, asynchronous=asynchronous)
    replica = Replica(MemoryStore() if store is None else store)
    adapter = adapter_class(asynchronous)(replica, client, account_id=account)
    with stepper(asynchronous) as run_step:
        try:
            yield Env(server, scenario, tape, adapter, run_step, other, account, calendar)
        finally:
            run_step(client.aclose() if asynchronous else client.close())
            other.close()
