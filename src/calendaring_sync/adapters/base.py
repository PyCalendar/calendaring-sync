# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The base classes every :term:`adapter` is built on.

:class:`Adapter` holds the order of a sync, written once for every
protocol: open a :term:`session`, read and :term:`ingest` what changed,
then :term:`push` the pending edits. An adapter subclasses it, fills in
how its protocol reads and sends, and adds :class:`SyncAdapter` or
:class:`AsyncAdapter` for the public methods.

Both variants run the same steps. A step is a generator that yields what
a client call returned, the answer itself from a synchronous client or a
coroutine from an asynchronous one, or an :class:`InStore` for a call into
the :term:`store`, and gets each result sent back. :class:`SyncAdapter`
runs the steps as they come; :class:`AsyncAdapter` awaits the coroutines
and runs the store calls as its description says.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import enum
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from calendaring_sync._checks import check_argument_types
from calendaring_sync.error import OtherScopeError, ScopeGoneError
from calendaring_sync.objects.edit import EditFields
from calendaring_sync.objects.push import PushOutcome, Rejected
from calendaring_sync.objects.record import Record, RecordState
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica
from calendaring_sync.session import Session

_T = TypeVar("_T")

# The records whose edit is waiting to be sent.
_PENDING = frozenset({RecordState.PENDING})


def _run(steps: Steps[_T]) -> _T:
    """Drive the steps of a sync for a synchronous client."""
    sent: Any = None
    failure: BaseException | None = None
    while True:
        try:
            step = steps.send(sent) if failure is None else steps.throw(failure)
        except StopIteration as done:
            return done.value  # type: ignore[no-any-return]
        sent, failure = None, None
        try:
            sent = step.run() if isinstance(step, InStore) else step
        except BaseException as exc:  # noqa: BLE001, thrown back into the steps
            failure = exc


async def _arun(steps: Steps[_T]) -> _T:
    """Drive the steps of a sync for an asynchronous client.

    A store call runs to its end in its worker thread before the steps hear
    of a cancellation or of the driver being closed, so a session never ends
    while one of its calls is still running.
    """
    sent: Any = None
    failure: BaseException | None = None
    while True:
        try:
            step = steps.send(sent) if failure is None else steps.throw(failure)
        except StopIteration as done:
            return done.value  # type: ignore[no-any-return]
        sent, failure = None, None
        call: concurrent.futures.Future[Any] | None = None
        try:
            if isinstance(step, InStore):
                call = _in_thread(step)
                running = asyncio.wrap_future(call)
                try:
                    sent = await asyncio.shield(running)
                except asyncio.CancelledError:
                    await asyncio.wait([running])
                    raise
            else:
                sent = await step
        except GeneratorExit:
            # Nothing can be awaited while closing, so the call is waited for here.
            if call is not None:
                _hand_over(steps, call)
            steps.close()
            raise
        except BaseException as exc:  # noqa: BLE001, thrown back into the steps
            failure = exc


def _in_thread(step: InStore) -> concurrent.futures.Future[Any]:
    """Start a store call in a worker thread of the running loop."""
    result: concurrent.futures.Future[Any] = concurrent.futures.Future()
    context = contextvars.copy_context()

    def run() -> None:
        try:
            result.set_result(context.run(step.run))
        except BaseException as exc:  # noqa: BLE001, kept for the driver
            result.set_exception(exc)

    asyncio.get_running_loop().run_in_executor(None, run)
    return result


def _hand_over(steps: Steps[Any], call: concurrent.futures.Future[Any]) -> None:
    """Give the steps what a store call they were waiting for ended with, when closing.

    The steps then know whether, for example, a session was entered and has
    to end. Whatever they do next goes nowhere, since the driver is closing.
    """
    try:
        try:
            outcome = call.result()
        except BaseException as exc:  # noqa: BLE001, thrown into the steps
            following = steps.throw(exc)
        else:
            following = steps.send(outcome)
    except BaseException:  # noqa: BLE001, nobody is left to receive it
        return
    if inspect.iscoroutine(following):
        following.close()


def _end(session_manager: Any, exc_info: tuple[Any, Any, Any]) -> Steps[None]:
    """End a session as a store step, or right away when the driver closes first.

    A closing driver drops the step that follows the call it waited for, and
    that step can be this one.
    """
    try:
        yield InStore(session_manager.__exit__, exc_info)
    except GeneratorExit:
        session_manager.__exit__(*exc_info)
        raise


