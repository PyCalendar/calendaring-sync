.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`~calendaring_sync.adapters`: Connecting to servers
========================================================

.. meta::
   :description: The adapter base classes, the CalDAV adapters built on python-caldav, and the JMAP adapters built on calendaring-jmap.

.. automodule:: calendaring_sync.adapters
   :no-members:

Base classes
------------

.. automodule:: calendaring_sync.adapters.base
   :members:
   :private-members: _read, _send, _waits, _new_object_id, _edit_fields, _gone, _ask

CalDAV
------

.. automodule:: calendaring_sync.adapters.caldav
   :members:
   :inherited-members:
   :no-show-inheritance:

JMAP
----

.. automodule:: calendaring_sync.adapters.jmap
   :members:
   :inherited-members:
   :no-show-inheritance:
