# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The JMAP :term:`adapter`, which syncs one account's calendar events through calendaring-jmap.

Install it with the ``jmap`` extra: ``pip install 'calendaring-sync[jmap]'``.
:doc:`/explanation/adapters` explains every rule it follows, and
:doc:`/reference/server-compatibility` how each tested server behaves.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

try:
    from calendaring_jmap import AsyncJMAPClient, JMAPClient, JMAPMethodError
except ImportError as exc:
    raise ImportError(
        "The JMAP adapter needs the jmap extra: pip install 'calendaring-sync[jmap]'"
    ) from exc

from calendaring_sync._checks import check_argument_types
from calendaring_sync.adapters._flow import (
    Steps,
    _arun,
    _EditFields,
    _Flow,
    _InStore,
    _Read,
    _run,
    _url_id,
)
from calendaring_sync.error import ScopeGoneError
from calendaring_sync.objects.change import Change, Changed, Removed
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.push import Applied, Gone, LogicalKeyTaken, PushOutcome, Rejected
from calendaring_sync.objects.record import Record
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica, Session

#: The name of the canonical fingerprint this adapter computes. A change to
#: how it's computed gets a new name, so every scope is listed again.
FINGERPRINT_SCHEME = "jmap-canonical-1"
#: The media type of a JMAP scope's content: JSCalendar (RFC 8984).
MEDIA_TYPE = "application/jscalendar+json"
#: How many events one ``/changes`` page reports, and one page of a
#: :term:`full listing` holds. Below every tested server's ``maxObjectsInGet``.
PAGE_SIZE = 100

# The record type a scope holds, part of its default id.
_RECORD_TYPE = "CalendarEvent"
# Members the server sets on an event (draft-ietf-jmap-calendars-33 sections 5
# and 5.9, RFC 8984 section 4.1.4), and Cyrus's own x-href.
_SERVER_SET = frozenset(
    {
        "id",
        "baseEventId",
        "isOrigin",
        "utcStart",
        "utcEnd",
        "created",
        "updated",
        "sequence",
        "prodId",
        "x-href",
    }
)
# Members a server sets inside a recurrence override's patch.
_OVERRIDE_SERVER_SET = frozenset({"created", "updated", "sequence"})
# Conversion metadata a server may add to any object.
_METADATA = "iCalendar"
# The version a server gives an event created without one (draft-ietf-jmap-calendars-33
# section 5.9); a tested server leaves it out of what it returns.
_VERSION = ("version", "2.0")
# Each member's default on an event (draft-ietf-calext-jscalendarbis-22 and
# draft-ietf-jmap-calendars-33 section 5); a member holding it carries no data.
_EVENT_DEFAULTS: dict[str, object] = {
    "@type": "Event",
    "title": "",
    "description": "",
    "descriptionContentType": "text/plain",
    "showWithoutTime": False,
    "duration": "PT0S",
    "status": "confirmed",
    "priority": 0,
    "freeBusyStatus": "busy",
    "privacy": "public",
    "recurrenceOverrides": {},
    "isDraft": False,
    "useDefaultAlerts": False,
    "mayInviteSelf": False,
    "mayInviteOthers": False,
    "hideAttendees": False,
}
# Each nested object's defaults, by the member that holds it; ``@type`` is
# implied by where the object sits (draft-ietf-calext-jscalendarbis-22 section 1.4.4).
_OBJECT_DEFAULTS: dict[str, dict[str, object]] = {
    "recurrenceRule": {
        "@type": "RecurrenceRule",
        "interval": 1,
        "rscale": "gregorian",
        "skip": "omit",
        "firstDayOfWeek": "mo",
    },
    "byDay": {"@type": "NDay"},
    "alerts": {"@type": "Alert", "action": "display"},
    "trigger": {"@type": "OffsetTrigger", "relativeTo": "start"},
    "links": {"@type": "Link", "rel": "enclosure"},
    "relatedTo": {"@type": "Relation", "relation": {}},
    "locations": {"@type": "Location"},
    "virtualLocations": {"@type": "VirtualLocation", "name": ""},
    "participants": {
        "@type": "Participant",
        "participationStatus": "needs-action",
        "expectReply": False,
    },
}
# Members holding a map of objects of one kind, keyed by id.
_MAPS = frozenset({"alerts", "links", "relatedTo", "locations", "virtualLocations", "participants"})
# A participant's roles, and the one JSCalendar 2.0 no longer lists, which tested servers drop.
_ROLES = "roles"
_DROPPED_ROLE = "attendee"
# The member a refusal for the logical key names.
_UID = "uid"
# How calendaring-jmap reports a server's errors (RFC 8620 sections 3.6.2, 5.2 and 5.3).
_ACCOUNT_NOT_FOUND = "accountNotFound"
_STATE_MISMATCH = "stateMismatch"
_NOT_FOUND = "notFound"
_INVALID_PROPERTIES = "invalidProperties"
# Errors that refuse the state a sync asked from, so the scope is listed again:
# the RFC's own, and the one a tested server gives for a state it can't read.
_REFUSED_STATE = frozenset({"cannotCalculateChanges", "invalidArguments"})


