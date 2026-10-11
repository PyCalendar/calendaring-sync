.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

========
Adapters
========

.. meta::
   :description: What every calendaring-sync adapter provides, and how each adapter maps its protocol onto the sync model.

An adapter maps one protocol onto the model in :doc:`design`. Adding an adapter doesn't change the core. The adapters in this package each sit behind their own optional extra, so the core never imports a protocol's client library, and an adapter can also live in another package. Each adapter's section cites the specifications behind its rules, and :doc:`../reference/server-compatibility` records where tested servers depart from them.

Why every adapter provides the same things
==========================================

The core never branches on protocol, so it asks every adapter for the same things:

- what a scope is, and an object id unique within it
- a version per object, if the protocol has one; without it, every reported object is fetched and compared by fingerprint
- changes since a token, if the protocol has them; without them, every sync is a full listing
- the content's media type, and its own fingerprint function and scheme name if the exact text isn't stable
- each object's containers and logical key, where the protocol has them
- change sets built from the protocol's reports: incremental or a full listing, complete or one truncated page
- each push's outcome (applied, rejected, gone, refused for its logical key, or unknown), and which errors mean the scope must be listed again

An adapter's synchronous and asynchronous variants share all of its translation and parsing, and differ only in the client library calls they make.

CalDAV
======

Why a CalDAV scope is one calendar collection
---------------------------------------------

:rfc:`6578` issues one sync token per collection.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - calendaring-sync
     - CalDAV
   * - Scope
     - A calendar collection
   * - Object id
     - The member's href, as the server reports it
   * - Version
     - ``DAV:getetag`` (:rfc:`4918#section-15.6`), a strong entity tag compared character by character (:rfc:`9110#section-8.8.3.2`)
   * - Sync token
     - ``DAV:sync-token``, an opaque URI (:rfc:`6578#section-3.2`)
   * - Content and fingerprint
     - The iCalendar text (``text/calendar``), and SHA-256 of it
   * - Containers
     - The collection the member is in
   * - Logical key
     - The ``UID`` property (:rfc:`5545#section-3.8.4.7`)

Why the adapter checks supported reports first
----------------------------------------------

Before a collection's first sync, the adapter reads ``DAV:supported-report-set`` (:rfc:`3253#section-3.1.5`), so a collection without ``sync-collection`` is known before any report fails. Such a collection is synced by a full listing every time, and only changed members are fetched.

Why a full listing reads the token first
----------------------------------------

A full listing reads the collection's ``DAV:sync-token`` property, which holds the token a report would return at that moment (:rfc:`6578#section-4`), and then lists each member and its ETag with a ``PROPFIND`` of ``Depth: 1`` (:rfc:`4918#section-9.1`). Members whose ETag matches the stored one are reported unchanged, and the rest are fetched. Anything that changes during the listing appears in the next report from that token.

The adapter never lists with an empty-token report. A server may truncate any report (:rfc:`6578#section-3.6`), and a tested server truncates an empty-token report at a checkpoint that skips the remaining members, so a listing would infer that objects were removed when they weren't. ``PROPFIND`` has no truncation: its answer lists every member.

Why changes come from python-caldav's sync report
-------------------------------------------------

Changes since a token come from python-caldav's ``get_objects_by_sync_token()``, which sends ``sync-collection`` with ``Depth: 0`` (:rfc:`6578#section-3.2`). When a server truncates a report with a 507 and a checkpoint token (:rfc:`6578#section-3.6`), python-caldav follows the checkpoints until the report is complete, and the adapter hands the core the whole result. The adapter turns python-caldav's fallback off: it would answer a failed report with a full listing under a client-side ``fake-`` token.

A removed member has a 404 status and no ``DAV:propstat`` (:rfc:`6578#section-3.2`), and python-caldav returns it as an object with no ETag and no data. Every stored calendar object has a strong ETag (:rfc:`4791#section-5.3.4`), so a member without one was removed.

The adapter never sends ``DAV:limit`` (:rfc:`6578#section-3.7`): a server can apply it by dropping the remaining changes and returning its newest token.

Why a failed report resets the scope
------------------------------------

A server that won't accept a token answers with the ``DAV:valid-sync-token`` precondition (:rfc:`6578#section-3.2`), and the client falls back to a full sync, without downloading unchanged cached resources. :rfc:`4791#section-1.3` specifies 403 or 409 for a failed precondition, but tested servers also send 412, or 500 for a token they can't parse. python-caldav keeps the precondition only on a 403.

So any failed report for a token the server issued resets the scope, except a 403 without ``DAV:valid-sync-token``, which means access was refused and is raised. A full listing is always correct, and if it fails as well, its error is raised.

A tested server answers a token it no longer accepts with a report that leaves out older changes and raises no error. The adapter can't tell that from an ordinary report, so on such a server, schedule regular full listings.

Why content comes from a multiget, with its own ETag
----------------------------------------------------

