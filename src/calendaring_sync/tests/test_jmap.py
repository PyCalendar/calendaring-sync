# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the JMAP adapter, on exchanges recorded from real servers.

``test_adapters`` replays every recording through both adapters, and
``record_jmap`` records them again. A rule no tested server shows on its own
is tested here on one exchange of a recording, edited in the test.
"""

from __future__ import annotations

import copy
import itertools
import json
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from typing import Any, cast
from unittest import mock

import pytest
from calendaring_jmap import JMAPMethodError

from calendaring_sync import (
    Changed,
    ChangeSet,
    Conflict,
    ConflictKind,
    EditKind,
    Gone,
    LocalEdit,
    LogicalKeyTaken,
    OtherScopeError,
    PushOutcome,
    Record,
    RecordState,
    Rejected,
    Replica,
    StateStore,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.adapters import jmap as adapter_module
from calendaring_sync.adapters.base import Steps
from calendaring_sync.adapters.base import _run as _drive
from calendaring_sync.adapters.jmap import (
    FINGERPRINT_SCHEME,
    MEDIA_TYPE,
    _event_fields,
    _refusal,
)
from calendaring_sync.tests import _jmap_scenarios as scenarios
from calendaring_sync.tests._content import event
from calendaring_sync.tests._helpers import (
    _SCOPE,
    _START,
    _assert_adapter_refuses,
    _insert,
    _local_record,
)
from calendaring_sync.tests._jmap_scenarios import (
    GUEST,
    PROTOCOL,
    REWRITTEN,
    SERVERS,
    Env,
    adapter_class,
    jmap_client,
    sync_while_d_and_e_arrive,
)
from calendaring_sync.tests._scenarios import (
    REPLAY_LOGIN,
    REPLAY_URL,
    create_c,
    done,
    edit_answer,
    fields_of,
    forgotten_edit,
    listing,
    recorded,
    replaying,
    run,
)
from calendaring_sync.tests._tape import Exchange, Tape, _Reply

_recording = partial(recorded, PROTOCOL)


_replaying = partial(replaying, scenarios)


_run = partial(run, scenarios)


# The calendar every test event is in.
_CALENDAR = "cal"


# A session URL, as a client takes it.
_SESSION = f"{REPLAY_URL}/.well-known/jmap"


# The member of a /set call each kind of edit sends, and of its answer when refused.
_SENT = {"create": "create", "change": "update", "delete": "destroy"}


_REFUSED = {"create": "notCreated", "change": "notUpdated", "delete": "notDestroyed"}


def _index(tape: Tape, method: str, *members: str, nth: int = 0, before: int | None = None) -> int:
    """Where the ``nth`` request calling ``method`` is, with each of ``members`` set.

    ``before`` counts only the requests ahead of that exchange.
    """
    found = [
        i
        for i, exchange in enumerate(tape.exchanges[:before])
        if exchange.request
        for name, args, _ in json.loads(exchange.request)["methodCalls"]
        if name == method and all(args.get(member) for member in members)
    ]
    return found[nth]


def _answer(method: str, **arguments: Any) -> str:
    """A server's answer to one method call."""
    return json.dumps({"methodResponses": [[method, arguments, "0"]]})


def _error(error_type: str) -> str:
    """An answer refusing a whole method call (RFC 8620 section 3.6.2)."""
    return _answer("error", type=error_type)


def _refused(kind: str, object_id: str, error_type: str, properties: list[str] | None) -> str:
    """A ``CalendarEvent/set`` answer refusing one create, update or destroy."""
    error: dict[str, Any] = {"type": error_type}
    if properties is not None:
        error["properties"] = properties
    return _answer(
        "CalendarEvent/set", oldState="s", newState="s", **{_REFUSED[kind]: {object_id: error}}
    )


def _content(**members: Any) -> bytes:
    return json.dumps({**event("event-1", "Planning", _CALENDAR), **members}).encode()


def _rich() -> dict[str, Any]:
    """An event with every member the fingerprint treats specially."""
    return event("u", "t", _CALENDAR, **REWRITTEN, participants={"p1": GUEST})


def _bare() -> dict[str, Any]:
    """An event holding one of each nested object, with no member set to its default."""
    bare = event("u", "t", _CALENDAR)
    del bare["@type"], bare["version"], bare["title"], bare["duration"]
    bare.update(
        recurrenceRule={"frequency": "weekly", "byDay": [{"day": "mo"}]},
        alerts={"a": {"trigger": {"offset": "-PT15M"}}},
        links={"k": {"href": "https://example.org/agenda"}},
        relatedTo={"r": {}},
        locations={"l": {"name": "Room"}},
        virtualLocations={"v": {"uri": "https://example.org/meet"}},
        participants={"p": {"calendarAddress": "mailto:p@example.org"}},
    )
    return bare


# Every default the fingerprint leaves out, written from the specs rather than
# read from the adapter: where it sits, the member and its default. Event members
# from draft-ietf-calext-jscalendarbis-22 and draft-ietf-jmap-calendars-33
# section 5; an implied @type from jscalendarbis-22 section 1.4.4; the version
# a server MUST set from jmap-calendars-33 section 5.9.
_DEFAULTS: list[tuple[tuple[str | int, ...], str, object]] = [
    ((), "@type", "Event"),
    ((), "version", "2.0"),
    ((), "title", ""),
    ((), "description", ""),
    ((), "descriptionContentType", "text/plain"),
    ((), "showWithoutTime", False),
    ((), "duration", "PT0S"),
    ((), "status", "confirmed"),
    ((), "priority", 0),
    ((), "freeBusyStatus", "busy"),
    ((), "privacy", "public"),
    ((), "recurrenceOverrides", {}),
    ((), "isDraft", False),
    ((), "useDefaultAlerts", False),
    ((), "mayInviteSelf", False),
    ((), "mayInviteOthers", False),
    ((), "hideAttendees", False),
    (("recurrenceRule",), "@type", "RecurrenceRule"),
    (("recurrenceRule",), "interval", 1),
    (("recurrenceRule",), "rscale", "gregorian"),
    (("recurrenceRule",), "skip", "omit"),
    (("recurrenceRule",), "firstDayOfWeek", "mo"),
    (("recurrenceRule", "byDay", 0), "@type", "NDay"),
    (("alerts", "a"), "@type", "Alert"),
    (("alerts", "a"), "action", "display"),
    (("alerts", "a", "trigger"), "@type", "OffsetTrigger"),
    (("alerts", "a", "trigger"), "relativeTo", "start"),
    (("links", "k"), "@type", "Link"),
    (("links", "k"), "rel", "enclosure"),
    (("relatedTo", "r"), "@type", "Relation"),
    (("relatedTo", "r"), "relation", {}),
    (("locations", "l"), "@type", "Location"),
    (("virtualLocations", "v"), "@type", "VirtualLocation"),
    (("virtualLocations", "v"), "name", ""),
    (("participants", "p"), "@type", "Participant"),
    (("participants", "p"), "participationStatus", "needs-action"),
    (("participants", "p"), "expectReply", False),
]


def _client(asynchronous: bool, url: str = _SESSION) -> Any:
    """A client that never connects, logged in as a replay is."""
    return jmap_client(asynchronous, url=url, username=REPLAY_LOGIN, password=REPLAY_LOGIN)


def _adapter(store: StateStore, asynchronous: bool) -> Any:
    """An adapter on the test scope, which exists."""
    replica = Replica(store)
    client = _client(asynchronous)
    adapter = adapter_class(asynchronous)(replica, client, account_id="a", scope_id=_SCOPE)
    with replica.session(_SCOPE, fingerprint_scheme=FINGERPRINT_SCHEME, media_type=MEDIA_TYPE):
        pass
    return adapter


def _make_edit(env: Env, kind: str) -> int:
    """Make the edit of ``kind`` that the recording ``cyrus-pushing`` sends, and return its key.

    The steps are the scenario's own, up to that edit.
    """
    key = create_c(env)
    if kind == "create":
        return key
    env.sync()
    env.sync()
    env.change(key, env.content("c", "changed here"))
    if kind == "delete":
        env.sync()
        env.sync()
        env.replica.delete(key)
    return key


def _refused_push(
    store: StateStore,
    asynchronous: bool,
    kind: str,
    refusal: str | tuple[str, list[str] | None],
    raised: type[Exception] | None,
) -> Record:
    """Replay ``cyrus-pushing`` with its push of ``kind`` refused, and return the edit's record.

    ``refusal`` is a method error's type, or a set error's type and properties.
    """
    tape = _recording("cyrus-pushing")
    # The adapter's own writes are the ones carrying ifInState.
    index = _index(tape, "CalendarEvent/set", _SENT[kind], "ifInState")
    del tape.exchanges[index + 1 :]
    with _replaying(tape, store, asynchronous) as env:
        key = _make_edit(env, kind)
        refused_id = env.at(key).object_id or "new-0"
        body = _error(refusal) if isinstance(refusal, str) else _refused(kind, refused_id, *refusal)
        edit_answer(tape, index, body=body)
        with pytest.raises(raised) if raised else nullcontext():
            env.sync()
    return env.at(key)


