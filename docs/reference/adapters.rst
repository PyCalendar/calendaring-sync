.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`adapters.caldav`: Syncing a CalDAV calendar
=================================================

.. meta::
   :description: CalDAVAdapter and AsyncCalDAVAdapter, which sync one CalDAV calendar collection with a replica through python-caldav.

An :term:`adapter` connects a :class:`~calendaring_sync.replica.Replica` to one kind of server. :class:`~calendaring_sync.adapters.caldav.CalDAVAdapter` syncs one CalDAV calendar collection through a python-caldav client, and :class:`~calendaring_sync.adapters.caldav.AsyncCalDAVAdapter` does the same through an asynchronous one. Install them with the ``caldav`` extra. :doc:`../explanation/adapters` explains every rule they follow, and :doc:`server-compatibility` how each tested server behaves.

.. automodule:: calendaring_sync.adapters.caldav
   :members:
   :inherited-members:
   :no-show-inheritance:
