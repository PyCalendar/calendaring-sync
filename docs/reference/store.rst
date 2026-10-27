.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`~calendaring_sync.store`: Where sync state lives
======================================================

.. meta::
   :description: StateStore, the storage interface, and its implementations: SQLiteStore for real use and MemoryStore for tests.

.. automodule:: calendaring_sync.store
   :no-members:

.. automodule:: calendaring_sync.store.base
   :members:
   :private-members: _begin, _commit, _rollback, _scope_records_in, _store_error, _writing, _reads_beside_writes

.. automodule:: calendaring_sync.store.memory
   :members:

.. automodule:: calendaring_sync.store.sqlite
   :members:
