# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the CalDAV adapter, on exchanges recorded from real servers.

``test_adapters`` replays every recording through both adapters, and
``record_caldav`` records them again. A rule no tested server shows on its
own is tested here on one exchange of a recording, edited in the test.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from caldav.collection import Calendar
from caldav.lib import error as dav_error

from calendaring_sync import (
    ConflictKind,
    OtherScopeError,
    RecordState,
    ScopeGoneError,
    StateStore,
    UnknownRecordError,
    UnknownScopeError,
)
from calendaring_sync.adapters import caldav as caldav_adapter
from calendaring_sync.adapters.caldav import (
    FINGERPRINT_SCHEME,
    MEDIA_TYPE,
    AsyncCalDAVAdapter,
    CalDAVAdapter,
    _fields,
    _server_fields,
)
from calendaring_sync.replica import Replica
from calendaring_sync.session import Session
from calendaring_sync.tests import _caldav_scenarios as scenarios
from calendaring_sync.tests import record_caldav
from calendaring_sync.tests._caldav_scenarios import (
    PROTOCOL,
    SCENARIOS,
    SERVERS,
    Env,
    adapter_class,
    caldav_client,
    calendar_path,
    edit_a_changed_behind,
)
from calendaring_sync.tests._helpers import _SCOPE, _WAIT, _assert_adapter_refuses
from calendaring_sync.tests._scenarios import (
    REPLAY_URL,
    create_c,
    done,
    edit_a,
    edit_answer,
    fields_of,
    forgotten_edit,
    put_a_and_b,
    recorded,
    replaying,
    run,
    sync_a_and_b,
)
from calendaring_sync.tests._tape import Exchange, Tape, _Reply

# An event with a time zone, a recurrence rule and parameters, for the
# fingerprint rules to work on.
_EVENT = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//one product//EN",
    "BEGIN:VTIMEZONE",
    "TZID:Europe/Berlin",
    "BEGIN:STANDARD",
    "DTSTART:19701025T030000",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "END:STANDARD",
    "END:VTIMEZONE",
    "BEGIN:VEVENT",
    "UID:event-1",
    "DTSTAMP:20261001T000000Z",
    "DTSTART;TZID=Europe/Berlin;VALUE=DATE-TIME:20261101T100000",
    "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO",
    "SUMMARY:Planning",
    "ATTENDEE;CN=Ana;ROLE=CHAIR:mailto:ana@example.org",
    "END:VEVENT",
    "END:VCALENDAR",
]


# Bytes that aren't UTF-8.
_NOT_UTF8 = b"\xff"


def _ical(lines: list[str], newline: str = "\r\n") -> bytes:
    return "".join(line + newline for line in lines).encode()


def _swap(first: str, second: str) -> list[str]:
    """The event with two of its lines swapped."""
    lines = list(_EVENT)
    i, j = lines.index(first), lines.index(second)
    lines[i], lines[j] = lines[j], lines[i]
    return lines


def _with(old: str, *new: str) -> list[str]:
    """The event with one of its lines replaced by ``new``."""
    lines = list(_EVENT)
    i = lines.index(old)
    return lines[:i] + list(new) + lines[i + 1 :]


def _text(body: str, element: str) -> str:
    """What the first ``element`` of a recorded answer holds, such as its sync token."""
    return body.split(f"<{element}>")[1].split(f"</{element}>")[0]


_tape = partial(recorded, PROTOCOL)


_edit = edit_answer


def _first_two_syncs(env: Env) -> None:
    """The first two syncs of the ``reading`` scenario: an empty listing, then a and b."""
    env.sync()
    sync_a_and_b(env)


_replaying = partial(replaying, scenarios)


_run = partial(run, scenarios)


def _calendar(asynchronous: bool, *, features: Any = None) -> Calendar:
    client = caldav_client(asynchronous, url=REPLAY_URL, features=features)
    return Calendar(client=client, url=f"{REPLAY_URL}/cal/")


def _session(replica: Replica, scope_id: str) -> AbstractContextManager[Session]:
    """A session on a CalDAV scope, which makes the scope if it's new."""
    return replica.session(scope_id, fingerprint_scheme=FINGERPRINT_SCHEME, media_type=MEDIA_TYPE)


def _adapter(store: StateStore, asynchronous: bool) -> CalDAVAdapter | AsyncCalDAVAdapter:
    """An adapter on the test scope, which exists."""
    adapter = adapter_class(asynchronous)(Replica(store), _calendar(asynchronous), scope_id=_SCOPE)
    with _session(adapter._replica, _SCOPE):
        pass
    return adapter


