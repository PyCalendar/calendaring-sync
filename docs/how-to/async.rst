.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

======================
Sync from asyncio code
======================

.. meta::
   :description: Sync with an asynchronous client from asyncio code, with the async variant of each adapter.

Each adapter that ships with calendaring-sync has an asynchronous variant for an asynchronous client, such as :class:`~calendaring_sync.adapters.caldav.AsyncCalDAVAdapter`. Its methods are the same, as coroutines. An adapter you write gets its variant from :class:`~calendaring_sync.adapters.base.AsyncAdapter`, as :doc:`write-an-adapter` shows.

.. literalinclude:: ../examples/async_sync.py
   :language: python
   :start-after: # start: sync
   :end-before: # end: sync

The adapter keeps the event loop free during a sync, as :class:`~calendaring_sync.adapters.base.AsyncAdapter` describes. The replica itself is synchronous: call its methods, such as :meth:`~calendaring_sync.replica.Replica.read_feed`, directly, or through :func:`asyncio.to_thread` when they read a lot.