def _url_id(url: str) -> str:
    """A server URL in one spelling, for a default scope id.

    Scheme and host are case-insensitive (RFC 3986 section 6.2.2.1), so
    they're lowercased, and a login is left out, so it never reaches a
    store. An explicit port is kept as a number, even the scheme's default.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=netloc))


@check_argument_types
class Adapter(ABC):
    """Syncs one :term:`scope` with a :class:`~calendaring_sync.replica.Replica`, for any protocol.

    A sync opens a :term:`session`, reads what changed on the server and
    ingests it, then pushes the pending edits one at a time: claimed, sent,
    settled. When the server refuses the scope's :term:`sync token`, the
    scope is :term:`reset` and listed in full in a new session. An adapter
    subclasses it together with :class:`SyncAdapter` or
    :class:`AsyncAdapter`, which add the public methods.

    A subclass sets these class attributes, which open each session:

    - ``_fingerprint_scheme``: the name of the adapter's :term:`fingerprint scheme`.
    - ``_media_type``: the media type of the scope's :term:`content`.
    - ``_guarded_by_version``: ``True`` if a push applies only while the
      object still has the :term:`version` the edit was based on.
    - ``_unique_logical_key``: ``True`` if no two objects in the scope can
      share a :term:`logical key`.
    - ``_guard_covers_scope``: ``True`` if the server checks a push against
      the whole scope rather than the object, so after one rejection every
      later push would be rejected too, and they wait for the next sync.

    It writes these methods for its protocol, each a generator of
    :data:`Steps` except :meth:`_waits`, :meth:`_new_object_id` and
    :meth:`_edit_fields`:

    - :meth:`_read` fetches what changed and ingests it.
    - :meth:`_send` sends one claimed edit.
    - :meth:`_waits` says whether a pending edit can't be sent yet.
    - :meth:`_new_object_id` picks the :term:`object id` of a create.
    - :meth:`_edit_fields` reads an edit's fields from its content.

    It may also write :meth:`_gone`, which says whether an error from the
    server means the scope is gone; a request made through :meth:`_ask`
    then raises :class:`~calendaring_sync.error.ScopeGoneError` for it.

    These names start with an underscore because your application calls
    the adapter's public methods: a read or a send called on its own would
    skip the session that orders them.

    Args:
        replica: Where the sync state is kept.
        scope_id: The scope's id.
    """

    _fingerprint_scheme: str
    _media_type: str
    _guarded_by_version: bool
    _unique_logical_key: bool
    _guard_covers_scope: bool

    def __init__(self, replica: Replica, scope_id: str) -> None:
        self._replica = replica
        self._scope_id = scope_id

    @property
    def scope_id(self) -> str:
        """The :term:`scope` this adapter syncs."""
        return self._scope_id

    @property
    def needs_full_listings(self) -> bool:
        """Whether the server can lose changes without refusing the scope's token.

        If it's ``True``, an incremental sync may miss a change or a
        removal, so schedule a sync with ``full_listing=True`` now and then,
        such as once a day. ``False`` by default; an adapter whose server
        can lose changes says so.
        """
        return False

    def edit_fields(self, content: bytes) -> EditFields:
        """Read an object's fields from its content, the same way a sync reads the server's copy.

        Use it for content your application made itself, such as a
        :class:`~calendaring_sync.objects.resolution.Merge`. Reads nothing
        from the store or the server.

        Raises:
            ValueError: If the content can't be an object of the scope, as
                for a create.
        """
        return self._edit_fields(content, create=True)

    def _create(self, content: bytes) -> Steps[int]:
        """Record a create of an object with this content, and return its local key."""
        fields = self._edit_fields(content, create=True)
        key: int = yield InStore(
            self._replica.create,
            (self._scope_id, content),
            {**vars(fields), "object_id": self._new_object_id()},
        )
        return key

    def _change(self, local_key: int, content: bytes) -> Steps[None]:
        """Record a change of a record in the scope to this content.

        A change to a create that wasn't sent stays a create, so its content
        must pass what a create's does.
        """
        fields = self._edit_fields(content, create=False)
        # A record never changes scope or stops being a create before it's
        # sent, so these hold when the change runs.
        record: Record | None = yield InStore(self._replica.record, (local_key,))
        if record is not None and record.scope != self._scope_id:
            raise OtherScopeError(f"Record {local_key} is in scope {record.scope!r}, not this one")
        if record is not None and record.is_create:
            fields = self._edit_fields(content, create=True)
        yield InStore(self._replica.change, (local_key, content), vars(fields))

    def _waiting(self) -> Steps[list[Record]]:
        """The pending edits the adapter won't send yet."""
        pending: list[Record] = yield InStore(
            self._replica.records, (self._scope_id,), {"states": _PENDING}
        )
        return [r for r in pending if self._waits(r)]

    def _sync(self, *, full_listing: bool) -> Steps[None]:
        """One sync: read and ingest, then push; a refused token resets and lists."""
        read = yield from self._in_session(
            lambda session: self._sync_session(session, full_listing=full_listing)
        )
        if read is ReadResult.INVALID_TOKEN:
            yield InStore(self._replica.reset, (self._scope_id,))
            yield from self._in_session(
                lambda session: self._sync_session(session, full_listing=True)
            )

    def _sync_session(self, session: Session, *, full_listing: bool) -> Steps[ReadResult]:
        scope: Scope = yield InStore(self._replica.scope, (self._scope_id,))
        read = yield from self._read(session, scope, full_listing=full_listing)
        if read is ReadResult.COMPLETE:
            yield from self._push(session)
        return read

    def _in_session(self, steps: Callable[[Session], Steps[_T]]) -> Steps[_T]:
        """Run ``steps`` inside a session of the scope, which ends whatever happens."""
        session_manager = self._replica.session(
            self._scope_id,
            fingerprint_scheme=self._fingerprint_scheme,
            media_type=self._media_type,
            guarded_by_version=self._guarded_by_version,
            unique_logical_key=self._unique_logical_key,
        )
        session: Session = yield InStore(session_manager.__enter__)
        try:
            result = yield from steps(session)
        except GeneratorExit:
            # A closing driver runs no more steps, so the session ends here.
            session_manager.__exit__(None, None, None)
            raise
        except BaseException as exc:
            yield from _end(session_manager, (type(exc), exc, exc.__traceback__))
            raise
        yield from _end(session_manager, (None, None, None))
        return result

    def _push(self, session: Session) -> Steps[None]:
        """Push every pending edit, each claimed, sent and settled on its own.

        Where the server checks a push against the whole scope, the pushes
        stop at the first rejection, and the rest stay pending, unclaimed.
        """
        records: list[Record] = yield InStore(session.to_push)
        for record in records:
            if self._waits(record):
                continue
            object_id = self._new_object_id() if record.object_id is None else None
            claimed: Record | None = yield InStore(
                session.claim, (record.local_key,), {"object_id": object_id}
            )
            if claimed is None:
                continue
            outcome = yield from self._send(claimed)
            yield InStore(session.settle, (claimed, outcome))
            if self._guard_covers_scope and isinstance(outcome, Rejected):
                return

    def _ask(self, request: Callable[[], Any]) -> Steps[Any]:
        """Make one request about the scope, and return the server's answer.

        ``request`` is a function making the call, so a synchronous client's
        error is raised inside this step too.

        Raises:
            ScopeGoneError: If :meth:`_gone` says the request's error means
                the server no longer has the scope.
        """
        try:
            return (yield request())
        except Exception as exc:
            gone = self._gone(exc)
            if gone is None:
                raise
            raise ScopeGoneError(gone) from exc

    def _gone(self, error: Exception) -> str | None:
        """Say why the scope is gone, if ``error`` means the server no longer has it.

        Returns:
            The :class:`~calendaring_sync.error.ScopeGoneError` message, or
            ``None`` for any other error, which is raised unchanged. The
            base class returns ``None``.
        """
        return None

    @abstractmethod
    def _read(self, session: Session, scope: Scope, *, full_listing: bool) -> Steps[ReadResult]:
        """Fetch what changed in the scope, ingest it, and say how the read ended.

        Without a token in ``scope.token``, or with ``full_listing``, list
        every object; otherwise ask for the changes since the token. Ingest
        each :class:`~calendaring_sync.objects.change_set.ChangeSet` with
        ``yield InStore(session.ingest, (change_set,))``.

        Args:
            session: The open session to ingest into.
            scope: The scope as the store holds it.
            full_listing: Whether the caller asked for a full listing.

        Returns:
            :attr:`ReadResult.INVALID_TOKEN` if the server refused the
            token, before anything was ingested; :attr:`ReadResult.TRUNCATED`
            if the server cut the changes short; :attr:`ReadResult.COMPLETE`
            otherwise.
        """

    @abstractmethod
    def _send(self, claimed: Record) -> Steps[PushOutcome]:
        """Send one claimed edit and return how it ended.

        An error raised here reaches the caller and leaves the push in
        flight, since the request may have reached the server; the next
        complete ingest settles it.

        Args:
            claimed: The record :meth:`~calendaring_sync.session.Session.claim`
                returned, holding the edit to send.
        """

    @abstractmethod
    def _waits(self, record: Record) -> bool:
        """Whether a pending edit can't be sent yet, so it waits for a later sync.

        Args:
            record: A record with a pending edit.
        """

    @abstractmethod
    def _new_object_id(self) -> str | None:
        """Return an object id for a create, or ``None`` if the server picks it."""

    @abstractmethod
    def _edit_fields(self, content: bytes, *, create: bool) -> EditFields:
        """Read an edit's fields from its content, the same way a sync reads the server's copy.

        Args:
            content: The edit's content.
            create: Whether the edit is a create, which may need more of
                the content, such as a logical key.

        Raises:
            ValueError: If the content can't be an object of the scope.
        """


