.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`replica`: Sessions, ingesting, local edits, and the change feed
=====================================================================

.. meta::
   :description: Replica and Session, where calendaring-sync applies change sets, records local edits, stores conflicts, and serves the change feed.

:class:`~calendaring_sync.replica.Replica` is the entry point. Record your :term:`local edits <local edit>` as you make them. To sync, open a :term:`session` for a :term:`scope` and :term:`ingest` the :term:`change sets <change set>` an :term:`adapter` fetched. Then read the :term:`change feed` to learn what changed and which :term:`conflicts <conflict>` came up.

.. automodule:: calendaring_sync.replica
   :members:
