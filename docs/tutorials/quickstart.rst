.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

==========
Quickstart
==========

.. meta::
   :description: Sync one calendar into a SQLite file and list what the sync brought in.

This tutorial syncs one calendar from a CalDAV server into a SQLite file, then lists what the sync brought in. You need an account on a CalDAV server with at least one calendar, and calendaring-sync installed with the ``caldav`` extra, as :doc:`../how-to/install` shows.

Sync a calendar
===============

.. literalinclude:: ../examples/quickstart.py
   :language: python
   :start-after: # start: sync
   :end-before: # end: sync

Replace the URL, username, and password with your own, then run the script. It prints a line for each event in your first calendar, such as ``changed /calendars/alice/work/meeting.ics``.

Change an event in your calendar app and run the script again. This sync fetches only that event, and the feed gains one entry for it.

What happened
=============

:class:`~calendaring_sync.store.sqlite.SQLiteStore` keeps the sync state in ``sync.db``, so each run continues where the last one stopped. On the first run, :class:`~calendaring_sync.adapters.caldav.CalDAVAdapter` listed the calendar in full and stored each event as a :term:`record`. Later runs ask the server only for what changed since the :term:`sync token` it stored. Every change also went into the :term:`change feed`, which :meth:`~calendaring_sync.replica.Replica.read_feed` returns from its first entry.

Only the lines that open the client and create the adapter are about CalDAV. With any other :term:`adapter`, including one you write, the rest of the script stays the same.

Next steps
==========

- :doc:`../how-to/every-calendar` to sync every calendar in an account.
- :doc:`../how-to/change-feed` to read only the entries you haven't handled yet, and :doc:`../how-to/mirror` to keep your own table up to date with them.
- :doc:`../how-to/local-edits` to change events and send the changes to the server.
- :doc:`../how-to/conflicts` for an event both sides changed.
- :doc:`../explanation/when-to-use` to see what calendaring-sync is for, and what it isn't.