@check_argument_types
class SyncAdapter(Adapter):
    """The public methods of an adapter for a synchronous client.

    Your application calls :meth:`sync` when it decides to sync, and
    records its edits with :meth:`create` and :meth:`change`. Deletes and
    discards need nothing from the content, so they go to
    :meth:`~calendaring_sync.replica.Replica.delete` and
    :meth:`~calendaring_sync.replica.Replica.discard`. The adapter's class
    says what content it takes and which of its client's errors reach you;
    the next sync settles a push such an error interrupted. Every method
    also raises the store's errors, as
    :class:`~calendaring_sync.replica.Replica` describes.
    """

    def sync(self, *, full_listing: bool = False) -> None:
        """Sync the scope: ingest what changed on the server, then push your edits.

        A scope with no token is listed in full. When the server refuses the
        token, the scope is :term:`reset` and listed in full. Your edits are
        pushed only once everything the server reported was ingested. Each
        step runs in a store transaction of its own.

        Args:
            full_listing: List every object instead of asking for changes.

        Raises:
            ScopeGoneError: If the server no longer has the scope.
            IncompleteListingError: If the server cut a full listing short.
            StaleChangeSetError: If another process ingested the scope first.
            SessionError: If this replica already has a session open for the scope.
            ScopeMismatchError: If the scope's content has another media type.
            TransactionError: If a store transaction is open in this thread.
        """
        _run(self._sync(full_listing=full_listing))

    def create(self, content: bytes) -> int:
        """Record a create of an object with this content, and return its local key.

        The next :meth:`sync` sends it. Runs in a write transaction.

        Raises:
            ValueError: If the content can't be an object of the scope.
            UnknownScopeError: If the scope was never synced.
            TransactionError: If a store transaction is open in this thread.
        """
        return _run(self._create(content))

    def change(self, local_key: int, content: bytes) -> None:
        """Record a change of an object to this content; the next :meth:`sync` sends it.

        Reads the record, then writes the change, each in a transaction of
        its own. A change to a create that wasn't sent yet stays a create,
        so the content must pass what a create's does.

        Raises:
            ValueError: If the content can't be an object of the scope.
            OtherScopeError: If the record is in another scope.
            UnknownRecordError: If no record has this local key.
            EditRefusedError: If the record can't take an edit, such as one
                with an open conflict.
            TransactionError: If a store transaction is open in this thread.
        """
        _run(self._change(local_key, content))

    def waiting(self) -> list[Record]:
        """Return the pending edits the next :meth:`sync` won't send, because it can't yet.

        Runs in a read transaction.

        Raises:
            UnknownScopeError: If the scope was never synced.
            TransactionError: If a store transaction is open in this thread.
        """
        return _run(self._waiting())


