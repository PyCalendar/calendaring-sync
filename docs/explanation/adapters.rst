.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

========
Adapters
========

.. meta::
   :description: What every calendaring-sync adapter provides, and how each adapter maps its protocol onto the sync model.

An adapter maps one protocol onto the model in :doc:`design`. Adding an adapter doesn't change the core. The adapters in this package each sit behind their own optional extra, so the core never imports a protocol's client library, and an adapter can also live in another package. Each adapter's section cites the specifications behind its rules, and :doc:`../reference/server-compatibility` records where tested servers depart from them. The :doc:`../reference/glossary` defines the sync terms this page uses.

Why every adapter provides the same things
==========================================

The core never branches on protocol, so every adapter answers the same questions, which :class:`~calendaring_sync.adapters.base.Adapter` lists: what a :term:`scope` and an :term:`object id` are, how to read changes and send an edit, and what the server checks before it applies one. A protocol without a :term:`version` per object has every reported object fetched and compared by :term:`fingerprint`, and one without changes since a :term:`sync token` gets a :term:`full listing` on every sync. An adapter's synchronous and asynchronous variants share all of its translation and parsing, and differ only in the client they're given.

CalDAV
======

CalDAV keeps calendars on a web server, using the WebDAV extensions to HTTP. Each calendar is a calendar collection, a folder at its own web address. Each event or task in a calendar is a member: one iCalendar file, the standard text format for calendar data, at its own address, called its href. The server gives every member an ETag, a short string that changes when the member changes. To learn what changed, a client sends a ``sync-collection`` report with the sync token from its last report, and the server answers with the members changed or removed since then, plus a new token. A ``PROPFIND`` request lists every member with its ETag, and a ``calendar-multiget`` report fetches several members' content in one request. The adapter sends these requests through python-caldav, a Python CalDAV client library, at the version its ``caldav`` extra requires.

Why a CalDAV scope is one calendar collection
---------------------------------------------

Each collection has its own sync token (:rfc:`6578`), so one collection is one scope.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - calendaring-sync
     - CalDAV
   * - Scope
     - A calendar collection
   * - Object id
     - The path of the member's href, always spelled one way, since python-caldav can spell one member's address two ways (``%40`` from a sync report, ``@`` from a ``calendar-multiget``, for a path holding an ``@``)
   * - Version
     - ``DAV:getetag`` (:rfc:`4918#section-15.6`), a strong entity tag compared character by character (:rfc:`9110#section-8.8.3.2`)
   * - Sync token
     - ``DAV:sync-token``, an opaque URI (:rfc:`6578#section-3.2`)
   * - Content and fingerprint
     - The iCalendar data (``text/calendar``) as the server sent it, and SHA-256 of a canonical form of it, as :ref:`caldav-fingerprint` describes
   * - Containers
     - The collection the member is in
   * - Logical key
     - The ``UID`` property (:rfc:`5545#section-3.8.4.7`); not unique, since a tested server accepts two members with one UID, so a create is recognized by its href

Why the adapter checks supported reports first
----------------------------------------------

Before a collection's first sync, the adapter reads ``DAV:supported-report-set`` (:rfc:`3253#section-3.1.5`), the list of reports the collection supports, so it knows whether ``sync-collection`` works before any report fails. A collection without it gets a :term:`full listing` every time, and the adapter fetches only changed members. So does a collection that names no ``DAV:sync-token``, which one that supports the report must (:rfc:`6578#section-4`), since a report from an empty token would ask for every member.

Why a full listing reads the token first
----------------------------------------

A :term:`full listing` reads the collection's ``DAV:sync-token`` property, which holds the token a report would return at that moment (:rfc:`6578#section-4`), and then lists each member and its ETag with a ``PROPFIND`` (:rfc:`4918#section-9.1`) of ``Depth: 1``, which covers the collection and the members directly inside it (:rfc:`4918#section-10.2`). Members whose ETag matches the stored one are reported unchanged, and the rest are fetched. Anything that changes during the listing appears in the next report from that token.

