.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

====================================
Test code that uses calendaring-sync
====================================

.. meta::
   :description: Test the parts of your application that read the change feed and records, with no server, by ingesting the changes a server would send.

The parts of your application that read records and the :term:`change feed` don't need a server in their tests. Ingest what a server would send, and the replica reaches the state your code then reads.

.. literalinclude:: ../examples/testing.py
   :language: python
   :start-after: # start: server
   :end-before: # end: server

Each :class:`~calendaring_sync.objects.change_set.ChangeSet` is the :term:`change set` an adapter would hand the core. Your test then checks what your code made of it:

.. literalinclude:: ../examples/testing.py
   :language: python
   :start-after: # start: check
   :end-before: # end: check

To test your application's edits, and a server's answers to them, write a small adapter instead, as :doc:`write-an-adapter` shows. The examples on these pages run on one.
