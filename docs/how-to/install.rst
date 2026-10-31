.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=======
Install
=======

.. meta::
   :description: Install calendaring-sync, and the extra for each adapter you use that ships with it.

calendaring-sync needs Python 3.11 or later:

.. code-block:: bash

    pip install calendaring-sync

The :term:`core` uses only the Python standard library. That's all you need to keep sync state with an :term:`adapter` of your own, as :doc:`write-an-adapter` shows.

The package ships its type hints (:pep:`561`), so a type checker reads them without a stub package.

Adapters that ship with calendaring-sync
========================================

Each adapter that ships with calendaring-sync needs its protocol's client library, which an extra of the same name installs:

.. list-table::
   :header-rows: 1

   * - Extra
     - Adapter
     - Installs
   * - ``caldav``
     - :class:`~calendaring_sync.adapters.caldav.CalDAVAdapter`
     - python-caldav, icalendar
   * - ``jmap``
     - :class:`~calendaring_sync.adapters.jmap.JMAPAdapter`
     - calendaring-jmap

Name the extras you use when you install:

.. code-block:: bash

    pip install "calendaring-sync[caldav]"

From source
===========

To try a change that isn't released yet, install from GitHub:

.. code-block:: bash

    pip install "calendaring-sync @ git+https://github.com/PyCalendar/calendaring-sync"

To work on calendaring-sync itself, set up a development environment as :doc:`../contribute` describes.