def _uid_held(
    store: StateStore, record: Record, *, uid: object = "u", itself: bool = False
) -> bool:
    """Whether a create of an event with ``uid`` finds ``record``, written to the store, holding it.

    With ``itself``, the create is ``record``'s own edit.
    """
    adapter = _adapter(store, False)
    key = _insert(store, record)
    content = json.dumps({**event("u", "Planning", _CALENDAR), "uid": uid}).encode()
    edit = LocalEdit(EditKind.CREATE, content, "f", frozenset({_CALENDAR}), None, _START)
    return bool(_drive(adapter._uid_held(edit, key if itself else key + 1)))


# What each refusal of a push means, by edit kind: Rejected, Gone,
# LogicalKeyTaken, or None for an error that isn't one and is raised.
_REFUSALS: dict[tuple[str, tuple[str, ...] | None], dict[str, type[PushOutcome] | None]] = {
    ("stateMismatch", None): {"create": Rejected, "change": Rejected, "delete": Rejected},
    ("notFound", None): {"create": None, "change": Gone, "delete": Gone},
    ("invalidProperties", ("uid",)): {
        "create": LogicalKeyTaken,
        "change": LogicalKeyTaken,
        "delete": None,
    },
    ("invalidProperties", ("title",)): {"create": None, "change": None, "delete": None},
    ("invalidPatch", ("uid",)): {"create": None, "change": None, "delete": None},
    ("invalidProperties", None): {"create": None, "change": None, "delete": None},
    ("serverFail", None): {"create": None, "change": None, "delete": None},
}


