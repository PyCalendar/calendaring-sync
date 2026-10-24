# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One sync of one scope, written once for every protocol and for sync and async callers.

A sync is a generator. Each step yields what a client call returned, which
is the answer itself for a synchronous client and a coroutine for an
asynchronous one, or an :class:`_InStore` for a call into the
:term:`store`. :func:`_run` drives the generator for synchronous callers;
:func:`_arun` awaits the coroutines and runs each store call in a worker
thread, so a long ingest doesn't block the event loop. An adapter
subclasses :class:`_Flow` and writes its protocol's steps as generators
too; the order of a sync, the session and the pushes live here.
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
from typing import Any, TypedDict, TypeVar

from calendaring_sync.objects.push import PushOutcome
from calendaring_sync.objects.record import Record
from calendaring_sync.objects.scope import Scope
from calendaring_sync.replica import Replica, Session

_T = TypeVar("_T")

#: The steps of a sync: they yield client answers or :class:`_InStore`
#: calls, and are sent back what each one returned.
Steps = Generator[Any, Any, _T]


@dataclass(frozen=True)
class _InStore:
    """A call into the store, which an asynchronous driver runs in a worker thread."""

    call: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def run(self) -> Any:
        return self.call(*self.args, **self.kwargs)


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
            sent = step.run() if isinstance(step, _InStore) else step
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
            if isinstance(step, _InStore):
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


def _in_thread(step: _InStore) -> concurrent.futures.Future[Any]:
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
        yield _InStore(session_manager.__exit__, exc_info)
    except GeneratorExit:
        session_manager.__exit__(*exc_info)
        raise


class _EditFields(TypedDict):
    """What an edit records beside its content, read from the content as a sync reads it."""

    fingerprint: str
    containers: frozenset[str]
    logical_key: str | None


class _Read(enum.Enum):
    """How the read part of a sync ended."""

    #: Every change was ingested, so the pushes can go out.
    COMPLETE = "complete"
    #: The server cut the changes short; the next sync continues from the checkpoint.
    TRUNCATED = "truncated"
    #: The server refused the scope's token, so the scope needs a full listing.
    INVALID_TOKEN = "invalid token"


class _Flow(ABC):
    """The order of one sync, the same for every protocol.

    A sync opens a :term:`session`, reads what changed and ingests it, then
    pushes the pending edits. When the server refuses the scope's token,
    the scope is :term:`reset` and listed in a new session. Recording an
    edit reads its fields from the content as a sync would. A subclass
    sets the session options and writes :meth:`_read`, :meth:`_send`,
    :meth:`_waits`, :meth:`_new_object_id` and :meth:`_edit_fields` for
    its protocol.
    """

    #: The name of the adapter's fingerprint scheme.
    _fingerprint_scheme: str
    #: The media type of the scope's content.
    _media_type: str
    #: Whether a push only applies if the object still has the version it was based on.
    _guarded_by_version: bool
    #: Whether no two objects in the scope can share a logical key.
    _unique_logical_key: bool

    def __init__(self, replica: Replica, scope_id: str) -> None:
        self._replica = replica
        self._scope_id = scope_id

    @property
    def scope_id(self) -> str:
        """The :term:`scope` this adapter syncs."""
        return self._scope_id

    def _create(self, content: bytes) -> Steps[int]:
        """Record a create of an object with this content, and return its local key."""
        fields = self._edit_fields(content, create=True)
        key: int = yield _InStore(
            self._replica.create,
            (self._scope_id, content),
            {**fields, "object_id": self._new_object_id()},
        )
        return key

    def _change(self, local_key: int, content: bytes) -> Steps[None]:
        """Record a change of a record in the scope to this content."""
        fields = self._edit_fields(content, create=False)
        # A record never changes scope, so this holds when the change runs.
        record: Record | None = yield _InStore(self._replica.record, (local_key,))
        if record is not None and record.scope != self._scope_id:
            raise ValueError(f"Record {local_key} is in scope {record.scope!r}, not this one")
        yield _InStore(self._replica.change, (local_key, content), fields)

    def _sync(self, *, full_listing: bool) -> Steps[None]:
        """One sync: read and ingest, then push; a refused token resets and lists."""
        read = yield from self._in_session(
            lambda session: self._sync_session(session, full_listing)
        )
        if read is _Read.INVALID_TOKEN:
            yield _InStore(self._replica.reset, (self._scope_id,))
            yield from self._in_session(lambda session: self._sync_session(session, True))

    def _sync_session(self, session: Session, full_listing: bool) -> Steps[_Read]:
        scope: Scope = yield _InStore(self._replica.scope, (self._scope_id,))
        read = yield from self._read(session, scope, full_listing=full_listing)
        if read is _Read.COMPLETE:
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
        session: Session = yield _InStore(session_manager.__enter__)
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
        """Push every pending edit, each claimed, sent and settled on its own."""
        records: list[Record] = yield _InStore(session.to_push)
        for record in records:
            if self._waits(record):
                continue
            object_id = self._new_object_id() if record.object_id is None else None
            claimed: Record | None = yield _InStore(
                session.claim, (record.local_key,), {"object_id": object_id}
            )
            if claimed is None:
                continue
            # A send that raises leaves the push in flight, since the request
            # may have reached the server; the next complete ingest settles it.
            outcome = yield from self._send(claimed)
            yield _InStore(session.settle, (claimed, outcome))

    @abstractmethod
    def _read(self, session: Session, scope: Scope, *, full_listing: bool) -> Steps[_Read]:
        """Fetch what changed in the scope and ingest it, as one change set or listing pages."""

    @abstractmethod
    def _send(self, claimed: Record) -> Steps[PushOutcome]:
        """Send one claimed edit and return how it ended."""

    @abstractmethod
    def _waits(self, record: Record) -> bool:
        """Whether a pending edit can't be sent yet and waits for a later sync."""

    @abstractmethod
    def _new_object_id(self) -> str | None:
        """An object id for a create that has none, or ``None`` where the server picks it."""

    @abstractmethod
    def _edit_fields(self, content: bytes, *, create: bool) -> _EditFields:
        """The fields of an edit to this content, a create or a change.

        Raises:
            ValueError: If the content can't be an object of the scope.
        """
