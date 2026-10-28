.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

===================================
Mirror synced data in your database
===================================

.. meta::
   :description: A complete script that keeps a table in your application's own database up to date with every sync, from the change feed.

Your application often keeps its own table of events, in the shape its screens and queries need. This script keeps such a table up to date after every sync, from the :term:`change feed`. It works the same with any adapter.

.. literalinclude:: ../examples/mirror.py
   :language: python
   :start-after: # start: mirror
   :end-before: # end: mirror

The table and the :term:`cursor` live in one database, and each page of the feed updates both in one transaction. So a crash leaves the table matching the cursor, and the next run continues from there: no entry is handled twice, and none is skipped.

``apply`` reads the record each entry is about, since the record holds its latest synced copy, however many entries there were since the last run. A record that's removed, or that was :term:`purged <purge>` meanwhile, loses its row, and a dropped scope loses every row. Your own edits write no feed entries until the server has them, so the table holds each record's :term:`synced copy`. Read :attr:`Record.edit <calendaring_sync.objects.record.Record.edit>` as well where your application shows edits that aren't sent yet.

When the feed was trimmed past your cursor, :meth:`~calendaring_sync.replica.Replica.read_feed` raises :class:`~calendaring_sync.error.TrimmedFeedError`: rebuild the table from :meth:`~calendaring_sync.replica.Replica.records`, as :doc:`change-feed` shows.