Changed members are fetched with ``calendar-multiget`` (:rfc:`4791#section-7.9`), asking for ``DAV:getetag`` and ``calendar-data`` together, with no ``Depth`` header as that section asks. The stored version is the ETag from this response, not from the sync report: an object can change in between (:rfc:`6578#section-3.1`), and an older ETag would cause a needless fetch on the next sync and a wrong base for the next write.

A member that comes back 404, or is missing from the response although :rfc:`4791#section-7.9` requires an entry for every href, was removed after the report. It's left out of the change set, and the next report carries its removal.

Why writes are conditional
--------------------------

Updates and deletes send ``If-Match`` with the base ETag (:rfc:`9110#section-13.1.1`), and creates send ``If-None-Match: *`` (:rfc:`9110#section-13.1.2`). A mismatch is a 412, and the edit stays pending for the next ingest. Updates use python-caldav's ``save(increase_seqno=False)``, because ``save()`` increments ``SEQUENCE`` by default and the server wouldn't receive the edit you recorded. Deletes use ``delete(if_match=...)``.

A create uses an href of unreserved characters only (:rfc:`3986#section-2.3`), a UUID and ``.ics``, so it can't come back spelled differently. A server that already holds the UID answers with ``CALDAV:no-uid-conflict`` (:rfc:`4791#section-5.3.2.1`) or ``CALDAV:unique-scheduling-object-resource`` (:rfc:`6638#section-3.2.4.1`); the adapter checks for both before treating a 412 as a plain precondition failure.

A server MUST NOT return a strong ETag for data it stored differently (:rfc:`4791#section-5.3.4`), but tested servers do, so an applied push doesn't keep the ETag it got back.

Why the adapter needs a python-caldav newer than 3.4.0
------------------------------------------------------

The adapter relies on four python-caldav changes made after 3.4.0: following truncated sync reports, keeping a 403's preconditions, honoring ``increase_seqno`` on every save, and ``delete(if_match=...)``.

JMAP
====

Why a JMAP scope is one account and one data type
-------------------------------------------------

A JMAP ``state`` covers all records of one type in one account (:rfc:`8620#section-5.1`), so ``CalendarEvent`` and ``Task`` in one account are two scopes.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - calendaring-sync
     - JMAP
   * - Scope
     - One account and one data type
   * - Object id
     - The record's ``id``, unique per type per account (:rfc:`8620#section-1.6.3`)
   * - Version
     - None: JMAP has no per-record version
   * - Sync token
     - The type's ``state`` string
   * - Content and fingerprint
     - The JSCalendar JSON, and SHA-256 of it serialized with sorted keys
   * - Containers
     - The event's ``calendarIds``
   * - Logical key
     - ``uid`` plus ``recurrenceId``: one account may hold several events with one ``uid`` only when their ``recurrenceId`` values differ (draft-ietf-jmap-calendars-32 section 1.4.1)

Content stays JSCalendar, since converting to iCalendar would lose data on each round trip. Applications that want iCalendar can use calendaring-jmap's converters.

With no per-record version, every object ``/changes`` reports is fetched and compared by fingerprint, and a full listing fetches everything.

Why some errors reset the scope
-------------------------------

On ``cannotCalculateChanges`` the client MUST invalidate its cache for the type (:rfc:`8620#section-5.2`), so the adapter resets the scope. It also resets on two answers seen on a tested server: an ``oldState`` that doesn't echo the ``sinceState`` sent, and ``invalidArguments`` from ``/changes``. The adapter only sends states the server issued and a valid ``maxChanges``, so the state is the only argument that can be wrong.

Why pages follow the new state
------------------------------

When ``hasMoreChanges`` is true, the client may call ``/changes`` again with the ``newState`` it received (:rfc:`8620#section-5.2`). Each page is a truncated change set that advances the scope to its ``newState``.

Objects are fetched with ``/get`` after ``/changes``, so one can be newer than the ``newState`` it came with. The next ``/changes`` reports it again, and the matching fingerprint makes that a no-op.

A full listing reads the type's ``state`` first, then pages through ``/query`` and ``/get``. That state becomes the token, so anything changed during the listing appears in the next ``/changes``.

Why every create carries its own UID
------------------------------------

A server generates a ``uid`` only when a create omits it (draft-ietf-jmap-calendars-32 section 5.9). The adapter always sends one, so a create whose result was lost is recognized when it comes back under a server-assigned id.

Why writes use ifInState
------------------------

A ``/set`` with ``ifInState`` fails with ``stateMismatch`` if the type's state has moved (:rfc:`8620#section-5.3`). That state covers the whole account, so a rejection means something changed, not necessarily this object. The edits stay pending for the next ingest.

Why events come before tasks
----------------------------

Tasks fit the model as their own scope, but no tested JMAP server advertises ``urn:ietf:params:jmap:tasks``, and calendaring-jmap's task sync isn't released yet. The JMAP adapter starts with ``CalendarEvent`` and adds tasks once both are available.

Why the adapter waits on calendaring-jmap
-----------------------------------------

Paging and guarded writes need calendaring-jmap to return ``newState`` on a truncated ``/changes`` response instead of raising, and to support ``ifInState`` on writes. Accounts other than the primary one and a release with the task sync methods are needed too.
