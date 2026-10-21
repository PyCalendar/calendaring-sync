.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`objects`: Change sets, records, edits, conflicts, push outcomes, resolutions, scopes, and feed entries
============================================================================================================

.. meta::
   :description: The data objects calendaring-sync takes and returns: change sets and their changes, records with their local edits and conflicts, push outcomes, resolutions, scopes, feed entries, and dropped scopes.

The data an :term:`adapter` hands the core, such as change sets and how each :term:`push` ended, the resolutions your application chooses for :term:`conflicts <conflict>`, and what the core returns.

.. automodule:: calendaring_sync.objects.change_set
   :members:

.. automodule:: calendaring_sync.objects.change
   :members:

.. automodule:: calendaring_sync.objects.record
   :members:

.. automodule:: calendaring_sync.objects.edit
   :members:

.. automodule:: calendaring_sync.objects.conflict
   :members:

.. automodule:: calendaring_sync.objects.push
   :members:

.. automodule:: calendaring_sync.objects.resolution
   :members:

.. automodule:: calendaring_sync.objects.scope
   :members:

.. automodule:: calendaring_sync.objects.feed
   :members:

.. automodule:: calendaring_sync.objects.dropped_scope
   :members:

See :doc:`errors` for the exception hierarchy.