class TestAdapter:
    """Making an adapter, and the calls it takes."""

    @pytest.mark.parametrize(
        "url, spelled",
        [
            (_SESSION, _SESSION),
            ("HTTP://Recorded.Invalid/.well-known/jmap", _SESSION),
            ("http://someone:secret@recorded.invalid/.well-known/jmap", _SESSION),
            ("http://[::1]:8809/.well-known/jmap", "http://[::1]:8809/.well-known/jmap"),
        ],
        ids=["as written", "capitals", "login", "IPv6"],
    )
    def test_scope_id_defaults_to_the_session_url_and_account(
        self, store, asynchronous, url, spelled
    ):
        client = _client(asynchronous, url)
        adapter = adapter_class(asynchronous)(Replica(store), client, account_id="a")
        assert adapter.scope_id == f"{spelled}#a/CalendarEvent"
        assert _adapter(store, asynchronous).scope_id == _SCOPE

    def test_an_edit_gets_the_fields_a_sync_gives(self, store, asynchronous):
        adapter = _adapter(store, asynchronous)
        key = done(adapter.create(_content()))
        record = adapter._replica.record(key)
        assert record is not None and record.edit is not None
        edit, fields = record.edit, _event_fields(json.loads(_content()))
        assert (edit.fingerprint, edit.logical_key, edit.containers) == (
            fields.fingerprint,
            "event-1",
            frozenset({_CALENDAR}),
        )
        assert fields_of(adapter.edit_fields(_content())) == fields_of(edit)
        # The server picks a created event's id, and no JMAP edit waits.
        assert record.object_id is None
        assert done(adapter.waiting()) == []

    def test_no_tested_server_needs_full_listings(self, store, asynchronous):
        # Each refuses a state it can't calculate changes from, so a sync resets itself.
        assert _adapter(store, asynchronous).needs_full_listings is False

    def test_only_a_create_needs_a_uid_and_a_calendar(self, store, asynchronous):
        adapter = _adapter(store, asynchronous)
        synced = Changed(None, "f", _content(), frozenset({_CALENDAR}), "event-1")
        with adapter._replica.session(
            _SCOPE, fingerprint_scheme=FINGERPRINT_SCHEME, media_type=MEDIA_TYPE
        ) as session:
            session.ingest(ChangeSet(_SCOPE, None, "s1", {"e1": synced}, full_listing=True))
        key = adapter._replica.find_record(_SCOPE, "e1").local_key
        done(adapter.change(key, json.dumps({"title": "renamed"}).encode()))
        edit = adapter._replica.record(key).edit
        assert (edit.kind, edit.logical_key, edit.containers) == (
            EditKind.CHANGE,
            None,
            frozenset(),
        )

    def test_a_client_of_the_other_kind_is_refused(self, store):
        replica = Replica(store)
        with pytest.raises(TypeError, match="client"):
            adapter_class(False)(replica, _client(asynchronous=True), account_id="a")
        with pytest.raises(TypeError, match="client"):
            adapter_class(True)(replica, _client(asynchronous=False), account_id="a")

    # Each row calls an adapter of ``scope_id``: local key 1 is the record
    # the test makes in the test scope, 2 doesn't exist.
    @pytest.mark.parametrize(
        "call, scope_id, error, match, before",
        [
            pytest.param(
                lambda a: a.create(b"\xff"), _SCOPE, ValueError, "UTF-8 JSON", True, id="not UTF-8"
            ),
            pytest.param(
                lambda a: a.create(b"{"), _SCOPE, ValueError, "UTF-8 JSON", True, id="not JSON"
            ),
            pytest.param(
                lambda a: a.create(b"[]"), _SCOPE, ValueError, "one object", True, id="no object"
            ),
            pytest.param(
                lambda a: a.create(_content(uid="")), _SCOPE, ValueError, "uid", True, id="no uid"
            ),
            pytest.param(
                lambda a: a.create(_content(calendarIds={})),
                _SCOPE,
                ValueError,
                "in a calendar",
                True,
                id="no calendar",
            ),
            pytest.param(
                lambda a: a.edit_fields(_content(calendarIds={})),
                _SCOPE,
                ValueError,
                "in a calendar",
                True,
                id="fields, no calendar",
            ),
            pytest.param(
                lambda a: a.change(1, _content(uid="")),
                _SCOPE,
                ValueError,
                "uid",
                False,
                id="change of an unsent create, no uid",
            ),
            pytest.param(
                lambda a: a.change(1, _content(calendarIds={})),
                _SCOPE,
                ValueError,
                "in a calendar",
                False,
                id="change of an unsent create, no calendar",
            ),
            pytest.param(
                lambda a: a.create(_content()), "nope", UnknownScopeError, None, False, id="scope"
            ),
            pytest.param(
                lambda a: a.change(2, _content()),
                _SCOPE,
                UnknownRecordError,
                None,
                False,
                id="record",
            ),
            pytest.param(
                lambda a: a.change(1, _content()),
                "nope",
                OtherScopeError,
                f"^Record 1 is in scope '{_SCOPE}', not this one$",
                False,
                id="other scope",
            ),
            pytest.param(
                lambda a: a.waiting(), "nope", UnknownScopeError, None, False, id="waiting, scope"
            ),
        ],
    )
    def test_a_call_is_refused(self, store, asynchronous, call, scope_id, error, match, before):
        made = _adapter(store, asynchronous)
        assert done(made.create(_content())) == 1
        adapter = adapter_class(asynchronous)(
            made._replica, made._client, account_id="a", scope_id=scope_id
        )
        _assert_adapter_refuses(adapter, store, call, error, match, before)


class TestFingerprint:
    """The fingerprint, logical key and containers read from an event."""

    @pytest.mark.parametrize(
        "rewrite",
        [
            pytest.param(
                lambda e: e.update(
                    {"id": "x", "created": "t", "updated": "t", "sequence": 2, "prodId": "p"}
                    | {"x-href": "/dav/x.ics", "utcStart": "t", "utcEnd": "t", "isOrigin": True}
                    | {"baseEventId": "b"}
                ),
                id="members the server sets",
            ),
            pytest.param(
                lambda e: [
                    e["recurrenceOverrides"]["2026-11-09T10:00:00"].update(updated="t"),
                    e["locations"]["l1"].update(iCalendar={"name": "vlocation"}),
                ],
                id="what Stalwart adds",
            ),
            pytest.param(
                lambda e: [
                    e.update(iCalendar={"x": 1}),
                    e["recurrenceOverrides"]["2026-11-09T10:00:00"].update(iCalendar={"x": 1}),
                ],
                id="conversion metadata on the event and in an override",
            ),
            pytest.param(
                lambda e: e["participants"]["p1"].pop("roles"),
                id="the attendee role dropped",
            ),
        ],
    )
    def test_what_a_server_rewrites_keeps_the_fingerprint(self, rewrite):
        sent = _rich()
        stored = copy.deepcopy(sent)
        rewrite(stored)
        assert _event_fields(stored).fingerprint == _event_fields(sent).fingerprint

    @pytest.mark.parametrize(
        "where, member, default",
        _DEFAULTS,
        ids=[f"{'.'.join(map(str, w))}.{m}" for w, m, _ in _DEFAULTS],
    )
    def test_a_member_holding_its_default_is_left_out(self, where, member, default):
        bare = _bare()
        filled = copy.deepcopy(bare)
        target = filled
        for step in where:
            target = target[step]
        target[member] = default
        assert _event_fields(filled).fingerprint == _event_fields(bare).fingerprint

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda e: e.update(title="other"), id="title"),
            pytest.param(
                lambda e: e["recurrenceOverrides"]["2026-11-09T10:00:00"].update(
                    showWithoutTime=False
                ),
                id="a default set in an override",
            ),
            pytest.param(
                lambda e: e["alerts"]["a1"].update(
                    trigger={"@type": "AbsoluteTrigger", "when": "2026-11-01T09:00:00Z"}
                ),
                id="a trigger of another type",
            ),
            pytest.param(
                lambda e: e["participants"]["p1"]["roles"].update(owner=True), id="a role"
            ),
            pytest.param(lambda e: e.update(version="1.0"), id="another version"),
        ],
    )
    def test_an_edit_changes_the_fingerprint(self, edit):
        sent = _rich()
        changed = copy.deepcopy(sent)
        edit(changed)
        assert _event_fields(changed).fingerprint != _event_fields(sent).fingerprint

    @pytest.mark.parametrize(
        "members, logical_key",
        [
            ({}, "u"),
            ({"recurrenceId": "2026-11-09T10:00:00"}, "u 2026-11-09T10:00:00"),
            ({"uid": ""}, None),
            ({"uid": 7}, None),
        ],
    )
    def test_the_logical_key_is_the_uid_and_recurrence_id(self, members, logical_key):
        assert _event_fields({**event("u", "t", _CALENDAR), **members}).logical_key == logical_key

    @pytest.mark.parametrize(
        "calendars, containers",
        [({"a": True, "b": False, "c": True}, {"a", "c"}), ("not a map", set())],
    )
    def test_containers_are_the_events_calendars(self, calendars, containers):
        fields = _event_fields({**event("u", "t", _CALENDAR), "calendarIds": calendars})
        assert fields.containers == containers

    def test_values_of_unexpected_shapes_still_fingerprint(self):
        odd = {
            **event("u", "t", _CALENDAR),
            "locations": {"l1": "not an object"},
            "recurrenceRule": "not an object",
            "recurrenceOverrides": {"2026-11-09T10:00:00": "not a patch"},
            "participants": {"p1": {"roles": "not a map"}},
        }
        assert len(_event_fields(odd).fingerprint) == 64


class TestReading:
    """Reading changes."""

    def test_content_is_the_event_as_utf8_json(self, store, asynchronous):
        with _replaying(_recording("cyrus-own_writes"), store, asynchronous) as env:
            scenarios.own_writes(env)
        content = env.record("rich").content or b""
        assert "✓".encode() in content and b"\\u2713" not in content
        assert json.loads(content)["description"] == REWRITTEN["description"]

    def test_an_event_the_get_leaves_out_waits_for_the_next_changes(self, store, asynchronous):
        # The second sync's /get, after a and b were created, loses b.
        tape = _recording("cyrus-reading")
        index = _index(tape, "CalendarEvent/get", "ids")
        answer = json.loads(tape.exchanges[index].body)
        found = answer["methodResponses"][0][1]
        found["list"] = [e for e in found["list"] if e["uid"].endswith("-a")]
        edit_answer(tape, index, body=json.dumps(answer))
        del tape.exchanges[index + 1 :]
        with _replaying(tape, store, asynchronous) as env:
            env.sync()
            env.put("a", "first a")
            env.put("b", "first b")
            env.sync()
        assert [r.logical_key for r in env.replica.records(env.adapter.scope_id)] == [env.uid("a")]

    @pytest.mark.parametrize(
        "answer, reset",
        [
            pytest.param(_error("cannotCalculateChanges"), True, id="cannot calculate"),
            pytest.param(_error("invalidArguments"), True, id="invalid arguments"),
            pytest.param(
                _answer(
                    "CalendarEvent/changes",
                    oldState="another",
                    newState="n",
                    hasMoreChanges=False,
                    created=[],
                    updated=[],
                    destroyed=[],
                ),
                True,
                id="another state answered",
            ),
            pytest.param(_error("serverFail"), False, id="server failure"),
        ],
    )
    def test_a_refused_state_resets_the_scope(self, store, asynchronous, answer, reset):
        # The recording's only /changes is Cyrus' own refusal; each row answers it another way.
        tape = _recording("cyrus-invalid_token")
        index = _index(tape, "CalendarEvent/changes")
        edit_answer(tape, index, body=answer)
        if not reset:
            del tape.exchanges[index + 1 :]
        with _replaying(tape, store, asynchronous) as env:
            key = forgotten_edit(env)
            with nullcontext() if reset else pytest.raises(JMAPMethodError, match="serverFail"):
                env.sync()
        token = env.replica.scope(env.adapter.scope_id).token
        assert (token != env.server.unknown_token) is reset
        assert env.at(key).state is (RecordState.SYNCED if reset else RecordState.PENDING)


