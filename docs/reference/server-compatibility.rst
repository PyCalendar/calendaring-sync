.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

====================
Server compatibility
====================

.. meta::
   :description: How each tested server behaves where sync depends on it, and what calendaring-sync's adapters do about each difference.

Each table lists the servers an adapter was checked against on 2026-10-11, with the version tested in its column heading. Each row records what that version did and what the adapter does about it; a later release may behave differently. The probes and logs behind every row are on the `evidence branch <https://github.com/pycalendar/calendaring-sync/tree/evidence/sync-design>`_, with the steps to reproduce them. :doc:`../explanation/adapters` explains each rule.

CalDAV
======

.. list-table::
   :header-rows: 1
   :widths: 18 11 11 11 11 11 27

   * - Behavior
     - Radicale 3.8.3
     - Xandikos 0.4.8
     - Cyrus 3.13.7
     - Stalwart 0.16.21
     - Baïkal 0.10.1
     - Adapter
   * - ``sync-collection`` listed in ``supported-report-set``
     - Yes
     - Yes
     - Yes
     - Yes
     - Yes
     - Checked before the first sync
   * - ``DAV:sync-token`` property (:rfc:`6578#section-4`)
     - Defined
     - Defined
     - Defined
     - Defined
     - Defined
     - Read before every full listing
   * - Report with an empty token, after deletions (:rfc:`6578#section-3.4`: no removed members)
     - Lists removed members as 404
     - As specified
     - As specified
     - Also lists the collection itself
     - As specified
     - Lists with ``PROPFIND``, never an empty token
   * - Member created and deleted between two reports (:rfc:`6578#section-3.5.2`: reported as removed)
     - Reported as removed
     - Not reported
     - Reported as removed
     - Not reported
     - Reported as removed
     - A create of yours that's never mentioned stays pending
   * - Member deleted and re-created at one href (:rfc:`6578#section-3.5.1`, :rfc:`6578#section-3.2`: reported once, as changed)
     - Once, as changed
     - Once, as changed
     - Once, as changed
     - Twice: removed, then changed
     - Once, as changed
     - The later entry counts, and the fetch decides
   * - Token the server never issued (:rfc:`6578#section-3.2`: ``valid-sync-token``)
     - 403 with ``valid-sync-token``
     - 412 with ``valid-sync-token`` for a well-formed token; 500 otherwise
     - 403 with ``valid-sync-token``
     - 207 with a full listing and no error
     - 403 with ``valid-sync-token``
     - Resets the scope on any failed report for an issued token, unless access was refused; schedule full listings on Stalwart
   * - Token whose history the server has discarded
     - 403 with ``valid-sync-token``
     - Not tested
     - 403 with ``valid-sync-token``
     - 207 with only the changes still in its history; earlier ones aren't reported
     - Not tested
     - Resets the scope; schedule full listings on Stalwart to recover changes it doesn't report
   * - Report with ``DAV:limit`` (:rfc:`6578#section-3.6`, :rfc:`6578#section-3.7`)
     - Ignores the limit
     - Returns the first results and its newest token, with no 507; the rest are lost
     - 507 with a checkpoint
     - 507 with a checkpoint; from an empty token, the checkpoint skips the remaining members
     - 507 with a checkpoint; ignores the limit on an empty token
     - Never sends ``DAV:limit``; follows a 507 the server applies itself
   * - PUT the server stores differently (:rfc:`4791#section-5.3.4`: no strong ETag in the answer)
     - Adds ``DTSTAMP``, reorders properties, returns an ETag
     - Reorders properties, returns an ETag
     - Stores what was sent
     - Stores what was sent
     - Adds ``DTSTAMP``, returns no ETag
     - Never keeps the ETag a write returns
   * - Identical PUT
     - Same ETag
     - Same ETag
     - Same ETag
     - Same ETag
     - Same ETag
     - A version-only change
   * - Create with a UID already in the calendar (:rfc:`4791#section-5.3.2.1`)
     - 409 with ``no-uid-conflict``
     - 412 with ``no-uid-conflict``
     - 403 with ``unique-scheduling-object-resource``
     - 412 with ``no-uid-conflict``
     - Accepted: two resources share the UID
     - Recognizes both elements; finding objects by UID returns every match
   * - ``DELETE`` with ``If-Match`` on a resource that's gone
     - 404
     - 404
     - 404
     - 404
     - 412
     - The next ingest settles it
   * - ``calendar-multiget`` for a member deleted since the report (:rfc:`4791#section-7.9`: an entry for every href)
     - 404 entry
     - 404 entry
     - 404 entry
     - 404 entry
     - Leaves the href out
     - A missing or 404 member waits for the next report

Conditional writes behaved as :rfc:`9110#section-13.1.1` and :rfc:`9110#section-13.1.2` specify on every server in the table: a wrong ``If-Match`` or an ``If-None-Match: *`` on an existing resource gets 412.

JMAP
====

.. list-table::
   :header-rows: 1
   :widths: 30 25 25 20

   * - Behavior
     - Cyrus 3.13.7
     - Stalwart 0.16.21
     - Adapter
   * - ``/changes`` with ``maxChanges`` (:rfc:`8620#section-5.2`)
     - Pages correctly; ``oldState`` echoed
     - Pages correctly; ``oldState`` echoed
     - Follows ``newState`` page by page
   * - Event created and destroyed between two states (:rfc:`8620#section-5.2`: should be left out)
     - Left out; listed as destroyed while paging
     - Left out
     - A create of yours that's never mentioned stays pending
   * - ``sinceState`` the server can't parse
     - ``cannotCalculateChanges``
     - ``invalidArguments``
     - Resets the scope on either
   * - ``sinceState`` the server never issued but can decode
     - ``cannotCalculateChanges``
     - Answers from a different state; ``oldState`` doesn't echo ``sinceState``
     - Resets the scope when ``oldState`` doesn't echo
   * - State whose history the server has discarded
     - ``cannotCalculateChanges``
     - ``cannotCalculateChanges``
     - Resets the scope
   * - ``ifInState`` on ``CalendarEvent/set`` (:rfc:`8620#section-5.3`)
     - ``stateMismatch`` when anything in the account changed
     - ``stateMismatch`` when anything in the account changed
     - Keeps the edits pending
   * - Properties the server adds to a stored event
     - ``created``, ``isDraft``, ``isOrigin``, ``prodId``, ``sequence``, ``updated``
     - ``isDraft``, ``isOrigin``, ``updated``
     - Stores the event as the server returns it
   * - Create without ``version`` (draft-ietf-jmap-calendars-32 section 5.9: the server sets it)
     - Refused with ``invalidProperties``
     - Accepted
     - Include ``version`` in events you create
   * - Create with a ``uid`` already in the account (draft-ietf-jmap-calendars-32 section 1.4.1)
     - ``invalidProperties`` on ``uid``
     - ``invalidProperties`` on ``uid``
     - A create collision
   * - Update or destroy of an event that's gone
     - ``notFound``
     - ``notFound``
     - The next ingest settles it
   * - JMAP Tasks (``urn:ietf:params:jmap:tasks``)
     - Not advertised
     - Not advertised, also on the latest image
     - Events only for now

No tested server reports a per-event version, so every event a ``/changes`` call names is fetched and compared by fingerprint.
