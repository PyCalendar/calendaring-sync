# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An adapter for a server that keeps its objects in memory."""

from __future__ import annotations

import hashlib
import uuid
from typing import cast

from calendaring_sync import (
    Applied,
    Change,
    Changed,
    ChangeSet,
    EditFields,
    EditKind,
    Gone,
    LocalEdit,
    PushOutcome,
    Record,
    Rejected,
    Removed,
    Replica,
    Scope,
    ScopeGoneError,
    Session,
)
from calendaring_sync.adapters.base import (
    Adapter,
    AsyncAdapter,
    InStore,
    ReadResult,
    Steps,
    SyncAdapter,
)

# An object as the server holds it: its version and its content.
Stored = tuple[str, bytes]


class VersionMismatch(Exception):
    """The object doesn't have the version the write was based on."""


class NotFound(Exception):
    """The server has no object with that id."""


class ExpiredToken(Exception):
    """The server no longer knows what changed since that token."""


class NoSuchScope(Exception):
    """The server no longer holds the objects at all."""


class MemoryServer:
    """A server that numbers its writes: a write's number is the token and the version."""

    def __init__(self) -> None:
        self.objects: dict[str, Stored] = {}
        # The id of the object each write touched, in order.
        self.log: list[str] = []
        # The oldest token the server still answers changes since.
        self.oldest = 0
        self.removed = False

    def token(self) -> str:
        return str(len(self.log))

    def listing(self) -> tuple[dict[str, Stored], str]:
        if self.removed:
            raise NoSuchScope()
        return dict(self.objects), self.token()

    def changes(self, since: str) -> tuple[dict[str, Stored | None], str]:
        """Every object written since the token, ``None`` for one deleted since."""
        if self.removed:
            raise NoSuchScope()
        if int(since) < self.oldest:
            raise ExpiredToken(since)
        written = set(self.log[int(since) :])
        return {object_id: self.objects.get(object_id) for object_id in written}, self.token()

    def put(self, object_id: str, content: bytes, *, if_version: str | None) -> None:
        """Write an object, only if it has ``if_version``; ``None`` creates one."""
        self._check(object_id, if_version)
        self.log.append(object_id)
        self.objects[object_id] = (self.token(), content)

    def delete(self, object_id: str, *, if_version: str | None) -> None:
        """Delete an object, only if it has ``if_version``."""
        self._check(object_id, if_version)
        self.log.append(object_id)
        del self.objects[object_id]

    def _check(self, object_id: str, if_version: str | None) -> None:
        current = self.objects.get(object_id)
        if current is None and if_version is not None:
            raise NotFound(object_id)
        if (None if current is None else current[0]) != if_version:
            raise VersionMismatch(object_id)


class AsyncMemoryServer:
    """The same server, for an asynchronous client."""

    def __init__(self, server: MemoryServer) -> None:
        self.server = server

    async def listing(self) -> tuple[dict[str, Stored], str]:
        return self.server.listing()

    async def changes(self, since: str) -> tuple[dict[str, Stored | None], str]:
        return self.server.changes(since)

    async def put(self, object_id: str, content: bytes, *, if_version: str | None) -> None:
        self.server.put(object_id, content, if_version=if_version)

    async def delete(self, object_id: str, *, if_version: str | None) -> None:
        self.server.delete(object_id, if_version=if_version)


def fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def change(stored: Stored | None) -> Change:
    """What the core needs to know about an object the server reported."""
    if stored is None:
        return Removed()
    version, content = stored
    return Changed(version, fingerprint(content), content)


class _MemoryAdapter(Adapter):
    """Everything the synchronous and asynchronous adapters share."""

    _fingerprint_scheme = "sha256"
    _media_type = "text/plain"
    # Every write names the version it was based on.
    _guarded_by_version = True
    _unique_logical_key = False
    _guard_covers_scope = False

    def __init__(
        self, replica: Replica, server: MemoryServer | AsyncMemoryServer, scope_id: str
    ) -> None:
        super().__init__(replica, scope_id)
        self._server = server

    def _read(self, session: Session, scope: Scope, *, full_listing: bool) -> Steps[ReadResult]:
        since = None if full_listing else scope.token
        try:
            if since is None:
                objects, token = yield self._server.listing()
            else:
                objects, token = yield self._server.changes(since)
        except ExpiredToken:
            return ReadResult.INVALID_TOKEN
        except NoSuchScope as exc:
            raise ScopeGoneError(f"The server no longer holds {self.scope_id!r}") from exc
        changes = {object_id: change(stored) for object_id, stored in objects.items()}
        listing = since is None
        change_set = ChangeSet(self.scope_id, scope.token, token, changes, full_listing=listing)
        yield InStore(session.ingest, (change_set,))
        return ReadResult.COMPLETE

    def _send(self, claimed: Record) -> Steps[PushOutcome]:
        # A claimed record holds its edit and the id to send it under.
        edit, object_id = cast(LocalEdit, claimed.edit), cast(str, claimed.object_id)
        try:
            if edit.kind is EditKind.DELETE:
                yield self._server.delete(object_id, if_version=claimed.version)
            elif edit.kind is EditKind.CREATE:
                yield self._server.put(object_id, cast(bytes, edit.content), if_version=None)
            else:
                yield self._server.put(
                    object_id, cast(bytes, edit.content), if_version=claimed.version
                )
        except VersionMismatch:
            return Rejected()
        except NotFound:
            return Gone()
        return Applied()

    def _waits(self, record: Record) -> bool:
        return False

    def _new_object_id(self) -> str:
        return uuid.uuid4().hex

    def _edit_fields(self, content: bytes, *, create: bool) -> EditFields:
        return EditFields(fingerprint(content))


class MemoryAdapter(_MemoryAdapter, SyncAdapter):
    """Syncs a :class:`MemoryServer` with a replica."""


class AsyncMemoryAdapter(_MemoryAdapter, AsyncAdapter):
    """Syncs an :class:`AsyncMemoryServer` with a replica."""
