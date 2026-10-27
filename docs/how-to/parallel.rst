.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

======================================
Sync from several threads or processes
======================================

.. meta::
   :description: Sync several scopes at once from threads, share a store between processes, and retry when another process ingested first.

One :class:`~calendaring_sync.replica.Replica` serves every thread of a process, and a SQLite store serves every process on the machine.

Sync scopes at once
===================

.. literalinclude:: ../examples/parallel.py
   :language: python
   :start-after: # start: threads
   :end-before: # end: threads

Each adapter syncs its own scope, so they run side by side. The store runs one write at a time, with reads beside it. Sync a scope from one thread at a time: a second sync of the same scope in the same process raises :class:`~calendaring_sync.error.SessionError`. Your application's reads don't wait for a sync, as :class:`~calendaring_sync.store.base.StateStore` describes.

Share a store between processes
===============================

.. literalinclude:: ../examples/parallel.py
   :language: python
   :start-after: # start: retry
   :end-before: # end: retry

Each process opens the store file itself and makes its own replica. When two processes sync one scope at the same moment, the one that ingests second gets :class:`~calendaring_sync.error.StaleChangeSetError` and nothing changes, so syncing again is enough. :doc:`../explanation/design` explains why that's safe, and the rare case where one edit is sent twice.
