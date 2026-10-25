.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`adapters`: Syncing a CalDAV calendar or a JMAP account
============================================================

.. meta::
   :description: The CalDAV adapters, which sync one calendar collection through python-caldav, and the JMAP adapters, which sync one account's events through calendaring-jmap.

An :term:`adapter` connects a :class:`~calendaring_sync.replica.Replica` to one kind of server. Each comes in a synchronous and an asynchronous variant, takes the matching client, and is installed with an extra of its own. :doc:`../explanation/adapters` explains every rule they follow, and :doc:`server-compatibility` how each tested server behaves.

CalDAV
------

:class:`~calendaring_sync.adapters.caldav.CalDAVAdapter` syncs one CalDAV calendar collection through a python-caldav client, and :class:`~calendaring_sync.adapters.caldav.AsyncCalDAVAdapter` does the same through an asynchronous one. Install them with the ``caldav`` extra.

.. automodule:: calendaring_sync.adapters.caldav
   :members:
   :inherited-members:
   :no-show-inheritance:

JMAP
----

:class:`~calendaring_sync.adapters.jmap.JMAPAdapter` syncs one JMAP account's calendar events through a calendaring-jmap client, and :class:`~calendaring_sync.adapters.jmap.AsyncJMAPAdapter` does the same through an asynchronous one. Install them with the ``jmap`` extra.

.. automodule:: calendaring_sync.adapters.jmap
   :members:
   :inherited-members:
   :no-show-inheritance:
