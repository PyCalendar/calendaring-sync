# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""calendaring-sync's error hierarchy.

Every error the library raises on purpose derives from
:class:`CalendaringSyncError`, except for arguments that can never be
valid: those raise :class:`ValueError`, such as a feed page size of 0, or
:class:`TypeError`, such as content that isn't bytes. An argument refused
for what the store holds, such as a record of another scope, raises an
error that is both a :class:`CalendaringSyncError` and a
:class:`ValueError`, as a record a store can't hold does with
:class:`RecordValueError`.
"""


class CalendaringSyncError(Exception):
    """Base for every calendaring-sync error."""


class StaleChangeSetError(CalendaringSyncError):
    """A :term:`change set` was fetched from a token the :term:`scope` no longer has.

    Another process ingested first, or started another listing of the same
    scope, so the changes fetched are outdated. Nothing was ingested; the
    next sync fetches from the scope's current token.
    """


class ListingRequiredError(CalendaringSyncError, ValueError):
    """An incremental :term:`change set` arrived for a :term:`scope` with no :term:`sync token`.

    A scope gets its token from its first :term:`full listing`, and again
    after a :term:`reset`, so only a listing can be ingested until then.
    """


class UnknownVersionError(CalendaringSyncError, ValueError):
    """A :term:`full listing` reported an object unchanged, but the store holds no version for it.

    An adapter reports an object unchanged only when its version equals
    one :meth:`~calendaring_sync.replica.Replica.known_versions` returned,
    so the listing was built from versions the store no longer has. Nothing
    was ingested; build the listing again.
    """


class TrimmedFeedError(CalendaringSyncError):
    """A :term:`cursor` is older than the oldest :term:`change feed` entry kept.

    The entries after the cursor were trimmed, so they can't be read. Read
    the scope's records again, and continue from
    :meth:`~calendaring_sync.replica.Replica.feed_head`.
    """


class FeedRangeError(CalendaringSyncError, ValueError):
    """A :term:`change feed` number is below 0 or past the last entry.

    No entry has that number yet, so the number didn't come from this
    store's feed, or the store was replaced since.
    """


class UnknownScopeError(CalendaringSyncError):
    """The :term:`store` has no :term:`scope` with this id."""


class UnknownRecordError(CalendaringSyncError):
    """The :term:`store` has no :term:`record` with this :term:`local key`."""


class DuplicateObjectError(CalendaringSyncError):
    """Another :term:`record` in the :term:`scope` already has this :term:`object id`."""


class OtherScopeError(CalendaringSyncError, ValueError):
    """A :term:`record` or :term:`change set` belongs to another :term:`scope`.

    A session or adapter works on one scope, and refuses what belongs to
    another before anything is written.
    """


class ObjectIdAssignedError(CalendaringSyncError, ValueError):
    """The :term:`record` already has an :term:`object id`, so it can't be given another.

    Only a create that has none takes an object id when its push is
    claimed; claim any other record without one.
    """


class LoneSurrogateError(CalendaringSyncError, ValueError):
    """A string holds a :ref:`lone surrogate <lone-surrogates>`, which no store can keep.

    It's refused where the string comes in, before anything is written.
    """


class RecordValueError(CalendaringSyncError, ValueError):
    """A :term:`record` holds what its state can't, so a :term:`store` refuses to write it.

    For example a pending record without a :term:`pending edit`, or a
    :term:`conflict` that says the server removed the object but holds the
    server's content. It's a :class:`ValueError` too, since such a record
    can never be valid.
    """


class EditRefusedError(CalendaringSyncError):
    """The :term:`record` is in a state that takes no :term:`local edit`.

    Its subclasses say which: a :term:`push` :term:`in flight`, an open
    :term:`conflict`, or a record the server removed.
    """


class PushInFlightError(EditRefusedError):
    """The :term:`record`'s :term:`push` is :term:`in flight`; edit it once the push has ended."""


class OpenConflictError(EditRefusedError):
    """The :term:`record` holds an open :term:`conflict`; resolve it before editing."""


class RemovedRecordError(EditRefusedError):
    """The :term:`record` was removed from the server, so there's nothing to edit."""


class StaleConflictError(CalendaringSyncError):
    """The :term:`conflict` changed since your application read it, so a resolution isn't applied.

    The record ended, or its edit or the server's copy moved on. Read it
    again and decide on what it holds now.
    """


class UnknownHolderError(CalendaringSyncError):
    """No single record is known to hold what a refused create was refused for.

    The server named no record that's still there, and none or several hold
    the create's :term:`logical key`, or it was sent without one. So the
    create can't be pushed as a change to one of them. Resolve it another
    way, or sync first.
    """


class PendingEditError(CalendaringSyncError):
    """The :term:`record` holds a :term:`pending edit` of its own, which this would replace.

    Push or discard that edit first, then try again.
    """


class ScopeMismatchError(CalendaringSyncError):
    """A :term:`session` was opened with a media type the :term:`scope` doesn't have.

    The stored content is in the scope's media type, so it can't be reused.
    Drop the scope and sync it again.
    """


class ScopeGoneError(CalendaringSyncError):
    """The server no longer has the :term:`scope`, such as a calendar that was deleted.

    Nothing was ingested, so the :term:`store` is as it was. A server that
    lost the scope only for a moment has it back at the next sync. If it's
    gone for good, :meth:`~calendaring_sync.replica.Replica.drop_scope`
    returns the pending edits and open conflicts it discards.
    """


class IncompleteListingError(CalendaringSyncError):
    """The server cut a :term:`full listing` short, so it wasn't ingested.

    A listing that leaves objects out can't tell them from objects the
    server removed, so the :term:`store` is left as it was. Raising the
    server's limit on how many results it lists lets the next listing
    through.
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
