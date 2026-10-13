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


class StoreVersionError(CalendaringSyncError):
    """The store was written by a newer calendaring-sync, with a schema this one can't read."""
