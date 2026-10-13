.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`replica`: Sessions, ingesting, and the change feed
========================================================

.. meta::
   :description: Replica and Session, where calendaring-sync applies change sets, keeps scopes, and serves the change feed.

:class:`~calendaring_sync.replica.Replica` is the entry point. Open a :term:`session` for a :term:`scope`, :term:`ingest` the :term:`change sets <change set>` an :term:`adapter` fetched, then read the :term:`change feed` to learn what changed.

.. automodule:: calendaring_sync.replica
   :members:
