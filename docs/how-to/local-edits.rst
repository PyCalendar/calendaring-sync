.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

==================
Record local edits
==================

.. meta::
   :description: Record each change your application makes, so the next sync sends it to the server.

Record each change your application makes as you make it, and the next sync sends it.

Create and change
=================

.. literalinclude:: ../examples/local_edits.py
   :language: python
   :start-after: # start: record
   :end-before: # end: record

:meth:`~calendaring_sync.adapters.base.SyncAdapter.create` returns the new :term:`record`'s :term:`local key`, which stays the same once the server gives the object an id.

Undo an edit
============

.. literalinclude:: ../examples/local_edits.py
   :language: python
   :start-after: # start: undo
   :end-before: # end: undo

:meth:`~calendaring_sync.replica.Replica.discard` drops an edit the server hasn't received.

Delete
======

.. literalinclude:: ../examples/local_edits.py
   :language: python
   :start-after: # start: delete
   :end-before: # end: delete

An event with an open :term:`conflict` takes no edit until you resolve it, as :doc:`conflicts` shows.
