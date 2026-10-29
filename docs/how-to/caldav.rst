.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

======================
Sync a CalDAV calendar
======================

.. meta::
   :description: Connect CalDAVAdapter to a CalDAV calendar with the server's profile, and schedule full listings where the server needs them.

:class:`~calendaring_sync.adapters.caldav.CalDAVAdapter` syncs one calendar through a python-caldav client: every object in it, tasks and journal entries as well as events. :doc:`../tutorials/quickstart` shows a whole sync.

Connect with your server's profile
==================================

.. literalinclude:: ../examples/caldav_sync.py
   :language: python
   :start-after: # start: client
   :end-before: # end: client

Name your server in ``features`` when python-caldav has a profile for it, such as ``"radicale"`` or ``"stalwart"``. The profile saves a request for each event you create, and tells the adapter whether the server needs :term:`full listings <full listing>`. ``client.calendar(url=...)`` opens a calendar without a request; ``client.get_calendars()`` lists the account's calendars instead.

Schedule full listings
======================

.. literalinclude:: ../examples/caldav_sync.py
   :language: python
   :start-after: # start: listings
   :end-before: # end: listings

Some servers answer a :term:`sync token` they no longer accept with fewer changes, instead of refusing it. :attr:`~calendaring_sync.adapters.base.Adapter.needs_full_listings` is ``True`` for those, and a regular :term:`full listing` repairs what an incremental sync missed, at little cost, as :doc:`../explanation/adapters` explains. :doc:`../reference/server-compatibility` lists which tested servers need it.

Move from python-caldav's own sync
==================================

python-caldav's ``sync()`` keeps the :term:`sync token` only on the Python object, so a new process starts over unless you save the token yourself, and it stores nothing it returns. The adapter keeps the token, a copy of every object, and the :term:`change feed` in the store, and sends your edits too. Replace the ``sync()`` call with :meth:`adapter.sync() <calendaring_sync.adapters.base.SyncAdapter.sync>`, and read what changed from the feed instead of the two lists ``sync()`` returned, as :doc:`change-feed` shows.
