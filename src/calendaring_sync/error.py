# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""calendaring-sync's error hierarchy.

Every error the library raises on purpose derives from
:class:`CalendaringSyncError`. A call with an argument that can never be
valid, such as a negative cursor, raises :class:`ValueError` instead.
"""


class CalendaringSyncError(Exception):
    """Base for every calendaring-sync error."""


class StaleChangeSetError(CalendaringSyncError):
    """A :term:`change set` was fetched from a token the :term:`scope` no longer has.

    Another process ingested first, or started another listing of the same
    scope. The changes it fetched are already outdated, so the adapter
    fetches again from the scope's current token.
    """


class TrimmedFeedError(CalendaringSyncError):
    """A :term:`cursor` is older than the oldest :term:`change feed` entry kept.

    The entries after the cursor were trimmed, so they can't be read. Read
    the scope's records again, and continue from
    :meth:`~calendaring_sync.replica.Replica.feed_head`.
    """


class UnknownScopeError(CalendaringSyncError):
    """The :term:`store` has no :term:`scope` with this id."""


class UnknownRecordError(CalendaringSyncError):
    """The :term:`store` has no :term:`record` with this :term:`local key`."""


class DuplicateObjectError(CalendaringSyncError):
    """Another :term:`record` in the :term:`scope` already has this :term:`object id`."""


class EditRefusedError(CalendaringSyncError):
    """The :term:`record` is in a state that takes no :term:`local edit`.

    Raised as itself for a record whose :term:`push` is :term:`in flight`.
    Its subclasses cover a record in :term:`conflict` and a removed one.
    """


class OpenConflictError(EditRefusedError):
    """The :term:`record` holds an open :term:`conflict`; resolve it before editing."""


class RemovedRecordError(EditRefusedError):
    """The :term:`record` was removed from the server, so there's nothing to edit."""


class ScopeMismatchError(CalendaringSyncError):
    """A :term:`session` was opened with a media type the :term:`scope` doesn't have.

    The stored content is in the scope's media type, so it can't be reused.
    Drop the scope and sync it again.
    """


class SessionError(CalendaringSyncError):
    """A :term:`session` was used out of order.

    For example a second session on a scope that already has one open, an
    ingest after the session's last page, or a reset while a session is open.
    """


class TransactionError(CalendaringSyncError):
    """A :term:`store` transaction was used the wrong way.

    Raised for a transaction opened while the thread already has one, and
    for closing the store inside a transaction. Its subclasses cover an
    operation outside a transaction and a write inside a read.
    """


class TransactionRequiredError(TransactionError):
    """A :term:`store` operation ran outside a transaction."""


class TransactionPermissionError(TransactionError):
    """A :term:`store` operation that writes ran in a transaction opened with ``write=False``."""


class StoreError(CalendaringSyncError):
    """The :term:`store` couldn't read or write its data, such as a file that isn't a store."""


class StoreBusyError(StoreError):
    """Another process kept the :term:`store` busy for longer than the store waits.

    Nothing was changed. Try again later.
    """


class StoreVersionError(StoreError):
    """The store was written by a newer calendaring-sync, with a schema this one can't read."""


class UnexpectedStoreError(CalendaringSyncError):
    """A :term:`store` failed in a way it doesn't expect, which is a bug in it or its caller.

    It isn't a :class:`StoreError`, so code that retries on a store error
    doesn't retry a bug. The original exception is its ``__cause__``.
    """
