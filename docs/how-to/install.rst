.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=======
Install
=======

calendaring-sync has no release on PyPI yet, so install it from GitHub:

.. code-block:: bash

    pip install git+https://github.com/pycalendar/calendaring-sync

The CalDAV adapter needs python-caldav and icalendar, which the ``caldav`` extra installs:

.. code-block:: bash

    pip install "calendaring-sync[caldav] @ git+https://github.com/pycalendar/calendaring-sync"

To work on calendaring-sync itself, see :doc:`../contribute` for the development setup instead.

Without an extra, it has no runtime dependencies beyond the Python standard library. It needs Python 3.11 or newer.

The package ships inline type hints and a ``py.typed`` marker (:pep:`561`), so a type checker reads them directly without a separate stub package.
