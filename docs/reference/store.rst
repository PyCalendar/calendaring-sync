.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`~calendaring_sync.store`: Where sync state lives
======================================================

.. meta::
   :description: StateStore, the storage interface it keeps its data in, and the stores that ship: SQLiteStore, and MemoryStore with an optional keeper.

.. automodule:: calendaring_sync.store
   :no-members:

.. automodule:: calendaring_sync.store.base
   :members:

.. automodule:: calendaring_sync.store.memory
   :members:
   :exclude-members: MemoryData

.. autoclass:: calendaring_sync.store.memory.MemoryData()

.. automodule:: calendaring_sync.store.sqlite
   :members:
