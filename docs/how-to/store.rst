.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=========================
Choose and set up a store
=========================

.. meta::
   :description: Keep sync state in a SQLite file with WAL, back it up and restore it, keep it in memory for tests, or in storage of your own.

A :term:`store` keeps every scope your application syncs. Use a SQLite file to keep sync state between runs, memory in tests, or your own storage where your application keeps everything else.

Keep sync state in SQLite
=========================

.. literalinclude:: ../examples/sqlite.py
   :language: python
   :start-after: # start: open
   :end-before: # end: open

Set the file's journal mode to WAL once, before you open it. With WAL, each edit and each push writes several times faster, which counts when your application records edits one at a time. :class:`~calendaring_sync.store.sqlite.SQLiteStore` says what else WAL changes, and why it doesn't suit a network filesystem. Several processes can share the file, as :doc:`parallel` shows.

Retry a busy store
==================

.. literalinclude:: ../examples/sqlite.py
   :language: python
   :start-after: # start: busy
   :end-before: # end: busy

``timeout`` sets how long :class:`~calendaring_sync.store.sqlite.SQLiteStore` waits for another process before it gives up.

.. _store-backup:

Back up and restore the store
=============================

Back up the store file together with everything your application derived from the :term:`change feed`, such as your :term:`cursor` and a mirrored table, as of the same moment. Copy the file while no process has it open, or use :meth:`sqlite3.Connection.backup`, which copies a consistent snapshot of a file in use. A plain copy of a file in use can miss the changes WAL keeps in a file beside it.

Restoring an older copy loses nothing on the server. The next sync asks the server for every change since the copy's :term:`sync token`, or lists the scope in full where the server no longer accepts it. An edit the copy still holds as pending, which reached the server after the copy was made, is matched to the server's copy before anything is sent. A create whose id the server picks has nothing to match by, so it goes out again. Where :term:`logical keys <logical key>` are unique, the server refuses it, and it becomes a :term:`conflict` for you to resolve; elsewhere, the server can end up holding it twice.

The restored feed continues from the copy, so it numbers new entries again from there, and a cursor saved after the copy was made skips entries without an error. If you restore the store alone, rebuild what you derived from the feed from :meth:`~calendaring_sync.replica.Replica.records`, and set your cursor to :meth:`~calendaring_sync.replica.Replica.feed_head`, as :doc:`change-feed` does after a trim.

Keep sync state in memory
=========================

:class:`~calendaring_sync.store.memory.MemoryStore` suits tests, as :doc:`testing` shows.

Keep sync state in your own storage
===================================

To keep sync state in a file of your own, give :class:`~calendaring_sync.store.memory.MemoryStore` a :class:`~calendaring_sync.store.memory.MemoryKeeper`. The store loads its data from the keeper once, when it opens, and hands it to the keeper each time a write commits:

.. literalinclude:: ../examples/keeper.py
   :language: python
   :start-after: # start: keeper
   :end-before: # end: keeper

Only load a file your application wrote: unpickling a file runs code from it. Every lookup scans the records in memory, so for a large store use SQLite.

For a database that looks records up itself, implement :class:`~calendaring_sync.store.base.Storage` and :class:`~calendaring_sync.store.base.StorageTransaction`, and pass the storage to :class:`~calendaring_sync.store.base.StateStore`, which does everything but the reading and writing.