class TestAdapter:
    """Making an adapter, and the calls it takes."""

    @pytest.mark.parametrize(
        "url",
        [
            f"{REPLAY_URL}/a@b/",
            "HTTP://Recorded.Invalid/a@b/",
            "http://someone:secret@recorded.invalid:80/a%40b/",
        ],
        ids=["as written", "capitals", "login"],
    )
    def test_scope_id_defaults_to_the_calendars_url(self, store, asynchronous, url):
        calendar = Calendar(client=caldav_client(asynchronous, url=url), url=url)
        # One spelling, the port included and no login, however the URL was written.
        assert (
            adapter_class(asynchronous)(Replica(store), calendar).scope_id
            == "http://recorded.invalid:80/a%40b/"
        )
        assert _adapter(store, asynchronous).scope_id == _SCOPE

    def test_an_edit_gets_the_fields_a_sync_gives(self, store, asynchronous):
        adapter = _adapter(store, asynchronous)
        key = done(adapter.create(_ical(_EVENT)))
        record = adapter._replica.record(key)
        assert record is not None and record.edit is not None
        edit, fields = record.edit, _fields(_ical(_EVENT))
        assert (edit.fingerprint, edit.logical_key, edit.containers) == (
            fields.fingerprint,
            fields.logical_key,
            frozenset({_SCOPE}),
        )
        assert fields_of(adapter.edit_fields(_ical(_EVENT))) == fields_of(edit)
        assert re.fullmatch(r"/cal/[0-9a-f-]{36}\.ics", record.object_id or "")

    @pytest.mark.parametrize(
        "features, needed",
        [
            (None, False),
            ("radicale", False),
            ("cyrus", False),
            ("xandikos", True),
            ("stalwart", True),
        ],
    )
    def test_full_listings_are_needed_where_a_token_isnt_refused(self, store, features, needed):
        adapter = CalDAVAdapter(Replica(store), _calendar(False, features=features))
        assert adapter.needs_full_listings is needed

    def test_a_client_without_features_needs_no_full_listings(self, store):
        adapter = _adapter(store, asynchronous=False)
        adapter._client.features = None
        assert adapter.needs_full_listings is False

    def test_a_calendar_on_the_other_kind_of_client_is_refused(self, store):
        replica = Replica(store)
        with pytest.raises(TypeError, match="takes a calendar on a synchronous client"):
            CalDAVAdapter(replica, _calendar(asynchronous=True))
        with pytest.raises(TypeError, match="takes a calendar on an asynchronous client"):
            AsyncCalDAVAdapter(replica, _calendar(asynchronous=False))
        with pytest.raises(TypeError, match="calendar must be"):
            CalDAVAdapter(replica, "a calendar")  # type: ignore[arg-type]

    @pytest.mark.parametrize("missing", ["url", "client"])
    def test_a_calendar_without_url_or_client_is_refused(self, store, asynchronous, missing):
        calendar = _calendar(asynchronous)
        setattr(calendar, missing, None)
        with pytest.raises(ValueError, match="no URL or no client"):
            adapter_class(asynchronous)(Replica(store), calendar)

    # Each row calls an adapter of ``scope_id``: local key 1 is the record
    # the test makes in the test scope, 2 doesn't exist.
    @pytest.mark.parametrize(
        "call, scope_id, error, match, before",
        [
            pytest.param(
                lambda a: a.create(_NOT_UTF8),
                _SCOPE,
                ValueError,
                "must be UTF-8",
                True,
                id="create",
            ),
            pytest.param(
                lambda a: a.create(_ical(_with("UID:event-1"))),
                _SCOPE,
                ValueError,
                "with a UID",
                True,
                id="create, no UID",
            ),
            pytest.param(
                lambda a: a.edit_fields(_ical(_with("UID:event-1"))),
                _SCOPE,
                ValueError,
                "with a UID",
                True,
                id="fields, no UID",
            ),
            pytest.param(
                lambda a: a.change(1, _NOT_UTF8),
                _SCOPE,
                ValueError,
                "must be UTF-8",
                True,
                id="change",
            ),
            pytest.param(
                lambda a: a.change(1, _ical(_with("UID:event-1"))),
                _SCOPE,
                ValueError,
                "with a UID",
                False,
                id="change of an unsent create, no UID",
            ),
            pytest.param(
                lambda a: a.create(_ical(_EVENT)),
                "nope",
                UnknownScopeError,
                None,
                False,
                id="scope",
            ),
            pytest.param(
                lambda a: a.change(2, _ical(_EVENT)),
                _SCOPE,
                UnknownRecordError,
                None,
                False,
                id="record",
            ),
            pytest.param(
                lambda a: a.change(1, _ical(_EVENT)),
                "nope",
                OtherScopeError,
                "^Record 1 is in scope 'cal', not this one$",
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
        assert done(made.create(_ical(_EVENT))) == 1
        adapter = adapter_class(asynchronous)(
            made._replica, _calendar(asynchronous), scope_id=scope_id
        )
        _assert_adapter_refuses(adapter, store, call, error, match, before)


class TestFingerprint:
    """The fingerprint and logical key read from iCalendar data."""

    @pytest.mark.parametrize(
        "lines, newline",
        [
            pytest.param(_EVENT, "\n", id="LF line endings"),
            pytest.param(_with("SUMMARY:Planning", "SUMMARY:Plan", " ning"), "\r\n", id="folded"),
            pytest.param(_swap("SUMMARY:Planning", "UID:event-1"), "\r\n", id="properties moved"),
            pytest.param(
                _EVENT[:3] + _EVENT[11:19] + _EVENT[3:11] + _EVENT[19:],
                "\r\n",
                id="components moved",
            ),
            pytest.param(
                _with(
                    "DTSTART;TZID=Europe/Berlin;VALUE=DATE-TIME:20261101T100000",
                    "DTSTART;VALUE=DATE-TIME;TZID=Europe/Berlin:20261101T100000",
                ),
                "\r\n",
                id="parameters moved",
            ),
            pytest.param(
                _with(
                    "ATTENDEE;CN=Ana;ROLE=CHAIR:mailto:ana@example.org",
                    "attendee;cn=Ana;role=CHAIR:mailto:ana@example.org",
                ),
                "\r\n",
                id="names in lower case",
            ),
            pytest.param(
                _with("RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO", "RRULE:BYDAY=MO;FREQ=WEEKLY;COUNT=3"),
                "\r\n",
                id="recurrence rule parts moved",
            ),
            pytest.param(
                _with("DTSTAMP:20261001T000000Z", "DTSTAMP:20261023T120000Z"),
                "\r\n",
                id="DTSTAMP",
            ),
            pytest.param(
                _with("PRODID:-//one product//EN", "PRODID:-//another product//EN"),
                "\r\n",
                id="PRODID",
            ),
            pytest.param(
                _with("UID:event-1", "UID:event-1", "LAST-MODIFIED:20261023T120000Z"),
                "\r\n",
                id="LAST-MODIFIED",
            ),
        ],
    )
    def test_what_a_server_rewrites_keeps_the_fingerprint(self, lines, newline):
        assert _fields(_ical(lines, newline)) == _fields(_ical(_EVENT))

    @pytest.mark.parametrize(
        "lines",
        [
            pytest.param(_with("SUMMARY:Planning", "SUMMARY:Review"), id="value"),
            pytest.param(_with("UID:event-1", "UID:event-2"), id="UID"),
            pytest.param(
                _with("RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO", "RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO"),
                id="recurrence rule",
            ),
            pytest.param(
                _with(
                    "ATTENDEE;CN=Ana;ROLE=CHAIR:mailto:ana@example.org",
                    "ATTENDEE;CN=Anna;ROLE=CHAIR:mailto:ana@example.org",
                ),
                id="parameter",
            ),
            pytest.param(
                _with("SUMMARY:Planning", "SUMMARY:Planning", "LOCATION:Room 1"), id="property"
            ),
            pytest.param(_with("TZOFFSETTO:+0100", "TZOFFSETTO:+0000"), id="time zone"),
        ],
    )
    def test_an_edit_changes_the_fingerprint(self, lines):
        assert _fields(_ical(lines)).fingerprint != _fields(_ical(_EVENT)).fingerprint

    @pytest.mark.parametrize(
        "lines, logical_key",
        [
            pytest.param(_EVENT, "event-1", id="after a time zone"),
            pytest.param(_with("UID:event-1", r"UID:one\, two"), "one, two", id="escaped"),
            pytest.param(
                [line.replace("VEVENT", "VTODO") for line in _EVENT], "event-1", id="task"
            ),
            pytest.param(_with("UID:event-1"), None, id="none"),
            pytest.param(
                _with("TZID:Europe/Berlin", "TZID:Europe/Berlin", "UID:zone"),
                "event-1",
                id="not the time zone's",
            ),
            pytest.param(
                _with(
                    "UID:event-1",
                    "BEGIN:VALARM",
                    "UID:alarm",
                    "ACTION:DISPLAY",
                    "TRIGGER:-PT5M",
                    "END:VALARM",
                ),
                None,
                id="not an alarm's",
            ),
        ],
    )
    def test_the_logical_key_is_the_objects_uid(self, lines, logical_key):
        assert _fields(_ical(lines)).logical_key == logical_key

    def test_what_a_server_sends_always_gets_a_fingerprint(self):
        assert _server_fields(_ical(_EVENT)) == _fields(_ical(_EVENT))
        broken = _ical(_EVENT[:-1])
        fields = _server_fields(broken)
        assert (fields.fingerprint, fields.logical_key) == (
            hashlib.sha256(broken).hexdigest(),
            None,
        )

    @pytest.mark.parametrize(
        "content, match",
        [
            pytest.param(_NOT_UTF8, "must be UTF-8", id="not UTF-8"),
            pytest.param(b"", "whole components", id="empty"),
            pytest.param(_ical(_EVENT[1:3]), "whole components", id="no component"),
            pytest.param(_ical(_EVENT[:-1]), "whole components", id="not ended"),
            pytest.param(_ical(["END:VEVENT"]), "never began", id="never began"),
            pytest.param(
                _ical(["BEGIN:VCALENDAR", "BEGIN:VEVENT", "END:VCALENDAR", "END:VEVENT"]),
                "ends VCALENDAR inside VEVENT",
                id="ended out of order",
            ),
        ],
    )
    def test_what_isnt_calendar_data_is_refused(self, content, match):
        with pytest.raises(ValueError, match=match):
            _fields(content)


class TestReading:
    """Reading changes from a report."""

    def test_a_member_reported_with_its_stored_etag_isnt_fetched(self, store, asynchronous):
        # A third report, added after the second (7), names a and b again with
        # the ETags the second sync stored, so no multiget follows it.
        tape = _tape("radicale-reading", keep=9)
        again = "http://radicale.org/ns/sync/again"
        report = tape.exchanges[7]
        tape.exchanges.append(
            replace(report, body=report.body.replace(_text(report.body, "sync-token"), again))
        )
        with _replaying(tape, store, asynchronous) as env:
            _first_two_syncs(env)
            env.sync()
        assert env.replica.scope(env.adapter.scope_id).token == again

    def test_a_report_without_a_token_keeps_the_scope_token(self, store, asynchronous):
        # Exchange 7 is the second sync's report; it loses its token.
        tape = _tape("radicale-reading", keep=9)
        _edit(
            tape, 7, old=f"<sync-token>{_text(tape.exchanges[7].body, 'sync-token')}</sync-token>"
        )
        with _replaying(tape, store, asynchronous) as env:
            env.sync()
            token = env.replica.scope(env.adapter.scope_id).token
            sync_a_and_b(env)
        assert env.replica.scope(env.adapter.scope_id).token == token

    def test_a_member_answered_without_an_etag_is_kept(self, store, asynchronous):
        # Exchange 10 is the second listing's multiget, for b; it loses b's ETag.
        tape = _tape("radicale-listing")
        etag = _text(tape.exchanges[10].body, "getetag")
        _edit(tape, 10, old=f"<getetag>{etag}</getetag>")
        with _replaying(tape, store, asynchronous) as env:
            SCENARIOS["listing"](env)
        b = env.record("b")
        assert (b.state, b.version) == (RecordState.SYNCED, None)

    def test_a_member_reported_removed_that_still_has_data_is_kept(self, store, asynchronous):
        # The second sync's report (7) names b removed, as Nextcloud names a
        # member deleted and created again; the multiget for the removals,
        # after the one for a (8), still returns it.
        tape = _tape("radicale-reading", keep=9)
        tape.exchanges.append(tape.exchanges[8])
        b = "<response><href>/user1/calendaring-sync-reading/b.ics</href>"
        _edit(
            tape,
            7,
            old=b + tape.exchanges[7].body.split(b)[1].split("</response>")[0] + "</response>",
            new=f"{b}<status>HTTP/1.1 404 Not Found</status></response>",
        )
        with _replaying(tape, store, asynchronous) as env:
            _first_two_syncs(env)
        assert env.record("b").state is RecordState.SYNCED

    @pytest.mark.parametrize("calendar_there", [True, False], ids=["member gone", "calendar gone"])
    def test_a_multiget_refused_whole_is_fetched_member_by_member(
        self, store, asynchronous, calendar_there
    ):
        # Posteo answers the second sync's multiget (8) with 404 when one
        # member is gone. A read of the token then shows whether the calendar
        # is there, and a multiget for each member follows: a, then b, gone.
        tape = _tape("radicale-reading", keep=9)
        multiget = tape.exchanges[8]
        head, *members = multiget.body.split("<response>")
        only_a = head + "<response>" + members[1].replace("</multistatus>", "") + "</multistatus>"
        gone = replace(multiget, status=404, body="")
        token_read = replace(tape.exchanges[2], status=207 if calendar_there else 404)
        tape.exchanges[8:] = [gone, token_read]
        if calendar_there:
            tape.exchanges += [replace(multiget, body=only_a), gone]
        with _replaying(tape, store, asynchronous) as env:
            env.sync()
            put_a_and_b(env)
            with nullcontext() if calendar_there else pytest.raises(ScopeGoneError):
                env.sync()
        assert [r.object_id for r in env.replica.records(env.adapter.scope_id)] == (
            ["/user1/calendaring-sync-reading/a.ics"] if calendar_there else []
        )

    def test_a_truncated_report_ingests_its_page_and_pushes_nothing(self, store, asynchronous):
        # Exchange 7 is the second sync's report; 8 fetches what it named.
        tape = _tape("radicale-reading", keep=9)
        end = "</multistatus>"
        _edit(
            tape,
            7,
            old=end,
            new="<response><href>/user1/calendaring-sync-reading/</href>"
            f"<status>HTTP/1.1 507 Insufficient Storage</status></response>{end}",
        )
        checkpoint = _text(tape.exchanges[7].body, "sync-token")
        with _replaying(tape, store, asynchronous) as env:
            # A push would be a request the tape doesn't have.
            key = create_c(env)
            synced_at = env.replica.scope(env.adapter.scope_id).last_sync_at
            env.calendar.sync_max_pages = 1
            sync_a_and_b(env)
        assert env.at(key).state is RecordState.PENDING
        scope = env.replica.scope(env.adapter.scope_id)
        # A page that isn't the last moves the token but doesn't finish the sync.
        assert (scope.token, scope.last_sync_at) == (checkpoint, synced_at)

    @pytest.mark.parametrize(
        "index, edit, error, reset",
        [
            pytest.param(
                7,
                {"old": "<valid-sync-token />"},
                dav_error.AuthorizationError,
                False,
                id="refusal",
            ),
            pytest.param(8, {"status": 500, "body": ""}, dav_error.DAVError, True, id="listing"),
        ],
    )
    def test_a_token_sync_that_fails_is_raised(
        self, store, asynchronous, index, edit, error, reset
    ):
        # Exchange 7 refuses the token. Without the precondition naming why,
        # that's raised as it is; with it, the scope is reset and its listing
        # starts (8), and a listing that fails leaves the scope reset.
        tape = _tape("radicale-invalid_token", keep=index + 1)
        _edit(tape, index, **edit)
        with _replaying(tape, store, asynchronous) as env:
            key = forgotten_edit(env)
            with pytest.raises(error):
                env.sync()
        token = env.replica.scope(env.adapter.scope_id).token
        assert token == (None if reset else env.server.unknown_token)
        assert env.at(key).state is RecordState.PENDING


class TestListing:
    """Full listings."""

    def test_a_full_listing_goes_to_the_core_in_pages(self, store, asynchronous):
        # From the second listing on, a page holds one object: the calendar's
        # own entry, then a (unchanged), then b, which one multiget fetches
        # as recorded. Each page but the last is truncated.
        tape = _tape("radicale-listing")
        one_a_page = patch.object(caldav_adapter, "MULTIGET_SIZE", 1)
        with _replaying(tape, store, asynchronous) as env:
            env.tape.before("PUT", "SUMMARY:two", one_a_page.start)
            try:
                SCENARIOS["listing"](env)
            finally:
                one_a_page.stop()

    @pytest.mark.parametrize("missing", ["change reports", "token"])
    def test_a_calendar_without_change_reports_or_a_token_is_listed(
        self, store, asynchronous, missing
    ):
        # The second sync's report (7) becomes a listing: the token as the
        # first listing (2) read it, then the members the report named, with
        # their ETags. Without change reports, the supported reports (6) leave
        # the report out; without a token, they aren't read.
        tape = _tape("radicale-reading", keep=9)
        report, token_read = tape.exchanges[7], tape.exchanges[2]
        token = _text(report.body, "sync-token")
        listing = [
            replace(
                token_read,
                body=token_read.body.replace(_text(token_read.body, "sync-token"), token),
            ),
            replace(
                report,
                method="PROPFIND",
                body=report.body.replace(f"<sync-token>{token}</sync-token>", ""),
            ),
        ]
        if missing == "token":
            _edit(tape, 2, old=f"<sync-token>{_text(token_read.body, 'sync-token')}</sync-token>")
            tape.exchanges[6:8] = listing
        else:
            _edit(
                tape,
                6,
                old="<supported-report><report><sync-collection /></report></supported-report>",
            )
            tape.exchanges[7:8] = listing
        with _replaying(tape, store, asynchronous) as env:
            _first_two_syncs(env)
        assert env.replica.scope(env.adapter.scope_id).token == token

    def test_a_cut_the_client_reports_by_status_is_a_cut(self, store):
        # python-caldav after 3.6.0 gives the 507 as a status, not in the message.
        tape = _tape("stalwart-listing_cut_short")
        with _replaying(tape, store, asynchronous=False) as env:
            get_properties = env.calendar.get_properties

            def by_status(*args: Any, **kwargs: Any) -> Any:
                try:
                    return get_properties(*args, **kwargs)
                except dav_error.ResponseError:
                    raise dav_error.ResponseError(status=507) from None

            with patch.object(env.calendar, "get_properties", by_status):
                SCENARIOS["listing_cut_short"](env)

    def test_a_listing_error_other_than_a_cut_is_raised(self, store, asynchronous):
        # Exchange 6 is the listing, cut short.
        tape = _tape("stalwart-listing_cut_short")
        _edit(tape, 6, old="507 Insufficient Storage", new="500 Internal Server Error")
        with _replaying(tape, store, asynchronous) as env:
            for uid in ("a", "b", "c"):
                env.put(uid, "one")
            with pytest.raises(dav_error.ResponseError, match=" 500 "):
                env.sync()
        assert env.replica.records(env.adapter.scope_id) == []


class TestPushing:
    """Sending pending edits, and how each push ends."""

    def test_a_change_goes_out_with_its_sequence_unchanged(self, store, asynchronous):
        # Exchange 13 is the change's PUT, which must carry the bytes recorded:
        # a save bumps SEQUENCE unless told not to, or unless given raw bytes.
        tape = _tape("radicale-pushing", keep=14)
        with _replaying(tape, store, asynchronous) as env:
            key = create_c(env)
            env.sync()
            env.sync()
            content = env.content("c", "changed here")
            nl = b"\r\n" if b"\r\n" in content else b"\n"
            end = nl + b"END:VEVENT"
            env.change(key, content.replace(end, nl + b"SEQUENCE:1" + end, 1))
            env.never("PUT", "SEQUENCE:2", "The change went out with its SEQUENCE bumped")
            env.sync()
        assert env.at(key).state is RecordState.SYNCED

    def test_a_weak_etag_waits(self, store, asynchronous):
        # Exchange 5 fetches a; nothing after the second sync's report is sent.
        tape = _tape("radicale-rejected", keep=8)
        _edit(tape, 5, old='<getetag>"', new='<getetag>W/"')
        with _replaying(tape, store, asynchronous) as env:
            key = edit_a(env)
            env.sync()
        record = env.at(key)
        assert record.state is RecordState.PENDING
        assert record.version is not None and record.version.startswith("W/")
        assert done(env.adapter.waiting()) == [record]
        # Without the edit, nothing waits, though the ETag is still weak.
        env.replica.discard(key)
        assert done(env.adapter.waiting()) == []

    def test_an_edit_another_process_claimed_isnt_sent(self, store, asynchronous):
        # Exchange 5 is the second sync's report; the create's push would follow.
        tape = _tape("radicale-pushing", keep=6)
        with _replaying(tape, store, asynchronous) as env:
            key = create_c(env)
            with patch.object(Session, "claim", return_value=None):
                env.sync()
        assert env.at(key).state is RecordState.PENDING
        # It goes out with a later sync, so it isn't waiting.
        assert done(env.adapter.waiting()) == []

    @pytest.mark.parametrize(
        "recording, edit, status, body, state, conflict, raised",
        [
            pytest.param(
                "radicale-rejected",
                edit_a_changed_behind,
                404,
                "",
                RecordState.CONFLICT,
                ConflictKind.REMOVED_REMOTELY,
                None,
                id="change, gone",
            ),
            pytest.param(
                "radicale-rejected",
                edit_a_changed_behind,
                409,
                '<error xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                "<C:no-uid-conflict/></error>",
                RecordState.CONFLICT,
                ConflictKind.LOGICAL_KEY_TAKEN,
                None,
                id="change, UID taken",
            ),
            pytest.param(
                "radicale-rejected",
                edit_a_changed_behind,
                500,
                "",
                RecordState.IN_FLIGHT,
                None,
                dav_error.PutError,
                id="change, failed",
            ),
            pytest.param(
                "radicale-pushing",
                create_c,
                412,
                "",
                RecordState.PENDING,
                None,
                None,
                id="create, href taken",
            ),
            pytest.param(
                "radicale-pushing",
                create_c,
                500,
                "",
                RecordState.IN_FLIGHT,
                None,
                dav_error.PutError,
                id="create, failed",
            ),
        ],
    )
    def test_how_a_refused_push_ends(
        self, store, asynchronous, recording, edit, status, body, state, conflict, raised
    ):
        # Exchange 9 is the push, in both recordings.
        tape = _tape(recording, keep=10)
        _edit(tape, 9, status=status, body=body)
        with _replaying(tape, store, asynchronous) as env:
            key = edit(env)
            with nullcontext() if raised is None else pytest.raises(raised):
                env.sync()
        record = env.at(key)
        assert (record.state, record.conflict and record.conflict.kind) == (state, conflict)
        # The session ended, after an error too, so the scope takes another.
        with _session(env.replica, env.adapter.scope_id):
            pass

    @pytest.mark.parametrize(
        "body, holder",
        [
            pytest.param(
                '<error xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><C:no-uid-conflict>'
                "<href> /user1/calendaring-sync-uid-taken/dup.ics </href>"
                "</C:no-uid-conflict></error>",
                "/user1/calendaring-sync-uid-taken/dup.ics",
                id="named",
            ),
            pytest.param(
                '<error xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                "<C:unique-scheduling-object-resource><href>/user1/elsewhere/dup.ics</href>"
                "</C:unique-scheduling-object-resource></error>",
                None,
                id="in another calendar",
            ),
            pytest.param(
                '<error xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><C:no-uid-conflict>'
                "<href>https://proxy.invalid/user1/calendaring-sync-uid-taken/dup.ics</href>"
                "</C:no-uid-conflict></error>",
                None,
                id="on another host",
            ),
            pytest.param(
                '<error xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                "<C:no-uid-conflict/></error>",
                None,
                id="not named",
            ),
            pytest.param(
                '<!DOCTYPE error [<!ENTITY dup "/user1/calendaring-sync-uid-taken/dup.ics">]>'
                '<error xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><C:no-uid-conflict>'
                "<href>&dup;</href></C:no-uid-conflict></error>",
                None,
                id="through an entity, which isn't expanded",
            ),
        ],
    )
    def test_a_server_refusing_a_create_may_name_the_holder(
        self, store, asynchronous, body, holder
    ):
        # The UID search (exchange 7) finds nothing, so the server refuses the PUT.
        tape = _tape("radicale-uid_taken")
        _edit(tape, 7, body=tape.exchanges[8].body)
        path = calendar_path(SERVERS["radicale"], "uid_taken")
        tape.exchanges.append(
            Exchange(
                "PUT",
                f"{path}00000000-0000-0000-0000-000000000001.ics",
                409,
                {"content-type": "application/xml"},
                body,
            )
        )
        with _replaying(tape, store, asynchronous) as env:
            SCENARIOS["uid_taken"](env)
        conflict = env.record("dup").conflict
        assert conflict is not None and conflict.holder == holder


class TestInterruptions:
    """A sync cancelled, closed or cut off partway."""

    def test_a_cancelled_sync_ends_its_session_after_the_store_call(self, store):
        # The second sync's ingest runs in a worker thread until the task is
        # cancelled; the session ends only after the ingest has finished.
        tape = _tape("radicale-reading", keep=9)
        ingesting, release = threading.Event(), threading.Event()
        ingest = Session.ingest

        def held_ingest(session: Session, change_set: Any) -> None:
            ingesting.set()
            release.wait(_WAIT)
            ingest(session, change_set)

        async def cancel_mid_ingest(adapter: Any) -> None:
            task = asyncio.ensure_future(adapter.sync())
            await asyncio.to_thread(ingesting.wait, _WAIT)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        with _replaying(tape, store, asynchronous=True) as env:
            env.sync()
            put_a_and_b(env)
            with patch.object(Session, "ingest", held_ingest):
                env.run(cancel_mid_ingest(env.adapter))
        assert [r.state for r in env.replica.records(env.adapter.scope_id)] == [
            RecordState.SYNCED,
            RecordState.SYNCED,
        ]
        with _session(env.replica, env.adapter.scope_id):
            pass

    @pytest.mark.parametrize("last_call", [False, True], ids=["entering", "last call"])
    def test_a_closed_sync_ends_its_session(self, store, last_call):
        # Closing the coroutine while a store call runs waits for that call,
        # then ends the session itself, rather than leaving it to whenever the
        # garbage collector gets to it: while the first call enters the
        # session, or while the first sync's last call, listing the pushes
        # (after exchanges 2 and 3, its listing), runs, when ending the session
        # is the very step the closing driver drops.
        tape = _tape("radicale-reading", keep=4 if last_call else 2)
        listing, release = threading.Event(), threading.Event()
        to_push = Session.to_push

        def held_to_push(session: Session) -> Any:
            listing.set()
            release.wait(_WAIT)
            return to_push(session)

        ended: list[object] = []
        session = Replica.session

        class Watched:
            def __init__(self, manager: Any) -> None:
                self.manager = manager

            def __enter__(self) -> Session:
                if not last_call:
                    # Still entering when the driver suspends, so it can't run on.
                    release.wait(_WAIT)
                return self.manager.__enter__()

            def __exit__(self, *exc: object) -> Any:
                ended.append(exc[0])
                return self.manager.__exit__(*exc)

        async def close_mid_call(adapter: Any) -> None:
            sync = adapter.sync()
            waiting = sync.send(None)
            if last_call:
                held = asyncio.get_running_loop().run_in_executor(None, listing.wait, _WAIT)
                # Drive the sync step by step until its last call is held.
                while not held.done():
                    await asyncio.wait([waiting, held], return_when=asyncio.FIRST_COMPLETED)
                    if not held.done():
                        waiting = sync.send(None)
                threading.Timer(0.05, release.set).start()
            else:
                release.set()
            sync.close()

        with (
            _replaying(tape, store, asynchronous=True) as env,
            patch.object(Replica, "session", lambda *a, **k: Watched(session(*a, **k))),
            patch.object(Session, "to_push", held_to_push),
        ):
            env.run(close_mid_call(env.adapter))
        assert ended == [None]

    def test_a_push_interrupted_while_sending_stays_in_flight(self, store, asynchronous):
        # The interruption comes as the create's PUT (9) would go out.
        tape = _tape("radicale-pushing", keep=9)

        def interrupt() -> None:
            raise KeyboardInterrupt

        with _replaying(tape, store, asynchronous) as env:
            key = create_c(env)
            env.tape.before("PUT", "", interrupt)
            with pytest.raises(KeyboardInterrupt):
                env.sync()
        # The next complete ingest settles it; the session ended, so one can start.
        assert env.at(key).state is RecordState.IN_FLIGHT
        with _session(env.replica, env.adapter.scope_id):
            pass


class TestReplay:
    """The tape a recording replays through."""

    def test_a_recording_keeps_what_a_replay_needs(self, tmp_path, asynchronous):
        answer = Exchange(
            "PROPFIND",
            "/cal/",
            207,
            {"Content-Type": "text/xml", "Date": "today"},
            "<multistatus xmlns='DAV:'/>",
        )

        class Server:
            """A client's own HTTP session, answering every request the same."""

            def __init__(self) -> None:
                self.closed: list[str] = []

            def request(self, *args: Any, **kwargs: Any) -> _Reply:
                return _Reply(answer)

            def close(self) -> None:
                self.closed.append("close")

        class AsyncServer(Server):
            async def request(self, *args: Any, **kwargs: Any) -> _Reply:  # type: ignore[override]
                return _Reply(answer)

            async def close(self) -> None:  # type: ignore[override]
                self.closed.append("close")

            async def aclose(self) -> None:
                self.closed.append("aclose")

        client = caldav_client(asynchronous, url=REPLAY_URL)
        client.session = server = AsyncServer() if asynchronous else Server()
        tape = Tape(PROTOCOL)
        client.session = tape.session(client.session, asynchronous=asynchronous)
        through_tape: Any = client.session
        with patch("calendaring_sync.tests._tape.DATA", tmp_path):
            done(client.propfind(f"{REPLAY_URL}/cal/"))
            done(client.close())
            if asynchronous:
                asyncio.run(through_tape.aclose())
            tape.save("made", {"server": "test"})
            loaded = Tape.load(PROTOCOL, "made")
        assert server.closed == (["close", "aclose"] if asynchronous else ["close"])
        # Only the headers a replay reads are kept.
        assert loaded.exchanges == [replace(answer, headers={"content-type": "text/xml"})]

    @pytest.mark.parametrize(
        "keep, method, error",
        [
            (None, "PUT", "Request 1: MKCALENDAR /user1/calendaring-sync-reading/, recorded PUT"),
            (1, None, "Unrecorded request: MKCALENDAR /user1/calendaring-sync-reading/"),
        ],
    )
    def test_a_request_off_the_recording_fails(self, keep, method, error):
        tape = _tape("radicale-reading", keep=keep)
        if method is not None:
            tape.exchanges[1] = replace(tape.exchanges[1], method=method)
        with pytest.raises(AssertionError, match=error):
            _run(SERVERS["radicale"], "reading", tape)

    def test_a_request_that_must_never_go_out_fails(self, store):
        tape = _tape("radicale-reading", keep=3)
        with (
            pytest.raises(AssertionError, match=r"^No listing$"),
            _replaying(tape, store, asynchronous=False) as env,
        ):
            env.never("PROPFIND", "", "No listing")
            env.sync()


class TestRecorder:
    """The recorder's command line."""

    @pytest.mark.parametrize(
        "argv, login",
        [
            (["cyrus", "reading"], ("http://localhost:8802", "user1", "x")),
            (
                [
                    "cyrus",
                    "reading",
                    "--url",
                    "http://h.invalid",
                    "--username",
                    "u",
                    "--password",
                    "p",
                ],
                ("http://h.invalid", "u", "p"),
            ),
            (["radicale", "reading"], ("http://127.0.0.1:1", "user", "secret")),
        ],
    )
    def test_main_records_and_saves(self, argv, login):
        class Embedded:
            url, username, password = "http://127.0.0.1:1/user/", "user", "secret"

            def __init__(self, config: object) -> None:
                self.running = False

            def start(self) -> None:
                self.running = True

            def stop(self) -> None:
                assert self.running

        with (
            patch.object(sys, "argv", ["record_caldav", *argv]),
            patch.dict(scenarios._EMBEDDED, radicale=Embedded),
            patch("calendaring_sync.tests._scenarios.run") as recorded,
            patch.object(Tape, "save") as saved,
        ):
            record_caldav.main()
        (call,) = recorded.call_args_list
        assert call.args[:3] == (scenarios, SERVERS[argv[0]], "reading")
        assert call.kwargs["login"] == login
        assert saved.call_args.args[0] == f"{argv[0]}-reading"