The adapter never lists with a report sent with an empty token, which asks for every member. A server may truncate any report (:rfc:`6578#section-3.6`), and a tested server truncates an empty-token report at a :term:`checkpoint` that skips the remaining members, so a listing would infer that objects were removed when they weren't. A server can cap a ``PROPFIND`` too: a tested server lists at most its configured number of results and marks the cut with a 507 for the collection. That cut is visible and never comes with a token that skips members, but a listing cut short still can't tell a missing member from a removed one. So when a ``PROPFIND`` comes back with a 507, the adapter raises an error instead of ingesting it, and the server's limit has to be raised. That leaves a known gap: a calendar larger than the limit can't be synced from such a server.

Why changes come from python-caldav's sync report
-------------------------------------------------

Changes since a token come from python-caldav's ``get_objects_by_sync_token()``. Up to 3.6.0, python-caldav sends ``sync-collection`` with ``Depth: 1``, although the report is only defined for ``Depth: 0`` (:rfc:`6578#section-3.2`); every tested server answers it all the same, and later releases send ``Depth: 0``. When a server truncates a report with a 507 status and a :term:`checkpoint` token (:rfc:`6578#section-3.6`), python-caldav follows the checkpoints, up to :attr:`~caldav.collection.Calendar.sync_max_pages` reports, and the adapter hands the core the whole result as one change set. If the result is still cut after that, the change set is a :term:`truncated page` that ends at the last checkpoint, nothing is pushed, and the next sync continues from there. The adapter turns python-caldav's fallback off: it would answer a failed report with a full listing under a client-side ``fake-`` token.

A removed member has a 404 status and no ``DAV:propstat``, the element that would carry its properties (:rfc:`6578#section-3.2`), and python-caldav lists it in the result's ``deleted_urls``, which the adapter reads removals from. Some servers report a member deleted and created again at one href only as removed, so the adapter fetches removed members too, in multigets of their own: one that comes back with data is reported changed, the rest removed. A changed member whose reported ETag is the one already stored hasn't changed since the adapter fetched it, so it isn't fetched again. A server can leave the new token out of its answer; the adapter then keeps the scope's token, so nothing in that answer is lost, and the next sync asks again from the same point.

The adapter never sends ``DAV:limit`` (:rfc:`6578#section-3.7`), which asks the server to return at most a given number of results: a server can apply it by dropping the remaining changes and returning its newest token.

Why only an invalid token resets the scope
------------------------------------------

A server that won't accept a token answers with the ``DAV:valid-sync-token`` precondition, an error element saying the token isn't valid (:rfc:`6578#section-3.2`), and the client falls back to a full sync, without downloading unchanged cached resources. :rfc:`4791#section-1.3` specifies 403 or 409 for a failed precondition, and a tested server sends 412.

The adapter :term:`resets <reset>` the scope only when the error names ``DAV:valid-sync-token``, whatever its status. Any other failure, such as a refused login or a server that's down, is raised, so a flaky server doesn't cause a full listing on every error. The adapter only sends tokens the server issued, so a 500 means the server failed, not that the token was malformed. python-caldav keeps the precondition on every error, so the adapter reads it from the error's ``preconditions``, whatever the status.

A tested server answers a token it no longer accepts with a report that leaves out older changes and raises no error. The adapter can't tell that from an ordinary report, so it says whether a scope needs regular full listings, from python-caldav's ``sync-token.invalid-token`` feature. Your application sets that feature by naming the server or by probing it with caldav-server-tester, so a new server or an upgrade needs no list of names here. How often to run them is up to your application; a full listing only fetches members whose ETag changed, so a frequent one is cheap.

Why content comes from a multiget, with its own ETag
----------------------------------------------------

Changed members are fetched with ``calendar-multiget`` (:rfc:`4791#section-7.9`), asking for ``DAV:getetag`` and ``calendar-data`` together, with no ``Depth`` header as that section asks. The stored version is the ETag from this response, not from the sync report: an object can change in between (:rfc:`6578#section-3.1`), and an older ETag would cause a needless fetch on the next sync and a wrong base for the next write.

A member that comes back without data, as python-caldav returns a 404 entry, or is missing from the response although :rfc:`4791#section-7.9` requires an entry for every href, was removed after the report. It's left out of the change set, and the next report carries its removal. A server can also answer the whole ``calendar-multiget`` with 404 when one member is gone; the adapter then checks that the calendar is still there and fetches each member on its own. A member that comes back with data but no ETag is kept with no version, so the next listing fetches it again.