@dataclass(frozen=True)
class _Fields:
    """What the adapter reads from an event for the core."""

    fingerprint: str
    containers: frozenset[str]
    logical_key: str | None


@dataclass(frozen=True)
class _ChangesPage:
    """One page of ``/changes`` from a state, with the events it names fetched."""

    changed: dict[str, Changed]
    destroyed: list[str]
    new_state: str
    more: bool


def _without(obj: dict[str, Any], defaults: dict[str, object]) -> dict[str, Any]:
    """``obj`` without conversion metadata and without the members holding their default."""
    return {
        key: value
        for key, value in obj.items()
        if key != _METADATA and (key not in defaults or defaults[key] != value)
    }


def _canonical_member(name: str, value: Any) -> Any:
    """A member of a full JSCalendar object, its nested objects in canonical form."""
    if name in _MAPS and isinstance(value, dict):
        return {key: _canonical_object(name, item) for key, item in value.items()}
    if name == "byDay" and isinstance(value, list):
        return [_canonical_object(name, item) for item in value]
    if name in _OBJECT_DEFAULTS:
        return _canonical_object(name, value)
    return value


def _canonical_object(kind: str, obj: Any) -> Any:
    if not isinstance(obj, dict):
        return obj
    out = _without(obj, _OBJECT_DEFAULTS[kind])
    roles = out.get(_ROLES)
    if kind == "participants" and isinstance(roles, dict):
        kept = {role: value for role, value in roles.items() if role != _DROPPED_ROLE}
        out = {key: value for key, value in out.items() if key != _ROLES}
        if kept:
            out[_ROLES] = kept
    return {key: _canonical_member(key, value) for key, value in out.items()}


def _canonical(event: dict[str, Any]) -> dict[str, Any]:
    """The event as the fingerprint sees it.

    Left out: the members the server sets, conversion metadata, the version
    a server gives an event created without one, every member holding its
    default (``@type`` where its place implies it), and a participant's
    ``attendee`` role. A recurrence override's absent member means
    "inherited", not "default", so an override only loses what the server
    sets in it.
    """
    out = _without(
        {key: value for key, value in event.items() if key not in _SERVER_SET},
        {**_EVENT_DEFAULTS, _VERSION[0]: _VERSION[1]},
    )
    out = {key: _canonical_member(key, value) for key, value in out.items()}
    overrides = out.get("recurrenceOverrides")
    if isinstance(overrides, dict):
        out["recurrenceOverrides"] = {
            rid: {
                key: value
                for key, value in patch.items()
                if key not in _OVERRIDE_SERVER_SET and key != _METADATA
            }
            if isinstance(patch, dict)
            else patch
            for rid, patch in overrides.items()
        }
    return out


def _event_fields(event: dict[str, Any]) -> _Fields:
    """Read the fingerprint, the calendars and the logical key from an event."""
    canonical = json.dumps(
        _canonical(event), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    calendars = event.get("calendarIds")
    containers = (
        frozenset(key for key, value in calendars.items() if value is True)
        if isinstance(calendars, dict)
        else frozenset()
    )
    uid, recurrence_id = event.get(_UID), event.get("recurrenceId")
    if not isinstance(uid, str) or not uid:
        logical_key = None
    elif isinstance(recurrence_id, str):
        # A recurrence id is a date-time with no space, so the key splits one way.
        logical_key = f"{uid} {recurrence_id}"
    else:
        logical_key = uid
    return _Fields(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), containers, logical_key)


def _event(content: bytes) -> dict[str, Any]:
    """The event JSCalendar content holds.

    Raises:
        ValueError: If the content isn't UTF-8 JSON holding an object.
    """
    try:
        event = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSCalendar content must be UTF-8 JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise ValueError("JSCalendar content must hold one object")
    return event


def _content(event: dict[str, Any]) -> bytes:
    """An event the server sent, as content: compact UTF-8 JSON, members in the server's order."""
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _refusal(kind: EditKind, error: JMAPMethodError) -> PushOutcome | None:
    """The outcome a server's refusal of a push means, or ``None`` if it's no refusal of one."""
    if error.error_type == _STATE_MISMATCH:
        return Rejected()
    if error.error_type == _NOT_FOUND and kind is not EditKind.CREATE:
        return Gone()
    taken = error.error_type == _INVALID_PROPERTIES and _UID in (error.properties or ())
    if taken and kind is not EditKind.DELETE:
        return LogicalKeyTaken()
    return None


class _JMAPFlow(_Flow):
    """Everything the sync and async JMAP adapters share."""

    _fingerprint_scheme = FINGERPRINT_SCHEME
    _media_type = MEDIA_TYPE
    # JMAP has no version per event; one account holds one event per uid and
    # recurrence id; ifInState covers every event in the account.
    _guarded_by_version = False
    _unique_logical_key = True
    _guard_covers_scope = True

    def __init__(
        self, replica: Replica, client: Any, account_id: str, scope_id: str | None
    ) -> None:
        self._client = client
        self._account_id = account_id
        default_id = f"{_url_id(client.url)}#{account_id}/{_RECORD_TYPE}"
        super().__init__(replica, default_id if scope_id is None else scope_id)
        # The account's state as this sync last saw it, which each write must match.
        self._state = ""

    @property
    def needs_full_listings(self) -> bool:
        """Whether the server may leave changes out without refusing the scope's state; never.

        Every tested server refuses a state it can't calculate changes from,
        and the scope is listed again. The one answer that looks like a real
        one: Cyrus answers a state newer than any it issued as if nothing
        changed. So after restoring a server from a backup, list each scope
        once with ``sync(full_listing=True)``.
        """
        return False

    def _new_object_id(self) -> None:
        # The server picks a created event's id.
        return None

    def _waits(self, record: Record) -> bool:
        return False

    def _edit_fields(self, content: bytes, *, create: bool) -> _EditFields:
        event = _event(content)
        fields = _event_fields(event)
        if create and not (isinstance(event.get(_UID), str) and event[_UID]):
            raise ValueError("A created event must have a uid")
        if create and not fields.containers:
            raise ValueError("A created event must be in a calendar")
        return {
            "fingerprint": fields.fingerprint,
            "containers": fields.containers,
            "logical_key": fields.logical_key,
        }

    def _ask(self, request: Callable[[], Any]) -> Steps[Any]:
        """Make one call about the account; an account that's gone raises.

        ``request`` is a function making the call, so a synchronous client's
        error is raised here too.
        """
        try:
            return (yield request())
        except JMAPMethodError as exc:
            if exc.error_type == _ACCOUNT_NOT_FOUND:
                raise ScopeGoneError(f"The server has no account {self._account_id!r}") from exc
            raise

    def _changed(self, objects: Iterable[Any]) -> dict[str, Changed]:
        """Changes for the events a server returned."""
        changed: dict[str, Changed] = {}
        for obj in objects:
            event = obj.data
            fields = _event_fields(event)
            changed[event["id"]] = Changed(
                None, fields.fingerprint, _content(event), fields.containers, fields.logical_key
            )
        return changed

    def _changes_page(self, state: str) -> Steps[_ChangesPage | None]:
        """One page of the changes since ``state``, or ``None`` if the server refused it."""
        try:
            added, modified, destroyed, old_state, new_state, more = yield from self._ask(
                lambda: self._client.get_objects_by_sync_token(
                    state, account_id=self._account_id, max_changes=PAGE_SIZE
                )
            )
        except JMAPMethodError as exc:
            if exc.error_type in _REFUSED_STATE:
                return None
            raise
        # A server that answers from another state than the one asked took a
        # state it never issued (RFC 8620 section 5.2: oldState echoes sinceState).
        if old_state != state:
            return None
        return _ChangesPage(self._changed([*added, *modified]), destroyed, new_state, more)

    def _read(self, session: Session, scope: Scope, *, full_listing: bool) -> Steps[_Read]:
        if full_listing or not scope.token:
            return (yield from self._list(session, scope))
        state = scope.token
        while True:
            page = yield from self._changes_page(state)
            if page is None:
                return _Read.INVALID_TOKEN
            changes: dict[str, Change] = {
                **dict.fromkeys(page.destroyed, Removed()),
                **page.changed,
            }
            change_set = ChangeSet(
                self.scope_id, state, page.new_state, changes, truncated=page.more
            )
            yield _InStore(session.ingest, (change_set,))
            state = page.new_state
            if not page.more:
                self._state = state
                return _Read.COMPLETE

    def _list(self, session: Session, scope: Scope) -> Steps[_Read]:
        """A full listing: the state first, then every event, a page at a time.

        The position is counted here, since a tested server answers every
        page with position 0. Before the last page the state is read again:
        an event destroyed meanwhile shifts the later pages and can hide an
        unchanged one, which the last page would remove, so then the listing
        ends early; events created or changed meanwhile join the last page.
        """
        token = yield from self._ask(
            lambda: self._client.get_sync_token(account_id=self._account_id)
        )
        position = 0
        while True:
            found = yield from self._ask(
                partial(
                    self._client.search_events,
                    account_id=self._account_id,
                    position=position,
                    limit=PAGE_SIZE,
                    calculate_total=True,
                )
            )
            position += len(found.events)
            changes: dict[str, Change] = dict(self._changed(found.events))
            if found.events and (found.total is None or position < found.total):
                change_set = ChangeSet(
                    self.scope_id, scope.token, token, changes, full_listing=True, truncated=True
                )
                yield _InStore(session.ingest, (change_set,))
                continue
            break
        state = yield from self._ask(
            lambda: self._client.get_sync_token(account_id=self._account_id)
        )
        if state != token:
            moved = yield from self._changed_since(token)
            if moved is None:
                return _Read.TRUNCATED
            changes.update(moved[0])
            state = moved[1]
        change_set = ChangeSet(self.scope_id, scope.token, token, changes, full_listing=True)
        yield _InStore(session.ingest, (change_set,))
        self._state = state
        return _Read.COMPLETE

    def _changed_since(self, state: str) -> Steps[tuple[dict[str, Changed], str] | None]:
        """The events created or changed since ``state``, and the state they lead to.

        ``None`` if an event was destroyed since, or the server refused the state.
        """
        changed: dict[str, Changed] = {}
        while True:
            page = yield from self._changes_page(state)
            if page is None or page.destroyed:
                return None
            changed.update(page.changed)
            state = page.new_state
            if not page.more:
                return changed, state

    def _send(self, claimed: Record) -> Steps[PushOutcome]:
        # A claimed record holds its edit; a change or delete has an object id.
        edit = cast(LocalEdit, claimed.edit)
        object_id = cast(str, claimed.object_id)
        account = {"account_id": self._account_id, "if_in_state": self._state}
        try:
            if edit.kind is EditKind.CREATE:
                event = _event(cast(bytes, edit.content))
                created, _, state = yield from self._ask(
                    lambda: self._client.create_event(None, event, **account)
                )
                outcome: PushOutcome = Applied(object_id=created)
            elif edit.kind is EditKind.DELETE:
                _, state = yield from self._ask(
                    lambda: self._client.delete_event(object_id, **account)
                )
                outcome = Applied()
            else:
                event = _event(cast(bytes, edit.content))
                _, state = yield from self._ask(
                    lambda: self._client.update_event(object_id, event, **account)
                )
                outcome = Applied()
        except JMAPMethodError as exc:
            refused = _refusal(edit.kind, exc)
            if refused is None:
                raise
            # A refused write changes nothing, so the state stays where it was.
            return refused
        self._state = state
        return outcome


