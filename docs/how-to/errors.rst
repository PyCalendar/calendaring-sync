.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=============
Handle errors
=============

.. meta::
   :description: Catch the errors a sync raises, retry the ones that change nothing, and drop a scope the server no longer has.

Every error calendaring-sync raises on purpose derives from :class:`~calendaring_sync.error.CalendaringSyncError`, and :doc:`the error reference <../reference/errors>` says what each one means. A step of a sync that fails changes nothing, so you can always sync again, as :doc:`../reference/guarantees` lists.

.. literalinclude:: ../examples/errors.py
   :language: python
   :start-after: # start: sync
   :end-before: # end: sync

The client library's own errors, such as a refused login, reach you unchanged. Each adapter's class description names them.