.. _caldav-fingerprint:

Why the fingerprint leaves out three properties
-----------------------------------------------

Tested servers rewrite what they store: they add ``DTSTAMP``, reorder properties and parameters, fold lines at other points, and reorder the parts of a recurrence rule, which may come in any order (:rfc:`5545#section-3.3.10`). A server sets ``DTSTAMP`` and ``LAST-MODIFIED`` itself (:rfc:`5545#section-3.8.7.2`, :rfc:`5545#section-3.8.7.3`), and ``PRODID`` names the product that created the object (:rfc:`5545#section-3.7.3`). So the canonical form unfolds the lines, sorts the components, the properties and parameters within each, and the parts of each recurrence rule, and leaves those three properties out. A server's copy of your own write then fingerprints the same as what you sent, on every tested server. ``UID`` is kept, since it's the logical key, and so is everything else, because any other difference can be a real edit. Calendar data is UTF-8 unless a charset says otherwise (:rfc:`5545#section-3.1.4`): python-caldav hands the adapter what a server sent as UTF-8, and the adapter refuses an edit that isn't.

Why writes are conditional
--------------------------

Updates and deletes send ``If-Match`` with the base ETag (:rfc:`9110#section-13.1.1`), so the server applies them only if the member still has that ETag, and creates send ``If-None-Match: *`` (:rfc:`9110#section-13.1.2`), so a create never replaces an existing member. A mismatch gets status 412, and the edit stays pending for the next :term:`ingest`. ``If-Match`` compares ETags strongly, so a weak one (``W/``) never matches; an edit to a member whose stored ETag is weak waits, without being sent, until a sync stores a strong one, and :meth:`~calendaring_sync.adapters.base.SyncAdapter.waiting` lists such edits. The ETag comes from the ``DAV:getetag`` property of the server's answer, not from an HTTP header a proxy can weaken, so edits wait only on a server that reports a weak ETag itself.

Creates and updates use python-caldav's ``save()`` with ``raw_data``, which sends the bytes you recorded unchanged, and ``precondition="etag"``, which sends the stored ETag. Updates also pass ``increase_seqno=False``: ``save()`` increments an event's ``SEQUENCE`` revision number by default, and though it leaves the bytes passed as ``raw_data`` alone, the flag says outright that the server receives the edit you recorded. Creates pass ``no_overwrite=True``, which sends ``If-None-Match: *``. Deletes use ``delete(if_match=True, precondition="etag")``. A 404 on an update or a delete means the member is gone; a tested server answers a delete of a gone member with 412 instead, which leaves the delete pending until the next ingest reports the removal.

A create uses an href of unreserved characters only (:rfc:`3986#section-2.3`), a UUID and ``.ics``, so it can't come back spelled differently. A server that already holds the UID answers with an error element, ``CALDAV:no-uid-conflict`` (:rfc:`4791#section-5.3.2.1`) or ``CALDAV:unique-scheduling-object-resource`` (:rfc:`6638#section-3.2.4.1`); python-caldav raises ``UidConflictError`` for either, whatever the status, so the adapter never mistakes it for a plain precondition failure. For a create or an update, that's a :term:`conflict` with the object that holds the UID, named when the error element holds its ``DAV:href`` and that href is in the calendar; otherwise the core finds the holder by its UID.

Unless the client is configured with the server's profile, python-caldav searches the calendar for the UID of a create before sending it, which costs a request per create and refuses a UID that's taken even on a server that would accept it. Configuring the client's ``features`` with the server's name lets the server check instead.

A server MUST NOT return a strong ETag for data it stored differently (:rfc:`4791#section-5.3.4`), but tested servers do, so an applied push doesn't keep the ETag it got back.

JMAP
====

JMAP is a protocol for reading and changing data on a server with JSON requests over HTTP. A server holds one or more accounts, and each account holds records of several types, such as calendars, events, and tasks. Every record has an ``id``. For each record type in an account, the server keeps a ``state`` string that changes whenever any record of that type changes. A client calls methods on a record type, written as the type's name and the method, such as ``CalendarEvent/get``:

