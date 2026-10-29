.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

====================
Server compatibility
====================

.. meta::
   :description: How each tested server behaves where sync depends on it, and what each adapter that ships with calendaring-sync does about it.

Each table lists how the tested servers behave where sync depends on it, and what the adapter does about it. Each section names the versions it was tested with; a later release may behave differently. Stalwart's entries were checked again on Stalwart 1.0.1 on 2026-10-29, the version the integration tests now run; where it behaves differently from 0.16, both are named. The probes and logs behind every row are on the `evidence branch <https://github.com/PyCalendar/calendaring-sync/tree/evidence/sync-design>`_, with the steps to reproduce them. :doc:`../explanation/adapters` explains each rule, and the :doc:`glossary` defines the sync terms in the Adapter column.

CalDAV
======

The CalDAV adapter was checked against Radicale 3.8.3, Xandikos 0.4.8, Cyrus 3.13.7, Stalwart 0.16.21 and Baïkal 0.10.1 between 2026-10-10 and 2026-10-12. Its integration tests ran against the same five on 2026-10-23, with python-caldav 3.6.0 and Xandikos 0.4.10, and the rows they added are marked with that date. Entries marked (tester) come from `caldav-server-tester <https://github.com/python-caldav/caldav-server-tester>`_, which ran these checks against 22 servers in October 2026; python-caldav publishes its results as compatibility hints (`python-caldav/caldav#822 <https://github.com/python-caldav/caldav/pull/822>`_).

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
       - CCS: also lists the collection itself (tester)
       - Others: as specified
     - Lists with ``PROPFIND``, never an empty token
   * - Member created and deleted between two reports (:rfc:`6578#section-3.5.2`: reported as removed)
     - - Xandikos: not reported
       - Stalwart 0.16.11: reported only when a member that existed at the earlier token also changed since (tester)
       - Bedework 5: not reported (tester)
       - Others: reported as removed
     - A create of yours that's never mentioned stays :term:`pending <pending edit>`
   * - Member deleted and re-created at one href (:rfc:`6578#section-3.5.1`, :rfc:`6578#section-3.2`: reported once, as changed)
     - - Stalwart: twice, removed and then changed
       - Nextcloud, ecloud, Synology: only as removed, though the member is there (tester)
       - Others: once, as changed
     - Fetches removed members too: one that comes back with data counts as changed
   * - ``sync-collection`` sent with ``Depth: 1``, as python-caldav up to 3.6.0 sends it (:rfc:`6578#section-3.2`: defined for ``Depth: 0`` only; 2026-10-23)
     - All: answered as with ``Depth: 0``
     - Relies on python-caldav's report
   * - Token the server never issued (:rfc:`6578#section-3.2`: ``valid-sync-token``)
     - - Radicale, Cyrus: 403 with ``valid-sync-token``
       - Xandikos: 412 with ``valid-sync-token`` for a well-formed token, 500 otherwise (`xandikos#743 <https://github.com/jelmer/xandikos/issues/743>`_)
       - Stalwart: 207 with a full listing and no error
       - Baïkal: 403 with ``valid-sync-token`` for a token that doesn't start with its own prefix; one that does, even past its newest token, gets no changes and comes back as the new token (2026-10-23)
     - :term:`Resets <reset>` the scope when the error names ``valid-sync-token``; flags Stalwart for regular full listings. Baïkal's answer can't be told from an ordinary one, so after restoring its database from a backup, run a full listing
   * - Token whose history the server has discarded
     - - Radicale, Cyrus, Stalwart 1.0.1: 403 with ``valid-sync-token``
       - Stalwart 0.16.21: 207 with only the changes still in its history; earlier ones aren't reported
       - Xandikos, Baïkal: not tested
     - Resets the scope; flags Stalwart for regular full listings, which recover what 0.16 doesn't report
   * - Report with ``DAV:limit`` (:rfc:`6578#section-3.6`, :rfc:`6578#section-3.7`)
     - - Radicale: ignores the limit
       - Xandikos: returns the first results and its newest token, with no 507; the rest are lost
       - Cyrus: 507 with a checkpoint
       - Stalwart 1.0.1: 507 with a checkpoint, from an empty token too
       - Stalwart 0.16.21: 507 with a checkpoint; from an empty token, the checkpoint skips the remaining members
       - Baïkal: 507 with a checkpoint; ignores the limit on an empty token
     - Never sends ``DAV:limit``; follows a 507 the server applies itself
   * - Full listing of a calendar larger than the server's result limit
     - - Stalwart: ``PROPFIND`` and an empty-token report both stop at its ``maxResults`` setting (2000 by default) with a 507. The report hands back a checkpoint that lists the rest on 1.0.1, and its newest token on 0.16.27. A ``calendar-multiget`` gets at most that many members and a 507 for the collection, even when it asked for no more (2026-10-23)
       - Others: not tested
       - python-caldav's own Stalwart test container sets the limit to 50 (tester)
     - Lists with ``PROPFIND``, and raises an error instead of ingesting a listing cut short; fetches at most 100 members per ``calendar-multiget``, so a setting above 100 never cuts a fetch
   * - PUT the server stores differently (:rfc:`4791#section-5.3.4`: no strong ETag in the answer)
     - - Radicale: adds ``DTSTAMP``, reorders properties, returns an ETag
       - Xandikos: reorders properties, returns an ETag
       - Baïkal: adds ``DTSTAMP``, returns no ETag
       - Cyrus, Stalwart: store what was sent
       - CCS, Infomaniak: reorder properties; SOGo adds ``CLASS``; Zimbra and Bedework 5 add properties; all return a strong ETag (tester)
     - Never keeps the ETag a write returns; its :term:`fingerprint` ignores the rewrite
   * - Character set of calendar data (:rfc:`5545#section-3.1.4`: UTF-8 by default)
     - - Xandikos: ``text/calendar`` with no charset
       - Others: ``charset=utf-8``
     - Reads calendar data as UTF-8 unless the server names another charset
   * - Identical PUT
     - All: same ETag
     - A version-only change
   * - Create with a UID already in the calendar (:rfc:`4791#section-5.3.2.1`)
     - - Radicale: 409 with ``no-uid-conflict``
       - Xandikos, Stalwart 0.16.21: 412 with ``no-uid-conflict``
       - Stalwart 1.0.1: 403 with ``no-uid-conflict``
       - Cyrus: 403 with ``unique-scheduling-object-resource``, also for a UID that another of the user's calendars holds (2026-10-23)
       - Baïkal: accepted, so two resources share the UID
     - Recognizes both elements; finding objects by UID returns every match. Without the server's profile on the client, python-caldav's search for the UID refuses the create first, on Baïkal too
   * - ``DELETE`` with an ``If-Match`` that no longer matches (:rfc:`9110#section-13.1.1`: 412)
     - - Bedework 3 and 5, Zimbra, Posteo, SOGo: deleted anyway (tester)
       - Others: 412
     - Nothing it can do: there, a delete can remove a change made since your last sync
   * - ``DELETE`` with ``If-Match`` on a resource that's gone
     - - Baïkal: 412
       - Nextcloud, Davis, CCS, OX, Purelymail, Robur, Infomaniak: 412 (tester)
       - Others: 404
     - The next :term:`ingest` settles it
   * - ``calendar-multiget`` for a member deleted since the report (:rfc:`4791#section-7.9`: an entry for every href)
     - - Baïkal: leaves the href out
       - Nextcloud, DAViCal, Davis, Infomaniak: leave the href out (tester)
       - Posteo: 404 for the whole ``calendar-multiget`` (tester)
       - Others: a 404 entry, which python-caldav returns as an object without data
     - A missing member, or one without data, waits for the next report; after a 404 for the whole request, checks that the calendar is there and fetches each member on its own
   * - Removed member in a sync report
     - - Infomaniak: adds an empty ``propstat`` with 418, on which python-caldav 3.6.0 raises; fixed for its next release (`python-caldav/caldav#821 <https://github.com/python-caldav/caldav/pull/821>`_) (tester)
       - Others: as specified
     - Syncs Infomaniak from python-caldav's next release