class TestListing:
    """Full listings."""

    def test_a_listing_without_a_total_ends_at_an_empty_page(self, store, asynchronous):
        # The first listing's page loses its total, so an empty page follows it.
        tape = _recording("cyrus-listing")
        index = _index(tape, "CalendarEvent/query", nth=-2)
        answer = json.loads(tape.exchanges[index].body)
        del answer["methodResponses"][0][1]["total"]
        edit_answer(tape, index, body=json.dumps(answer))
        empty = copy.deepcopy(answer)
        empty["methodResponses"][0][1]["ids"] = []
        empty["methodResponses"][1][1]["list"] = []
        request = json.loads(tape.exchanges[index].request)
        request["methodCalls"][0][1]["position"] = 2
        page = replace(
            tape.exchanges[index],
            body=json.dumps(empty),
            request=json.dumps(request, sort_keys=True),
        )
        tape.exchanges.insert(index + 1, page)
        with _replaying(tape, store, asynchronous) as env:
            listing(env)

    def test_events_created_during_a_listing_join_its_last_page(self, store, asynchronous):
        # The first listing's pages miss d and e, as when the server sorts them
        # before the position reached: the page at 3 comes back empty, and the
        # one after it never goes out. Only the changes since the listing's
        # first state, read before its last page, bring them in.
        tape = _recording("cyrus-listing_moved")
        last = _index(tape, "CalendarEvent/query", "position", nth=2)
        answer = json.loads(tape.exchanges[last].body)
        answer["methodResponses"][0][1]["ids"] = []
        answer["methodResponses"][1][1]["list"] = []
        edit_answer(tape, last, body=json.dumps(answer))
        del tape.exchanges[last + 1]
        second_sync = [
            i
            for i, exchange in enumerate(tape.exchanges)
            if i > last and '"ids": []' in exchange.request
        ][1]
        del tape.exchanges[second_sync:]
        with (
            _replaying(tape, store, asynchronous) as env,
            mock.patch.object(adapter_module, "PAGE_SIZE", 1),
        ):
            sync_while_d_and_e_arrive(env)
        assert {env.record(uid).state for uid in ("d", "e")} == {RecordState.SYNCED}

    def test_a_listing_ends_early_when_its_first_state_is_refused(self, store, asynchronous):
        # The first listing moved; the changes since its first state are refused.
        tape = _recording("cyrus-listing_moved")
        index = _index(tape, "CalendarEvent/changes")
        edit_answer(tape, index, body=_error("cannotCalculateChanges"))
        del tape.exchanges[index + 1 :]
        with (
            _replaying(tape, store, asynchronous) as env,
            mock.patch.object(adapter_module, "PAGE_SIZE", 1),
        ):
            sync_while_d_and_e_arrive(env)
        # The pages before the last were ingested; the listing's token never was.
        assert env.replica.scope(env.adapter.scope_id).token is None
        assert env.replica.records(env.adapter.scope_id)


