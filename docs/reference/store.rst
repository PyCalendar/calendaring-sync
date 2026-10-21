.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`store`: Where sync state lives
====================================

.. meta::
   :description: StateStore, the storage interface, and its two implementations: MemoryStore, for tests or as the base of a store that saves its objects elsewhere, and SQLiteStore for real use.

A :term:`store` keeps every :term:`scope`, its records, and the :term:`change feed`. Use :class:`~calendaring_sync.store.sqlite.SQLiteStore` to keep state between runs, and :class:`~calendaring_sync.store.memory.MemoryStore` in tests. To save the objects themselves somewhere else, such as with :mod:`pickle` or in an object database, subclass ``MemoryStore`` as its documentation shows.

.. automodule:: calendaring_sync.store.base
   :members:

.. automodule:: calendaring_sync.store.memory
   :members:

.. automodule:: calendaring_sync.store.sqlite
   :members:
