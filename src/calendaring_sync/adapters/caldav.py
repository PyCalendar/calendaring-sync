# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The CalDAV :term:`adapter`, which syncs one calendar collection through python-caldav.

Install it with the ``caldav`` extra: ``pip install 'calendaring-sync[caldav]'``.
:doc:`/explanation/adapters` explains every rule it follows, and
:doc:`/reference/server-compatibility` how each tested server behaves.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

try:
    import lxml.etree as etree
    from caldav.calendarobjectresource import CalendarObjectResource
    from caldav.collection import Calendar
    from caldav.davclient import DAVClient
    from caldav.elements import dav
    from caldav.lib import error as dav_error
    from caldav.lib.url import URL
    from icalendar.parser import Contentline, Contentlines
    from icalendar.prop import vText
except ImportError as exc:
    raise ImportError(
        "The CalDAV adapter needs the caldav extra: pip install 'calendaring-sync[caldav]'"
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
)
from calendaring_sync.error import IncompleteListingError, ScopeGoneError
from calendaring_sync.objects.change import Change, Changed, Removed, Unchanged
from calendaring_sync.objects.change_set import ChangeSet
from calendaring_sync.objects.edit import EditKind, LocalEdit
from calendaring_sync.objects.push import Applied, Gone, LogicalKeyTaken, PushOutcome, Rejected
from calendaring_sync.objects.record import Record
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica, Session

#: The name of the canonical fingerprint this adapter computes. A change to
#: how it's computed gets a new name, so every scope is listed again.
FINGERPRINT_SCHEME = "caldav-canonical-1"
#: The media type of a CalDAV scope's content: iCalendar data.
MEDIA_TYPE = "text/calendar"
#: How many objects one ``calendar-multiget`` fetches, and one page of a
#: :term:`full listing` holds.
MULTIGET_SIZE = 100

# Properties a server sets on its own (RFC 5545 sections 3.8.7.2, 3.8.7.3 and
# 3.7.3), left out of the fingerprint so a server's copy of your own write
# fingerprints the same.
_SERVER_SET = frozenset({"DTSTAMP", "LAST-MODIFIED", "PRODID"})
# Recurrence rules, whose parts may come in any order (RFC 5545 section 3.3.10).
_RECURRENCE_RULES = frozenset({"RRULE", "EXRULE"})
# What the canonical form's outermost level is called; calendar data has
# no component of that name.
_ROOT = ""
# Components that are no object of their own, so their UID isn't the logical key.
_NOT_AN_OBJECT = frozenset({_ROOT, "VCALENDAR", "VTIMEZONE"})
# Where an object's component sits: in a calendar, or outermost.
_HOLDS_OBJECTS = frozenset({_ROOT, "VCALENDAR"})
_SYNC_COLLECTION = "{DAV:}sync-collection"
_VALID_SYNC_TOKEN = "{DAV:}valid-sync-token"
_HREF = "{DAV:}href"
# How python-caldav's parse_preconditions reads an error body: no entities
# expanded, nothing fetched.
_ERROR_PARSER = {"resolve_entities": False, "no_network": True}
# The prefix of a weak entity tag (RFC 9110 section 8.8.3).
_WEAK = "W/"
# python-caldav's feature saying how a server refuses a sync token it won't take.
_TOKEN_REFUSAL_FEATURE = "sync-token.invalid-token"
# The status a server gives the collection when it cuts a listing short.
_TRUNCATED = 507
_NOT_FOUND = 404
# How python-caldav's server profiles describe a server whose refusal of a
# token can't be relied on, so its scopes need regular full listings.
_UNRELIABLE_TOKEN_REFUSAL = frozenset({"broken", "ungraceful"})


@dataclass(frozen=True)
class _Fields:
    """What the adapter reads from an object's content for the core."""

    fingerprint: str
    logical_key: str | None