class TestPushing:
    """Sending pending edits, and how each push ends."""

    def test_a_create_whose_answer_was_lost_is_recognized_by_its_uid(self, store, asynchronous):
        # The server made the event, but its answer fails: the push stays in
        # flight with no id, and the next sync's changes bring the event,
        # which the core takes as that create by its uid, not as a new event.
        tape = _recording("cyrus-pushing")
        create = _index(tape, "CalendarEvent/set", "create", "ifInState")
        edit_answer(tape, create, body=_error("serverFail"))
        # The recording goes on to change the event; keep up to the sync after the create.
        update = _index(tape, "CalendarEvent/set", "update", "ifInState")
        read_before_update = _index(tape, "CalendarEvent/changes", nth=-1, before=update)
        del tape.exchanges[read_before_update:]
        with _replaying(tape, store, asynchronous) as env:
            key = create_c(env)
            with pytest.raises(JMAPMethodError, match="serverFail"):
                env.sync()
            assert env.at(key).state is RecordState.IN_FLIGHT
            env.sync()
        (record,) = env.replica.records(env.adapter.scope_id)
        assert (record.local_key, record.state) == (key, RecordState.SYNCED)
        assert record.object_id is not None

    @pytest.mark.parametrize(
        "kind, refusal, state, conflict, raised",
        [
            pytest.param(
                "create", "stateMismatch", RecordState.PENDING, None, None, id="create, state moved"
            ),
            pytest.param(
                "create",
                ("invalidProperties", ["uid"]),
                RecordState.CONFLICT,
                ConflictKind.LOGICAL_KEY_TAKEN,
                None,
                id="create, uid taken",
            ),
            pytest.param(
                "create",
                ("notFound", None),
                RecordState.IN_FLIGHT,
                None,
                JMAPMethodError,
                id="create, not found",
            ),
            pytest.param(
                "change",
                ("invalidProperties", ["uid"]),
                RecordState.CONFLICT,
                ConflictKind.LOGICAL_KEY_TAKEN,
                None,
                id="change, uid taken",
            ),
            pytest.param(
                "change",
                ("notFound", None),
                RecordState.CONFLICT,
                ConflictKind.REMOVED_REMOTELY,
                None,
                id="change, gone",
            ),
            pytest.param(
                "change",
                ("invalidProperties", ["title"]),
                RecordState.IN_FLIGHT,
                None,
                JMAPMethodError,
                id="change, other property",
            ),
            pytest.param(
                "delete", ("notFound", None), RecordState.REMOVED, None, None, id="delete, gone"
            ),
            pytest.param(
                "delete",
                ("invalidProperties", ["uid"]),
                RecordState.IN_FLIGHT,
                None,
                JMAPMethodError,
                id="delete, uid named",
            ),
            pytest.param(
                "create",
                ("invalidProperties", ["calendarIds"]),
                RecordState.IN_FLIGHT,
                None,
                JMAPMethodError,
                id="create, calendar named, no event holds its uid",
            ),
            pytest.param(
                "delete",
                ("invalidProperties", ["calendarIds"]),
                RecordState.IN_FLIGHT,
                None,
                JMAPMethodError,
                id="delete, calendar named",
            ),
        ],
    )
    def test_how_a_refused_push_ends(
        self, store, asynchronous, kind, refusal, state, conflict, raised
    ):
        record = _refused_push(store, asynchronous, kind, refusal, raised)
        assert (record.state, record.conflict and record.conflict.kind) == (state, conflict)

    def test_a_refusal_naming_the_calendar_is_a_taken_uid_where_an_event_holds_it(
        self, store, asynchronous
    ):
        def held(*_: Any) -> Steps[bool]:
            return True
            # Never reached; it makes this a generator, as the method it stands in for is.
            yield

        refusal = ("invalidProperties", ["calendarIds"])
        with mock.patch.object(adapter_class(asynchronous), "_uid_held", held):
            record = _refused_push(store, asynchronous, "create", refusal, None)
        assert record.conflict is not None
        assert record.conflict.kind is ConflictKind.LOGICAL_KEY_TAKEN

    @pytest.mark.parametrize(
        "logical_key, containers, state, itself, held",
        [
            pytest.param("u", _CALENDAR, RecordState.SYNCED, False, True, id="same uid"),
            pytest.param(
                "u 2026-11-02T10:00:00", _CALENDAR, RecordState.SYNCED, False, True, id="instance"
            ),
            pytest.param("u", "other", RecordState.SYNCED, False, False, id="another calendar"),
            pytest.param("u2", _CALENDAR, RecordState.SYNCED, False, False, id="a uid it starts"),
            pytest.param("u", _CALENDAR, RecordState.REMOVED, False, False, id="removed"),
            pytest.param("u", _CALENDAR, RecordState.SYNCED, True, False, id="the record itself"),
        ],
    )
    def test_which_event_holds_a_uid(self, store, logical_key, containers, state, itself, held):
        record = replace(
            _local_record(state, "x"), logical_key=logical_key, containers=frozenset({containers})
        )
        assert _uid_held(store, record, itself=itself) is held

    @pytest.mark.parametrize(
        "remote_key, remote_calendar, held",
        [
            pytest.param("u", _CALENDAR, True, id="the server's copy has the uid"),
            pytest.param("u", "other", False, id="the server's copy moved calendar"),
            pytest.param(None, _CALENDAR, False, id="the server removed it"),
        ],
    )
    def test_an_event_in_conflict_holds_what_the_servers_copy_holds(
        self, store, remote_key, remote_calendar, held
    ):
        record = _local_record(RecordState.CONFLICT, "x")
        conflict = replace(
            cast(Conflict, record.conflict),
            remote_logical_key=remote_key,
            remote_containers=frozenset({remote_calendar}),
        )
        synced = frozenset({_CALENDAR})
        record = replace(record, logical_key="v", containers=synced, conflict=conflict)
        assert _uid_held(store, record) is held

    def test_an_edit_whose_uid_isnt_text_matches_no_event(self, store):
        # The key a uid of 5 would match if it were taken as the text "5".
        record = replace(
            _local_record(RecordState.SYNCED, "x"),
            logical_key="5 2026-11-02T10:00:00",
            containers=frozenset({_CALENDAR}),
        )
        assert _uid_held(store, record, uid=5) is False

    @pytest.mark.parametrize(
        "error, kind",
        list(itertools.product(_REFUSALS, EditKind)),
        ids=lambda v: v.value if isinstance(v, EditKind) else "-".join(map(str, filter(None, v))),
    )
    def test_every_refusal_of_every_edit_kind(self, error, kind):
        error_type, properties = error
        refused = JMAPMethodError(
            error_type=error_type, properties=None if properties is None else list(properties)
        )
        expected = _REFUSALS[error][kind.value]
        outcome = _refusal(kind, refused)
        assert (None if outcome is None else type(outcome)) is expected