@check_argument_types
class JMAPAdapter(_JMAPFlow):
    """Syncs one JMAP account's calendar events with a :class:`~calendaring_sync.replica.Replica`.

    The client is a calendaring-jmap ``JMAPClient``, and the account's
    ``CalendarEvent`` records are one :term:`scope`. Your application calls
    :meth:`sync` when it decides to sync, and records its edits with
    :meth:`create` and :meth:`change`, which build an event's fingerprint,
    calendars and logical key the same way a sync does. Deletes and
    discards need nothing from the content, so they go to
    :meth:`~calendaring_sync.replica.Replica.delete` and
    :meth:`~calendaring_sync.replica.Replica.discard`.

    Every write carries ``ifInState`` with the state the sync last saw, and
    that state covers every event in the account. When the server refuses a
    write because the state moved, the remaining edits wait, unsent, for
    the next sync.

    Every method also raises the store's errors, as
    :class:`~calendaring_sync.replica.Replica` describes.

    Args:
        replica: Where the sync state is kept.
        client: The client of the JMAP server.
        account_id: The account whose events to sync, such as the one the
            client's ``get_account_id()`` returns, or a shared account's id.
        scope_id: The scope's id. Defaults to the client's session URL,
            without a login and spelled one way, then ``#``, the account id
            and ``/CalendarEvent``, so accounts on two servers never share
            a scope.

    Raises:
        TypeError: If an argument has the wrong type, such as an
            asynchronous client.

    Examples:
        A sync, an edit, and the sync that pushes it::

            adapter = JMAPAdapter(replica, client, account_id=client.get_account_id())
            adapter.sync()
            key = adapter.create(event_json_bytes)
            adapter.sync()
    """

    def __init__(
        self,
        replica: Replica,
        client: JMAPClient,
        *,
        account_id: str,
        scope_id: str | None = None,
    ) -> None:
        super().__init__(replica, client, account_id, scope_id)

    def sync(self, *, full_listing: bool = False) -> None:
        """Sync the account's events: ingest what changed on the server, then push your edits.

        Changes since the scope's token come from ``CalendarEvent/changes``,
        a page at a time, and a scope that has no token is listed in full
        instead. When the server refuses the token, the scope is
        :term:`reset` and listed in full. Your edits are pushed only once
        everything the server reported was ingested. Each step runs in a
        store transaction of its own.

        Args:
            full_listing: List every event instead of asking for changes.
                JMAP has no version per event, so every event is fetched.

        Raises:
            ScopeGoneError: If the server has no such account.
            StaleChangeSetError: If another process ingested the scope first.
            SessionError: If this replica already has a session open for the scope.
            ScopeMismatchError: If the scope's content has another media type.
            TransactionError: If a store transaction is open in this thread.
            calendaring_jmap.JMAPError: For any other failure calendaring-jmap
                reports, such as a refused login, unchanged. A push it
                interrupted is settled by the next sync.
        """
        _run(self._sync(full_listing=full_listing))

    def create(self, content: bytes) -> int:
        """Record a create of an event with this JSCalendar content, and return its local key.

        The server picks the event's id when the next :meth:`sync` sends it.
        Runs in a write transaction.

        Raises:
            ValueError: If the content isn't UTF-8 JSON holding an event
                with a ``uid`` and at least one calendar in ``calendarIds``.
            UnknownScopeError: If the account was never synced.
            TransactionError: If a store transaction is open in this thread.
        """
        return _run(self._create(content))

    def change(self, local_key: int, content: bytes) -> None:
        """Record a change of an event to this JSCalendar content; the next :meth:`sync` sends it.

        Reads the record, then writes the change, each in a transaction of its own.

        Raises:
            ValueError: If the content isn't UTF-8 JSON holding an object,
                the record is in another scope, or the record is a create
                not sent yet and the content has no ``uid`` or no calendar.
            UnknownRecordError: If no record has this local key.
            EditRefusedError: If the record can't take an edit, such as one
                with an open conflict.
            TransactionError: If a store transaction is open in this thread.
        """
        _run(self._change(local_key, content))

    def waiting(self) -> list[Record]:
        """Return the pending edits the next :meth:`sync` won't send; none for JMAP.

        Every pending edit goes out with the next sync, unless the server
        rejects one and the rest wait for the sync after. Runs in a read
        transaction.

        Raises:
            UnknownScopeError: If the account was never synced.
            TransactionError: If a store transaction is open in this thread.
        """
        return _run(self._waiting())


