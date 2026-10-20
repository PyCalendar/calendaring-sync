.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

:mod:`replica`: Sessions, ingesting, local edits, pushing, resolving conflicts, and the change feed
===================================================================================================

.. meta::
   :description: Replica and Session, where calendaring-sync applies change sets, records local edits, pushes them, stores and resolves conflicts, and serves the change feed.

:class:`~calendaring_sync.replica.Replica` is the entry point. Record your :term:`local edits <local edit>` as you make them. To sync, open a :term:`session` for a :term:`scope` and :term:`ingest` the :term:`change sets <change set>` an :term:`adapter` fetched. After the last page, the adapter pushes your edits in the same session: it :term:`claims <claim>` each one, sends it, and settles it with how the :term:`push` ended. Then read the :term:`change feed` to learn what changed and which :term:`conflicts <conflict>` came up, and resolve them with :meth:`~.Replica.resolve` or :meth:`~.Replica.resolve_conflicts`.

.. automodule:: calendaring_sync.replica
   :members:
