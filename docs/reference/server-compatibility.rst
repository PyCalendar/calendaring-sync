.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

====================
Server compatibility
====================

.. meta::
   :description: How each tested server behaves where sync depends on it, and what calendaring-sync's adapters do about each difference.

Each table lists what the tested servers did where sync depends on it, and what the adapter does about it. The CalDAV adapter was checked against Radicale 3.8.3, Xandikos 0.4.8, Cyrus 3.13.7, Stalwart 0.16.21 and Baïkal 0.10.1, and the JMAP adapter against Cyrus 3.13.7 and Stalwart 0.16.21, between 2026-10-10 and 2026-10-12. A later release may behave differently. The probes and logs behind every row are on the `evidence branch <https://github.com/pycalendar/calendaring-sync/tree/evidence/sync-design>`_, with the steps to reproduce them. :doc:`../explanation/adapters` explains each rule, and the :doc:`glossary` defines the sync terms in the Adapter column.

CalDAV
======

.. list-table::
   :header-rows: 1
   :widths: 34 36 30

   * - Behavior
     - Tested servers
     - Adapter
   * - ``sync-collection`` listed in ``supported-report-set``
     - All: yes
     - Checked before the first sync
   * - ``DAV:sync-token`` property (:rfc:`6578#section-4`)
     - All: defined
     - Read before every :term:`full listing`
   * - Report with an empty token, after deletions (:rfc:`6578#section-3.4`: no removed members)
     - - Radicale: lists removed members as 404
       - Stalwart: also lists the collection itself
       - Others: as specified
     - Lists with ``PROPFIND``, never an empty token
   * - Member created and deleted between two reports (:rfc:`6578#section-3.5.2`: reported as removed)
     - - Xandikos, Stalwart: not reported
       - Others: reported as removed
     - A create of yours that's never mentioned stays :term:`pending <pending edit>`
   * - Member deleted and re-created at one href (:rfc:`6578#section-3.5.1`, :rfc:`6578#section-3.2`: reported once, as changed)
     - - Stalwart: twice, removed and then changed
       - Others: once, as changed
     - The later entry counts, and the fetch decides
   * - Token the server never issued (:rfc:`6578#section-3.2`: ``valid-sync-token``)
     - - Radicale, Cyrus, Baïkal: 403 with ``valid-sync-token``
       - Xandikos: 412 with ``valid-sync-token`` for a well-formed token, 500 otherwise (`xandikos#743 <https://github.com/jelmer/xandikos/issues/743>`_)
       - Stalwart: 207 with a full listing and no error
     - :term:`Resets <reset>` the scope when the error names ``valid-sync-token``; flags Stalwart for regular full listings
   * - Token whose history the server has discarded
     - - Radicale, Cyrus: 403 with ``valid-sync-token``
       - Stalwart: 207 with only the changes still in its history; earlier ones aren't reported
       - Xandikos, Baïkal: not tested
     - Resets the scope; flags Stalwart for regular full listings, which recover what it doesn't report
   * - Report with ``DAV:limit`` (:rfc:`6578#section-3.6`, :rfc:`6578#section-3.7`)
     - - Radicale: ignores the limit
       - Xandikos: returns the first results and its newest token, with no 507; the rest are lost
       - Cyrus: 507 with a checkpoint
       - Stalwart: 507 with a checkpoint; from an empty token, the checkpoint skips the remaining members
       - Baïkal: 507 with a checkpoint; ignores the limit on an empty token
     - Never sends ``DAV:limit``; follows a 507 the server applies itself
   * - Full listing of a calendar larger than the server's result limit
     - - Stalwart: ``PROPFIND`` and an empty-token report both stop at its ``maxResults`` setting (2000 by default) with a 507; the report also hands back its newest token
       - Others: not tested
     - Lists with ``PROPFIND``, and raises an error instead of ingesting a listing cut short
   * - PUT the server stores differently (:rfc:`4791#section-5.3.4`: no strong ETag in the answer)
     - - Radicale: adds ``DTSTAMP``, reorders properties, returns an ETag
       - Xandikos: reorders properties, returns an ETag
       - Baïkal: adds ``DTSTAMP``, returns no ETag
       - Cyrus, Stalwart: store what was sent
     - Never keeps the ETag a write returns
   * - Identical PUT
     - All: same ETag
     - A version-only change
   * - Create with a UID already in the calendar (:rfc:`4791#section-5.3.2.1`)
     - - Radicale: 409 with ``no-uid-conflict``
       - Xandikos, Stalwart: 412 with ``no-uid-conflict``
       - Cyrus: 403 with ``unique-scheduling-object-resource``
       - Baïkal: accepted, so two resources share the UID
     - Recognizes both elements; finding objects by UID returns every match
   * - ``DELETE`` with ``If-Match`` on a resource that's gone
     - - Baïkal: 412
       - Others: 404
     - The next :term:`ingest` settles it
   * - ``calendar-multiget`` for a member deleted since the report (:rfc:`4791#section-7.9`: an entry for every href)
     - - Baïkal: leaves the href out
       - Others: a 404 entry
     - A missing or 404 member waits for the next report

