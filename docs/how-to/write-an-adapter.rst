.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

================
Write an adapter
================

.. meta::
   :description: Connect calendaring-sync to a new kind of server by subclassing the adapter base classes.

An :term:`adapter` connects calendaring-sync to one kind of server. You write how your protocol reads changes and sends one edit. The base classes in :mod:`calendaring_sync.adapters.base` hold everything else: the session, the order of a sync, claiming and settling each push, and resetting a scope whose token the server refused. The adapter below syncs a server that keeps its objects in memory, and the other how-to pages use it.

.. literalinclude:: ../examples/memory_adapter.py
   :language: python
   :lines: 4-

Write the protocol once
=======================

``_MemoryAdapter`` subclasses :class:`~calendaring_sync.adapters.base.Adapter`, sets the class attributes that describe what the server checks, and writes the methods its docstring lists. ``MemoryAdapter`` and ``AsyncMemoryAdapter`` add :class:`~calendaring_sync.adapters.base.SyncAdapter` or :class:`~calendaring_sync.adapters.base.AsyncAdapter`, which supply the public methods. The two differ only in the client they're given.

Each method that talks to the server yields every client call, as :mod:`calendaring_sync.adapters.base` describes, so ``yield from self._ask(self._server.listing)`` works with either client.

Read changes
============

:meth:`~calendaring_sync.adapters.base.Adapter._read` turns each answer into a :class:`~calendaring_sync.objects.change_set.ChangeSet`. Here an expired token ends the read with ``ReadResult.INVALID_TOKEN``. Requests about the scope go through :meth:`~calendaring_sync.adapters.base.Adapter._ask`, which raises :class:`~calendaring_sync.error.ScopeGoneError` for the error :meth:`~calendaring_sync.adapters.base.Adapter._gone` names: here, a server that lost the scope. Compute the fingerprint so it ignores what your server rewrites on its own, as :doc:`../explanation/design` explains.

Send edits
==========

:meth:`~calendaring_sync.adapters.base.Adapter._send` maps each answer your protocol gives to a :data:`~calendaring_sync.objects.push.PushOutcome`: here a version mismatch is :class:`~calendaring_sync.objects.push.Rejected`, and an object the server doesn't have is :class:`~calendaring_sync.objects.push.Gone`. Any other error propagates.

Test it
=======

Run the same steps through both variants, and compare the stores they leave. This project's tests do that for every adapter. :doc:`../explanation/adapters` describes how each adapter that ships with calendaring-sync maps its protocol, with the reason for each rule.
