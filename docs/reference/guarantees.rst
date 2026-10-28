.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=====================
Guarantees and limits
=====================

.. meta::
   :description: What calendaring-sync guarantees about your data, edits and changes, and the limits it has, each linked to the reason behind it.

What the :term:`core` guarantees, whichever :term:`adapter` and :term:`store` you use, and the limits it has. Each row links to the reason behind it.

Guarantees
==========

.. list-table::
   :header-rows: 1
   :widths: 75 25

   * - Guarantee
     - Why
   * - A change set is applied whole or not at all, and the :term:`sync token` moves only with it.
     - :ref:`design-change-sets`
   * - A sync that stops at any point loses nothing: the next one starts from the last :term:`sync token` the store saved.
     - :ref:`design-change-sets`
   * - A change set fetched from an outdated token is refused, so two processes never apply the same changes twice.
     - :ref:`design-change-sets`
   * - Nothing is pushed until every page of a sync is ingested.
     - :ref:`design-reads-first`
   * - Two processes never :term:`claim` the same push at once.
     - :ref:`design-reads-first`
   * - A :term:`pending edit` stays until the server applies it. A rejected push, or one whose answer never arrived, doesn't discard it.
     - :ref:`design-server-answer`
   * - An object the server removed while you edited it is always a :term:`conflict`, never resolved for you.
     - :ref:`design-conflicts`
   * - A conflict that changed since you read it can't be resolved, so a decision never applies to changes you haven't seen.
     - :ref:`design-conflicts`
   * - A server's copy of your own write isn't reported as a change, however the server rewrote it.
     - :ref:`design-fingerprints`
   * - Everything a sync changes, every conflict you resolve and every scope you drop reaches the :term:`change feed` in the same transaction, numbered in order, and a removal stays readable until you :term:`purge` it. Your own edit reaches it when its push ends.
     - :ref:`design-feed`
   * - A scope the server no longer has is never emptied for you. Dropping it returns the edits and conflicts it discards.
     - :ref:`design-dropped-scope`
   * - In the stores that ship, a read in another thread doesn't wait for a sync's write, at most for its commit, and sees the store as it was before the write began. A store over a storage of your own does the same where the storage sets :attr:`~calendaring_sync.store.base.Storage.reads_beside_writes`.
     - :ref:`design-storage`
   * - Every store checks arguments and raises errors the same way, since the checks live in the base class.
     - :class:`~calendaring_sync.store.base.StateStore`

Limits
======

.. list-table::
   :header-rows: 1
   :widths: 75 25

   * - Limit
     - Why
   * - An edit that changes only values a server stamps on its own, such as when an object was last saved, isn't pushed.
     - :ref:`design-fingerprints`
   * - If two processes sync a scope at once, both can send the same edit. A guarded change or delete is then rejected, but a create whose id the server picks can exist twice where the adapter doesn't treat logical keys as unique.
     - :ref:`design-reads-first`
   * - After a push is applied, the next sync fetches that object from the server once more.
     - :ref:`design-server-answer`
   * - A full listing compares every object, and an adapter whose protocol has no :term:`version` for each object fetches every one.
     - :ref:`design-invalid-tokens`
   * - A replica runs one sync of a scope at a time, and a second raises :class:`~calendaring_sync.error.SessionError`.
     - :ref:`design-reads-first`
   * - Store calls are synchronous. The asynchronous adapters run them in a worker thread.
     - :ref:`design-storage`
   * - A store restored from an older copy numbers new feed entries again from the copy's point, so a cursor saved after the copy skips entries.
     - :ref:`store-backup`
   * - calendaring-sync logs nothing. Your client library's logging shows the requests an adapter sends.
     - :doc:`../how-to/inspect`

Each adapter's own limits, and how each tested server behaves, are on :doc:`../explanation/adapters` and :doc:`server-compatibility`.