Conditional writes behaved as :rfc:`9110#section-13.1.1` and :rfc:`9110#section-13.1.2` specify on every server in the table: a wrong ``If-Match`` or an ``If-None-Match: *`` on an existing resource gets 412.

JMAP
====

The JMAP adapter was checked against Cyrus 3.13.7 and Stalwart 0.16.21 between 2026-10-10 and 2026-10-12. Its integration tests ran against Cyrus 3.13.7 and Stalwart 0.16.27 on 2026-10-25, with calendaring-jmap before its 2.0.0 release, and the rows they added are marked with that date.

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
   * - ``sinceState`` the server never issued but can decode (2026-10-25)
     - - Cyrus: a state past its newest is echoed in ``oldState`` and answered with no changes and the newest state
       - Stalwart 1.0.1: ``invalidArguments`` for a state with a character changed; a state with characters added is answered from a different state, and ``oldState`` doesn't echo ``sinceState``
       - Stalwart 0.16.27: answers from a different state; ``oldState`` doesn't echo ``sinceState``
     - Resets the scope when ``oldState`` doesn't echo. Cyrus' answer looks like a real one, so a scope whose token is newer than the server's, such as after the server is restored from a backup, misses what changed; list it in full then
   * - State whose history the server has discarded
     - Both: ``cannotCalculateChanges``
     - Resets the scope
   * - ``ifInState`` on ``CalendarEvent/set`` (:rfc:`8620#section-5.3`)
     - Both: ``stateMismatch`` when anything in the account changed
     - Keeps the edits pending
   * - Properties the server adds to a stored event
     - - Cyrus: ``created``, ``isDraft``, ``isOrigin``, ``prodId``, ``sequence``, ``updated``
       - Stalwart 1.0.1: ``created``, ``isDraft``, ``isOrigin``, ``updated``, ``version``, and members holding their default: ``hideAttendees``, ``mayInviteSelf``, ``mayInviteOthers``, ``useDefaultAlerts``; ``sequence`` after an update
       - Stalwart 0.16.21: ``isDraft``, ``isOrigin``, ``updated``
     - Stores the event as the server returns it; its :term:`fingerprint` leaves those properties out
   * - What else the server changes in an event it stores (2026-10-25)
     - - Cyrus: leaves out members holding their default, such as an alert's ``action`` of ``display`` or a rule's ``interval`` of 1
       - Stalwart 1.0.1: leaves out a ``@type`` its place implies and ``showWithoutTime`` when it's ``false`` (2026-10-29)
       - Stalwart 0.16.27: leaves out a ``@type`` its place implies, ``version`` and ``useDefaultAlerts``; adds ``updated`` inside each recurrence override, changed on every write, and ``iCalendar`` to a location
       - Cyrus, Stalwart 0.16.27: drop a participant's ``attendee`` role
     - The fingerprint leaves out defaults, implied types, ``iCalendar``, an override's server-set members and the ``attendee`` role, so your own write isn't news
   * - Create with participants and no ``organizerCalendarAddress`` (draft-ietf-jmap-calendars-33 section 5.9: the server sets it) (2026-10-25)
     - Both: set it to the account's address, and add that address as a participant with the ``owner`` role
     - Reports the event changed at the next sync, once
   * - Create without ``version`` (draft-ietf-jmap-calendars-33 section 5.9: the server sets it)
     - - Cyrus: refused with ``invalidProperties``
       - Stalwart: accepted
     - Include ``version`` in events you create
   * - Create with a ``uid`` already in the account (draft-ietf-jmap-calendars-33 section 1.4.1)
     - Both: ``invalidProperties`` on ``uid``
     - Stored as a :term:`conflict record`, refused for its UID
   * - Create with a ``uid`` another event holds, but another ``recurrenceId`` (draft-ietf-jmap-calendars-33 section 1.4.1: allowed) (2026-10-25)
     - - Cyrus: accepted, as is one with the same ``uid`` and no ``recurrenceId``, which the draft doesn't allow (2026-10-29)
       - Stalwart 1.0.1: in another calendar, accepted; in the same calendar, ``invalidProperties`` on ``calendarIds``, as for a calendar that doesn't exist (2026-10-29)
       - Stalwart 0.16.27: ``invalidProperties`` on ``uid``
     - Stored as a conflict record where it's refused; a refusal on ``calendarIds`` counts only when another event with that ``uid`` is in one of the event's calendars, and is raised otherwise
   * - Update sending another ``uid`` (2026-10-25)
     - - Both: accepted, and the event keeps its own ``uid``
       - Cyrus: afterwards no longer refuses a create with the ``uid`` that was sent, even once both events are deleted
     - The next sync stores the event with the ``uid`` the server kept
   * - Event in two calendars (``calendarIds`` with two entries) (2026-10-25)
     - - Cyrus: ``invalidProperties`` on ``calendarIds``
       - Stalwart: stored in both
     - Raises the refusal; the push stays in flight until the next sync settles it
   * - Update or destroy of an event that's gone
     - Both: ``notFound``
     - A change becomes a conflict and a delete is done; with ``ifInState``, another client's delete is refused for the moved state first, and the next ingest reports the removal
   * - ``CalendarEvent/query`` paged with ``position`` and ``limit`` (:rfc:`8620#section-5.5`) (2026-10-25)
     - - Cyrus: right pages, but every answer says ``position`` 0
       - Stalwart: right pages and positions
     - Counts the position itself; a listing reads the state again before its last page
   * - Most objects one ``/get`` returns (``maxObjectsInGet``) (2026-10-25)
     - - Cyrus: 4096
       - Stalwart: 500
     - Asks for 100 at a time
   * - JMAP Tasks (``urn:ietf:params:jmap:tasks``)
     - Both: not advertised, Stalwart also not on its latest image
     - Events only for now

No tested server reports a per-event version, so every event a ``/changes`` call names is fetched and compared by :term:`fingerprint`.