@check_argument_types
class AsyncAdapter(Adapter):
    """The public methods of an adapter for an asynchronous client.

    The same as :class:`SyncAdapter`'s, as coroutines. Each store call runs
    in a worker thread, so a long ingest doesn't block the event loop.
    """

    async def sync(self, *, full_listing: bool = False) -> None:
        """See :meth:`~calendaring_sync.adapters.base.SyncAdapter.sync`."""
        await _arun(self._sync(full_listing=full_listing))

    async def create(self, content: bytes) -> int:
        """See :meth:`~calendaring_sync.adapters.base.SyncAdapter.create`."""
        return await _arun(self._create(content))

    async def change(self, local_key: int, content: bytes) -> None:
        """See :meth:`~calendaring_sync.adapters.base.SyncAdapter.change`."""
        await _arun(self._change(local_key, content))

    async def waiting(self) -> list[Record]:
        """See :meth:`~calendaring_sync.adapters.base.SyncAdapter.waiting`."""
        return await _arun(self._waiting())


#: The steps of a sync, as a generator that yields client answers or
#: :class:`InStore` calls, is sent back what each one returned, and returns
#: its result.
Steps = Generator[Any, Any, _T]


@dataclass(frozen=True)
class InStore:
    """A call into the store, which an asynchronous adapter runs in a worker thread.

    Attributes:
        call: The function to call, such as ``session.ingest``.
        args: Its positional arguments.
        kwargs: Its keyword arguments.

    Examples:
        A step that ingests a change set::

            yield InStore(session.ingest, (change_set,))
    """

    call: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def run(self) -> Any:
        """Make the call and return what it returned."""
        return self.call(*self.args, **self.kwargs)


class ReadResult(enum.Enum):
    """How the read part of a sync ended, which decides what the sync does next."""

    #: Every change was ingested, so the pushes go out.
    COMPLETE = "complete"
    #: The server cut the changes short. The pushes wait, and the next sync
    #: continues from the :term:`checkpoint`.
    TRUNCATED = "truncated"
    #: The server refused the scope's :term:`sync token`, so the scope is
    #: :term:`reset` and listed in full.
    INVALID_TOKEN = "invalid token"
