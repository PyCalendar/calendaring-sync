.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

====================
Read the change feed
====================

.. meta::
   :description: Read the change feed from a cursor your application keeps, so it handles every change once, even after a crash.

The :term:`change feed` lists every change syncs made to the store, every conflict resolved and every scope dropped. Keep a :term:`cursor`, the number of the last entry you handled, and read the entries after it.

Read from a cursor
==================

.. literalinclude:: ../examples/change_feed.py
   :language: python
   :start-after: # start: read
   :end-before: # end: read

``handle`` and ``reload_everything`` are your application's. The example reads the cursor before a pass and saves it after each page. A crash between the two replays that page instead of skipping it, so make ``handle`` safe to call twice for one entry. Each part of your application that reads the feed keeps a cursor of its own.

The ``except`` branch handles a cursor older than what the feed keeps, as :class:`~calendaring_sync.error.TrimmedFeedError` describes.

Trim and purge
==============

.. literalinclude:: ../examples/change_feed.py
   :language: python
   :start-after: # start: trim
   :end-before: # end: trim

The feed and the removed records grow until you trim and purge them. Pass the lowest cursor any part of your application still reads from. :meth:`~calendaring_sync.replica.Replica.trim_feed` deletes the entries up to it, and :meth:`~calendaring_sync.replica.Replica.purge` deletes the records removed by then.