- ``/changes`` returns the ids created, updated, or destroyed since a state you give it, and the new state
- ``/get`` fetches records by id
- ``/query`` lists the ids that match a filter
- ``/set`` creates, updates, and destroys records

Events are JSCalendar objects, a JSON format for calendar data. The adapter makes these calls through calendaring-jmap, a Python JMAP client library, at the version its ``jmap`` extra requires, which writes the JSCalendar you recorded without converting it and keeps an event's own ``calendarIds``.

Why a JMAP scope is one account and one record type
---------------------------------------------------

A record type is the kind of data a JMAP record holds, such as ``CalendarEvent`` for events or ``Task`` for tasks. A ``state`` string covers all records of one type in one account (:rfc:`8620#section-5.1`), so ``CalendarEvent`` and ``Task`` in one account are two scopes. You name the account when you make the adapter, since a JMAP session can reach several, such as ones shared with you.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - calendaring-sync
     - JMAP
   * - Scope
     - One account and one record type
   * - Object id
     - The record's ``id``, unique per record type per account (:rfc:`8620#section-1.6.3`)
   * - Version
     - None: JMAP has no per-record version
   * - Sync token
     - The type's ``state`` string
   * - Content and fingerprint
     - The event as the server returned it, as compact UTF-8 JSON, and SHA-256 of a canonical form of it, as :ref:`jmap-fingerprint` describes
   * - Containers
     - The calendars set to true in the event's ``calendarIds``
   * - Logical key
     - The ``uid``, followed by a space and the ``recurrenceId`` for an event that's one instance: one account may hold several events with one ``uid`` only when their ``recurrenceId`` values differ (draft-ietf-jmap-calendars-33 section 1.4.1)

Content stays JSCalendar, since converting to iCalendar would lose data on each round trip; converting is left to your application.

With no per-record version, every object ``/changes`` reports is fetched and compared by :term:`fingerprint`, and a :term:`full listing` fetches everything.

.. _jmap-fingerprint:

Why the fingerprint leaves out what the server sets or fills in
---------------------------------------------------------------

The server sets ``id``, ``baseEventId`` and ``isOrigin``, and works out ``utcStart`` and ``utcEnd`` when asked (draft-ietf-jmap-calendars-33 section 5). It also sets ``created``, ``updated`` and ``sequence`` itself (draft-ietf-jmap-calendars-33 section 5.9), and ``prodId`` names the product that last changed the event (:rfc:`8984#section-4.1.4`). Tested servers rewrite more than that when they store an event, without changing what it means. One leaves out every member that holds its default value, such as an alert's ``action`` of ``display``. The other leaves out a ``@type`` its place already implies, adds ``updated`` inside each recurrence override, adds conversion metadata (``iCalendar``) to a location, and leaves out ``version``.

So the fingerprint is SHA-256 of the event serialized with sorted keys, leaving out:

- the members the server sets, and Cyrus' ``x-href``
- the ``iCalendar`` member of any object
- every member holding its default, from the defaults draft-ietf-calext-jscalendarbis-22 and draft-ietf-jmap-calendars-33 section 5 give each kind of object, a ``@type`` its place implies included (draft-ietf-calext-jscalendarbis-22 section 1.4.4)
- ``version`` ``2.0``, which a server MUST give an event created without one (draft-ietf-jmap-calendars-33 section 5.9)
- a participant's ``attendee`` role, which JSCalendar 2.0 no longer lists and both tested servers drop, and an empty ``roles``
- inside each recurrence override, only ``created``, ``updated``, ``sequence`` and ``iCalendar``: a member an override leaves out is inherited from the event, so a default there is a real change

A server's copy of your own write then fingerprints the same as what you sent, on both tested servers. The price is that an edit setting a member to its default, or changing only an ``iCalendar`` member, isn't pushed. A server's copy is always fingerprinted from a full ``/get``, since a ``/set`` answer only carries some properties; your edit is fingerprinted from the content you recorded.

A server does add real data sometimes: an event created with participants and no ``organizerCalendarAddress`` gets one, since the server MUST set it (draft-ietf-jmap-calendars-33 section 5.9), and tested servers add the organizer as a participant too. The next sync then reports that event changed, once, so your application sees its organizer.

