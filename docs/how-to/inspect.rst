.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

==========================
Inspect and repair a scope
==========================

.. meta::
   :description: See what a scope's sync state holds, from its token to its unsent edits, and repair a scope that looks out of step with the server.

calendaring-sync keeps everything it knows about a :term:`scope` in the store, so you can read where a sync stands at any time, from any thread.

Inspect a scope
===============

.. literalinclude:: ../examples/inspect_scope.py
   :language: python
   :start-after: # start: inspect
   :end-before: # end: inspect

The :class:`~calendaring_sync.objects.scope.Scope` says how far the last sync got and when the last full listing finished. The records in each state show what's waiting on whom: :term:`pending edits <pending edit>` wait for the next sync, :term:`conflicts <conflict>` wait for you, and :meth:`~calendaring_sync.adapters.base.SyncAdapter.waiting` lists the edits the adapter can't send yet. The :term:`change feed` shows what each sync did, as :doc:`change-feed` shows.

Repair a scope
==============

.. literalinclude:: ../examples/inspect_scope.py
   :language: python
   :start-after: # start: repair
   :end-before: # end: repair

A :term:`full listing` compares every object with the server and repairs whatever an incremental sync missed. :meth:`~calendaring_sync.replica.Replica.reset` forgets the scope's :term:`sync token`, so its next sync is a full listing too. Neither touches your pending edits or open conflicts. To start a scope over entirely, :meth:`~calendaring_sync.replica.Replica.drop_scope` it, keep the edits it returns, and sync again.

After a server is restored from a backup, sync every scope once with a full listing. A token from after the backup is newer than anything the restored server issued, and some servers answer it as if nothing changed, as :doc:`../reference/server-compatibility` records.

To see the requests themselves, turn on your client library's logging.