class TestScenarios:
    """Steps the scenarios share with the integration tests."""

    def test_emptying_an_account_skips_an_event_already_gone(self):
        class Client:
            searched = 0

            def search_events(self, **_: Any) -> Any:
                self.searched += 1
                gone = [] if self.searched > 1 else [mock.Mock(data={"id": "e1"})]
                return mock.Mock(events=gone)

            def delete_event(self, event_id: str, **_: Any) -> None:
                raise JMAPMethodError(error_type="notFound")

        scenarios._clear(Client(), "a")  # type: ignore[arg-type]

    def test_emptying_an_account_raises_any_other_refusal(self):
        client = mock.Mock()
        client.search_events.return_value = mock.Mock(events=[mock.Mock(data={"id": "e1"})])
        client.delete_event.side_effect = JMAPMethodError(error_type="forbidden")
        with pytest.raises(JMAPMethodError):
            scenarios._clear(client, "a")


class TestReplay:
    """The tape a recording replays through."""

    def test_a_recording_keeps_each_json_request(self):
        class Answer:
            status_code, headers, content = 200, {"Content-Type": "application/json"}, b"{}"

        class Server:
            def request(self, *args: Any, **kwargs: Any) -> Answer:
                return Answer()

        tape = Tape(PROTOCOL)
        tape.session(Server()).post(f"{REPLAY_URL}/jmap/", json={"b": 1, "a": 2})
        assert tape.exchanges[0].request == '{"a": 2, "b": 1}'

    def test_a_request_with_other_calls_fails(self):
        # Every JMAP call goes to one path, so a replay checks the calls sent too.
        tape = _recording("cyrus-reading")
        index = _index(tape, "CalendarEvent/changes")
        tape.exchanges[index] = replace(tape.exchanges[index], request='{"methodCalls": []}')
        with pytest.raises(AssertionError, match=f"^Request {index}: .*, recorded "):
            _run(SERVERS["cyrus"], "reading", tape)

    def test_a_failed_answer_raises_as_a_client_library_would(self):
        with pytest.raises(Exception, match="500"):
            _Reply(Exchange("GET", "/", 500, {}, "")).raise_for_status()