Conditional writes behaved as :rfc:`9110#section-13.1.1` and :rfc:`9110#section-13.1.2` specify on every server in the table: a wrong ``If-Match`` or an ``If-None-Match: *`` on an existing resource gets 412.

JMAP
====

.. list-table::
   :header-rows: 1
   :widths: 34 36 30

   * - Behavior
     - Tested servers
     - Adapter
   * - ``/changes`` with ``maxChanges`` (:rfc:`8620#section-5.2`)
     - Both: pages correctly, ``oldState`` echoed
     - Follows ``newState`` page by page
   * - Event created and destroyed between two states (:rfc:`8620#section-5.2`: should be left out)
     - - Cyrus: left out, but listed as destroyed while paging
       - Stalwart: left out
     - A create of yours that's never mentioned stays :term:`pending <pending edit>`
   * - ``sinceState`` the server can't parse
     - - Cyrus: ``cannotCalculateChanges``
       - Stalwart: ``invalidArguments``
     - :term:`Resets <reset>` the scope on either
   * - ``sinceState`` the server never issued but can decode
     - - Cyrus: ``cannotCalculateChanges``
       - Stalwart: answers from a different state; ``oldState`` doesn't echo ``sinceState``
     - Resets the scope when ``oldState`` doesn't echo
   * - State whose history the server has discarded
     - Both: ``cannotCalculateChanges``
     - Resets the scope
   * - ``ifInState`` on ``CalendarEvent/set`` (:rfc:`8620#section-5.3`)
     - Both: ``stateMismatch`` when anything in the account changed
     - Keeps the edits pending
   * - Properties the server adds to a stored event
     - - Cyrus: ``created``, ``isDraft``, ``isOrigin``, ``prodId``, ``sequence``, ``updated``
       - Stalwart: ``isDraft``, ``isOrigin``, ``updated``
     - Stores the event as the server returns it
   * - Create without ``version`` (draft-ietf-jmap-calendars-32 section 5.9: the server sets it)
     - - Cyrus: refused with ``invalidProperties``
       - Stalwart: accepted
     - Include ``version`` in events you create
   * - Create with a ``uid`` already in the account (draft-ietf-jmap-calendars-32 section 1.4.1)
     - Both: ``invalidProperties`` on ``uid``
     - Stored as a :term:`conflict record`, refused for its UID
   * - Update or destroy of an event that's gone
     - Both: ``notFound``
     - The next :term:`ingest` settles it
   * - JMAP Tasks (``urn:ietf:params:jmap:tasks``)
     - Both: not advertised, Stalwart also not on its latest image
     - Events only for now

No tested server reports a per-event version, so every event a ``/changes`` call names is fetched and compared by :term:`fingerprint`.