def _fields(content: bytes) -> _Fields:
    """Read the fingerprint and the logical key from iCalendar data.

    The fingerprint is SHA-256 of a canonical form: content lines unfolded,
    components sorted, properties and parameters sorted within each, the
    parts of each recurrence rule sorted, the properties a server sets left
    out, and every other value as sent. The logical key is the ``UID`` of
    the first object: a component in the calendar other than a time zone,
    so not one inside an object, such as an alarm.

    Raises:
        ValueError: If the content isn't UTF-8 or isn't iCalendar data.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Calendar data must be UTF-8: {exc}") from exc
    # Each open component: its name, its properties and its finished children.
    stack: list[tuple[str, list[tuple[str, tuple[tuple[str, str], ...], str]], list[str]]] = [
        (_ROOT, [], [])
    ]
    uid: str | None = None
    for line in Contentlines.from_ical(text):
        if not line:
            continue
        name, params, value = Contentline(line).parts()
        name = name.upper()
        if name == "BEGIN":
            stack.append((value.upper(), [], []))
        elif name == "END":
            if len(stack) == 1:
                raise ValueError(f"Calendar data ends {value} that never began")
            component, properties, children = stack.pop()
            if value.upper() != component:
                raise ValueError(f"Calendar data ends {value} inside {component}")
            stack[-1][2].append(repr((component, sorted(properties), sorted(children))))
        elif name not in _SERVER_SET:
            in_object = stack[-1][0] not in _NOT_AN_OBJECT and stack[-2][0] in _HOLDS_OBJECTS
            if name == "UID" and uid is None and in_object:
                uid = str(vText.from_ical(value))
            if name in _RECURRENCE_RULES:
                value = ";".join(sorted(value.split(";")))
            parameters = tuple(sorted((key.upper(), str(item)) for key, item in params.items()))
            stack[-1][1].append((name, parameters, value))
    if len(stack) != 1 or not stack[0][2]:
        raise ValueError("Calendar data must hold whole components")
    canonical = repr(sorted(stack[0][2]))
    return _Fields(hashlib.sha256(canonical.encode()).hexdigest(), uid)


def _server_fields(content: bytes) -> _Fields:
    """The fields of content the server sent, which a malformed object mustn't stop.

    Data the canonical form can't read is fingerprinted as it came, so it
    still syncs, and only differs from what you sent where a rewrite would.
    """
    try:
        return _fields(content)
    except ValueError:
        return _Fields(hashlib.sha256(content).hexdigest(), None)


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class _CalDAVFlow(_Flow):
    """Everything the sync and async CalDAV adapters share."""

    _fingerprint_scheme = FINGERPRINT_SCHEME
    _media_type = MEDIA_TYPE
    # A tested server accepts two objects with one UID, and every write is
    # guarded by the object's ETag.
    _guarded_by_version = True
    _unique_logical_key = False
    # Whether the adapter takes a calendar on an asynchronous client.
    _asynchronous: bool

    def __init__(self, replica: Replica, calendar: Calendar, scope_id: str | None) -> None:
        if calendar.url is None or calendar.client is None:
            raise ValueError("The calendar has no URL or no client")
        if calendar.is_async_client is not self._asynchronous:
            wanted = "an asynchronous" if self._asynchronous else "a synchronous"
            raise TypeError(f"{type(self).__name__} takes a calendar on {wanted} client")
        self._calendar = calendar
        self._url: URL = calendar.url
        self._client: DAVClient = calendar.client
        super().__init__(replica, self._calendar_id() if scope_id is None else scope_id)
        self._collection = self._object_id(calendar.url)
        self._containers = frozenset({self.scope_id})
        # Whether the collection supports sync-collection; read before its first sync.
        self._reports_changes: bool | None = None

    @property
    def needs_full_listings(self) -> bool:
        """Whether the server may leave changes out without refusing the scope's token.

        Read from the server profile configured on the client, python-caldav's
        ``sync-token.invalid-token`` feature. When it's true, schedule regular
        full listings with ``sync(full_listing=True)``: a listing only fetches
        objects whose ETag changed, so it costs little more than a sync.
        """
        features = self._client.features
        if features is None:
            return False
        # python-caldav leaves return_type unannotated, so its default types it as bool.
        support = features.is_supported(
            _TOKEN_REFUSAL_FEATURE,
            return_type=str,  # pyright: ignore[reportArgumentType]
        )
        return support in _UNRELIABLE_TOKEN_REFUSAL

    def _calendar_id(self) -> str:
        """The calendar's URL in one spelling, without a login, as the default scope id."""
        parts = urlsplit(str(self._url.canonical()))
        # Scheme and host are case-insensitive (RFC 3986 section 6.2.2.1).
        return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()))

    def _object_id(self, href: Any) -> str:
        """The object id of an href, in one spelling however the server or client wrote it."""
        return str(self._url.join(href).canonical().path)

    def _new_object_id(self) -> str:
        # Only unreserved characters (RFC 3986 section 2.3), so it can't come
        # back spelled another way.
        return self._object_id(f"{uuid.uuid4()}.ics")

    def _edit_fields(self, content: bytes, *, create: bool) -> _EditFields:
        fields = _fields(content)
        if create and fields.logical_key is None:
            raise ValueError("Calendar data must hold an object with a UID")
        return {
            "fingerprint": fields.fingerprint,
            "containers": self._containers,
            "logical_key": fields.logical_key,
        }

    def _ask(self, request: Any) -> Steps[Any]:
        """Send one request about the collection; a collection that's gone raises.

        ``request`` is a function making the call, so a synchronous client's
        error is raised here too.
        """
        try:
            return (yield request())
        except dav_error.NotFoundError as exc:
            raise ScopeGoneError(f"The server has no calendar at {self._url}") from exc

    def _read(self, session: Session, scope: Scope, *, full_listing: bool) -> Steps[_Read]:
        # A collection that names no token (one that supports the report
        # must, RFC 6578 section 4) is listed every time too.
        if full_listing or not scope.token or not (yield from self._supports_changes()):
            yield from self._list(session, scope)
            return _Read.COMPLETE
        try:
            found = yield from self._ask(
                lambda: self._calendar.get_objects_by_sync_token(scope.token, disable_fallback=True)
            )
        except dav_error.DAVError as exc:
            if _VALID_SYNC_TOKEN in exc.preconditions:
                return _Read.INVALID_TOKEN
            raise
        removed = {self._object_id(url) for url in found.deleted_urls}
        changes: dict[str, Change] = dict.fromkeys(removed, Removed())
        known: dict[str, str] = yield _InStore(self._replica.known_versions, (self.scope_id,))
        # A member reported with the ETag already stored hasn't changed since,
        # such as one this sync already fetched or that came back twice.
        reported = ((self._object_id(obj.url), obj.etag) for obj in found.objects)
        changed = [
            object_id
            for object_id, version in reported
            if object_id not in removed and (version is None or known.get(object_id) != version)
        ]
        changes.update((yield from self._fetch(changed)))
        # A reply without a token keeps the scope's, so nothing in it is lost:
        # the next sync asks again from where this one started.
        token = scope.token if found.sync_token is None else found.sync_token
        change_set = ChangeSet(
            self.scope_id, scope.token, token, changes, truncated=found.truncated
        )
        yield _InStore(session.ingest, (change_set,))
        return _Read.TRUNCATED if found.truncated else _Read.COMPLETE

    def _supports_changes(self) -> Steps[bool]:
        """Whether the collection lists ``sync-collection`` in its supported reports."""
        if self._reports_changes is None:
            props = yield from self._ask(
                lambda: self._calendar.get_properties([dav.SupportedReportSet()])
            )
            self._reports_changes = _SYNC_COLLECTION in (
                props.get(dav.SupportedReportSet.tag) or ()
            )
        return self._reports_changes

    def _list(self, session: Session, scope: Scope) -> Steps[None]:
        """A full listing: the token first, then every member and its ETag."""
        props = yield from self._ask(lambda: self._calendar.get_properties([dav.SyncToken()]))
        token = props.get(dav.SyncToken.tag) or ""
        try:
            response = yield from self._ask(
                lambda: self._calendar.get_properties(
                    [dav.GetEtag()], depth=1, parse_response_xml=False
                )
            )
        except dav_error.ResponseError as exc:
            # python-caldav refuses a multistatus that marks the cut with a 507,
            # and gives the status line, not a status.
            if f" {_TRUNCATED} " not in f" {exc.url} ":
                raise
            raise IncompleteListingError(
                f"The server cut the listing of {self._url} short"
            ) from exc
        versions = {
            self._object_id(entry.href): entry.properties.get(dav.GetEtag.tag)
            for entry in response.parse_propfind()
        }
        known: dict[str, str] = yield _InStore(self._replica.known_versions, (self.scope_id,))
        pages = list(_chunks(list(versions), MULTIGET_SIZE)) or [()]
        for number, page in enumerate(pages, start=1):
            changes: dict[str, Change] = {
                i: Unchanged()
                for i in page
                if versions[i] is not None and known.get(i) == versions[i]
            }
            changes.update((yield from self._fetch(i for i in page if i not in changes)))
            change_set = ChangeSet(
                self.scope_id,
                scope.token,
                token,
                changes,
                full_listing=True,
                truncated=number < len(pages),
            )
            yield _InStore(session.ingest, (change_set,))

    def _fetch(self, object_ids: Iterable[str]) -> Steps[dict[str, Changed]]:
        """Fetch objects' content and ETags together, a multiget at a time.

        An object missing from the answer, or answered without data, was
        removed after it was reported; the next report carries its removal.
        One answered without an ETag is kept with no version, so the next
        listing fetches it again.
        """
        wanted = [i for i in object_ids if i != self._collection]
        fetched: dict[str, Changed] = {}
        for chunk in _chunks(wanted, MULTIGET_SIZE):
            urls = [self._url.join(i) for i in chunk]
            found = yield from self._ask(lambda urls=urls: self._calendar.multiget(urls))
            for obj in found:
                content = obj.raw_data
                if content is None:
                    continue
                fields = _server_fields(content)
                fetched[self._object_id(obj.url)] = Changed(
                    obj.etag, fields.fingerprint, content, self._containers, fields.logical_key
                )
        return fetched

    def _waits(self, record: Record) -> bool:
        # If-Match compares strongly, so a weak ETag never matches (RFC 9110
        # section 13.1.1); python-caldav would send the write unconditionally.
        return not record.is_create and (record.version or "").startswith(_WEAK)

    def _send(self, claimed: Record) -> Steps[PushOutcome]:
        # A claimed record holds its edit, and a claim gives every create its href.
        edit = cast(LocalEdit, claimed.edit)
        props = {dav.GetEtag.tag: claimed.version} if claimed.version else {}
        resource = CalendarObjectResource(
            client=self._client,
            url=self._url.join(cast(str, claimed.object_id)),
            parent=self._calendar,
            props=props,
        )
        if edit.kind is EditKind.CREATE:
            try:
                yield resource.save(no_overwrite=True, raw_data=edit.content, precondition="etag")
            except dav_error.UidConflictError as exc:
                return LogicalKeyTaken(self._holder(exc))
            except dav_error.ConsistencyError:
                return Rejected()
            return Applied()
        if edit.kind is EditKind.DELETE:
            try:
                yield resource.delete(if_match=True, precondition="etag")
            except dav_error.NotFoundError:
                return Gone()
            except dav_error.ETagMismatchError:
                return Rejected()
            return Applied()
        try:
            yield resource.save(raw_data=edit.content, precondition="etag", increase_seqno=False)
        except (
            dav_error.UidConflictError,
            dav_error.ETagMismatchError,
            dav_error.ScheduleTagMismatchError,
        ):
            return Rejected()
        except dav_error.PutError as exc:
            if exc.status == _NOT_FOUND:
                return Gone()
            raise
        return Applied()

    def _holder(self, refusal: Any) -> str | None:
        """The object id of the object holding a refused create's UID, if the server named it.

        The precondition element may hold its ``DAV:href`` (:rfc:`4791#section-5.3.2.1`),
        read with the parser settings python-caldav found the precondition
        with, so it parses. Naming the holder is a help, never a reason to
        fail the push:
        a body or href this calendar can't place, such as one in another
        calendar or on another host, names no holder, and the core finds it
        by the UID instead.
        """
        body = refusal.body.encode() if isinstance(refusal.body, str) else refusal.body
        if not body:
            return None
        root = etree.fromstring(body, parser=etree.XMLParser(**_ERROR_PARSER))
        for tag in dav_error.UID_CONFLICT_PRECONDITIONS:
            for element in root.iter(tag):
                href = element.find(_HREF)
                if href is not None and href.text:
                    try:
                        holder = self._object_id(href.text.strip())
                    except ValueError:  # on another host than the calendar
                        return None
                    in_calendar = holder.rpartition("/")[0] == self._collection.rstrip("/")
                    return holder if in_calendar else None
        return None