Why some errors reset the scope
-------------------------------

On ``cannotCalculateChanges``, the error a server returns when it can't work out the changes since the state given, the client MUST invalidate its cache for the type (:rfc:`8620#section-5.2`), so the adapter :term:`resets <reset>` the scope. It also resets on two answers seen on a tested server: an ``oldState`` that doesn't repeat the ``sinceState`` the adapter sent, and ``invalidArguments`` from ``/changes``. The adapter only sends states the server issued and a valid ``maxChanges``, so the state is the only argument that can be wrong. One answer looks like a real one: Cyrus answers a state newer than any it issued as if nothing changed, which happens after the server is restored from a backup. Nothing tells the adapter to reset then, so list each scope in full after a restore. An account the server doesn't have (``accountNotFound``, :rfc:`8620#section-3.6.2`) raises :class:`~calendaring_sync.error.ScopeGoneError`, as any scope a server lost does.

Why pages follow the new state
------------------------------

When a ``/changes`` answer leaves out some changes, it sets ``hasMoreChanges`` to true, and the client may call ``/changes`` again with the ``newState`` it received (:rfc:`8620#section-5.2`). The adapter asks for at most :data:`~calendaring_sync.adapters.jmap.PAGE_SIZE` changes a page, and each answer is a :term:`truncated page` that advances the scope to its ``newState``.

Objects are fetched with ``/get`` after ``/changes``, so one can be newer than the ``newState`` it came with. The next ``/changes`` reports it again, and the matching fingerprint makes that a no-op. One that's gone by the ``/get`` is left out, and the next ``/changes`` reports its removal.

Why a full listing checks the state before its last page
--------------------------------------------------------

A full listing reads the type's ``state`` first, then pages through ``/query`` and ``/get`` (:rfc:`8620#section-5.5`), :data:`~calendaring_sync.adapters.jmap.PAGE_SIZE` events a page, counting the position itself, since a tested server answers every page with position 0. That first state becomes the token, so anything changed during the listing appears in the next ``/changes``.

Pages are counted by position, so an event destroyed while the listing runs shifts every later page by one, and an unchanged event can fall between two pages. The last page removes every object the listing never mentioned, so before it, the adapter reads the state again. If it moved, the adapter reads ``/changes`` from the first state: events created or changed meanwhile join the last page, but if one was destroyed, the listing ends there, removes nothing and pushes nothing. The scope's token hasn't moved, so the next sync reads the changes since it, or lists again if the scope has no token yet.

Why every create carries its own UID
------------------------------------

A server generates a ``uid`` only when a create omits it (draft-ietf-jmap-calendars-33 section 5.9). The adapter refuses a create without one, and a change that would leave a create it hasn't sent yet without one, so a create whose result was lost is recognized when it comes back under a server-assigned id. A create the server refuses because another event holds its ``uid`` (``invalidProperties`` naming ``uid``) becomes a :term:`conflict`; the server doesn't name that event, so the core finds it by the logical key. Both tested servers keep an event's ``uid`` when an update sends another one, so a change can't take a ``uid`` either.

Why writes use ifInState
------------------------

A ``/set`` with ``ifInState``, the state the client expects, fails with ``stateMismatch`` if the type's state has moved (:rfc:`8620#section-5.3`). Each write carries the state after the previous one: the state the sync ingested to, then each applied write's ``newState``, which every ``/set`` answer holds. A write the server refuses changes nothing, so the state stays.

That state covers the whole account, so a rejection means something changed, not necessarily this object. The rest of the sync's pushes would be rejected too, so they aren't sent: every edit stays pending for the next sync, which ingests the change first. Another client's delete moves the state as well, so a delete of an event someone else deleted is rejected before the server would say it's gone; the next ingest reports the removal. When a server does answer ``notFound``, the push ended gone: a change becomes a :term:`conflict` and a delete is done.

Why the adapter syncs events only
---------------------------------

The JMAP adapter syncs ``CalendarEvent`` records. Tasks would fit the model as a scope of their own, but no tested JMAP server advertises JMAP Tasks (``urn:ietf:params:jmap:tasks``), so there's nothing to test a task adapter against.