@check_argument_types
class AsyncJMAPAdapter(_JMAPFlow):
    """:class:`JMAPAdapter` for an asynchronous client.

    :meth:`sync`, :meth:`create`, :meth:`change` and :meth:`waiting` are
    coroutines; each store call runs in a worker thread, so a long ingest
    doesn't block the event loop. Everything else is as
    :class:`JMAPAdapter` describes.

    Raises:
        TypeError: If an argument has the wrong type, such as a synchronous client.
    """

    def __init__(
        self,
        replica: Replica,
        client: AsyncJMAPClient,
        *,
        account_id: str,
        scope_id: str | None = None,
    ) -> None:
        super().__init__(replica, client, account_id, scope_id)

    async def sync(self, *, full_listing: bool = False) -> None:
        """See :meth:`JMAPAdapter.sync`."""
        await _arun(self._sync(full_listing=full_listing))

    async def create(self, content: bytes) -> int:
        """See :meth:`JMAPAdapter.create`."""
        return await _arun(self._create(content))

    async def change(self, local_key: int, content: bytes) -> None:
        """See :meth:`JMAPAdapter.change`."""
        await _arun(self._change(local_key, content))

    async def waiting(self) -> list[Record]:
        """See :meth:`JMAPAdapter.waiting`."""
        return await _arun(self._waiting())