@check_argument_types
class CalDAVAdapter(_CalDAVFlow):
    """Syncs one calendar collection with a :class:`~calendaring_sync.replica.Replica`.

    The calendar is a python-caldav ``Calendar`` on a synchronous client,
    and it's one :term:`scope`. Your application calls :meth:`sync` when it
    decides to sync, and records its edits with :meth:`create` and
    :meth:`change`, which build an object's fingerprint and logical key the
    same way a sync does. Deletes and discards need nothing from the
    content, so they go to :meth:`~calendaring_sync.replica.Replica.delete`
    and :meth:`~calendaring_sync.replica.Replica.discard`.

    With no server profile on the client, python-caldav searches the
    calendar for a create's ``UID`` before sending it. Configuring the
    client's ``features`` (``features="radicale"``, for example) lets the
    server check instead, in the same request.

    A change or delete of an object whose stored ETag is weak (``W/``)
    waits, unsent, until a sync stores a strong one, since ``If-Match``
    never matches a weak ETag. Such a record stays pending with a
    ``version`` that starts with ``W/``.

    Every method also raises the store's errors, as
    :class:`~calendaring_sync.replica.Replica` describes.

    Args:
        replica: Where the sync state is kept.
        calendar: The calendar collection to sync.
        scope_id: The scope's id. Defaults to the calendar's URL, without
            a login and spelled one way, so calendars on two servers never
            share a scope and one calendar always gets the same one.

    Raises:
        TypeError: If the calendar is on an asynchronous client, or an
            argument has the wrong type.
        ValueError: If the calendar has no URL or no client.

    Examples:
        A sync, an edit, and the sync that pushes it::

            adapter = CalDAVAdapter(replica, calendar)
            adapter.sync()
            key = adapter.create(ics_bytes)
            adapter.sync()
    """

    _asynchronous = False

    def __init__(
        self, replica: Replica, calendar: Calendar, *, scope_id: str | None = None
    ) -> None:
        super().__init__(replica, calendar, scope_id)

    def sync(self, *, full_listing: bool = False) -> None:
        """Sync the calendar: ingest what changed on the server, then push your edits.

        Changes since the scope's token come from a ``sync-collection``
        report, and a calendar that has none, or a scope that has no token,
        is listed in full instead. When the server refuses the token, the
        scope is :term:`reset` and listed in full. Your edits are pushed only
        once everything the server reported was ingested. Each step runs in
        a store transaction of its own.

        Args:
            full_listing: List every object instead of asking for changes.
                Members whose ETag didn't change aren't fetched.

        Raises:
            ScopeGoneError: If the server has no calendar at its URL.
            IncompleteListingError: If the server cut a full listing short.
            StaleChangeSetError: If another process ingested the scope first.
            SessionError: If this replica already has a session open for the scope.
            ScopeMismatchError: If the scope's content has another media type.
            TransactionError: If a store transaction is open in this thread.
            caldav.lib.error.DAVError: For any other failure python-caldav
                reports, such as a refused login, unchanged. A push it
                interrupted is settled by the next sync.
        """
        _run(self._sync(full_listing=full_listing))

    def create(self, content: bytes) -> int:
        """Record a create of an object with this iCalendar data, and return its local key.

        The object gets an href of its own; the next :meth:`sync` sends it.
        Runs in a write transaction.

        Raises:
            ValueError: If the content isn't UTF-8 iCalendar data with a ``UID``.
            UnknownScopeError: If the calendar was never synced.
            TransactionError: If a store transaction is open in this thread.
        """
        return _run(self._create(content))

    def change(self, local_key: int, content: bytes) -> None:
        """Record a change of an object to this iCalendar data; the next :meth:`sync` sends it.

        Reads the record, then writes the change, each in a transaction of its own.

        Raises:
            ValueError: If the content isn't UTF-8 iCalendar data, or the
                record is in another scope.
            UnknownRecordError: If no record has this local key.
            EditRefusedError: If the record can't take an edit, such as one
                with an open conflict.
            TransactionError: If a store transaction is open in this thread.
        """
        _run(self._change(local_key, content))


@check_argument_types
class AsyncCalDAVAdapter(_CalDAVFlow):
    """:class:`CalDAVAdapter` for a calendar on an asynchronous client.

    :meth:`sync`, :meth:`create` and :meth:`change` are coroutines; each
    store call runs in a worker thread, so a long ingest doesn't block the
    event loop. Everything else is as :class:`CalDAVAdapter` describes.

    Raises:
        TypeError: If the calendar is on a synchronous client, or an
            argument has the wrong type.
        ValueError: If the calendar has no URL or no client.
    """

    _asynchronous = True

    def __init__(
        self, replica: Replica, calendar: Calendar, *, scope_id: str | None = None
    ) -> None:
        super().__init__(replica, calendar, scope_id)

    async def sync(self, *, full_listing: bool = False) -> None:
        """See :meth:`CalDAVAdapter.sync`."""
        await _arun(self._sync(full_listing=full_listing))

    async def create(self, content: bytes) -> int:
        """See :meth:`CalDAVAdapter.create`."""
        return await _arun(self._create(content))

    async def change(self, local_key: int, content: bytes) -> None:
        """See :meth:`CalDAVAdapter.change`."""
        await _arun(self._change(local_key, content))
