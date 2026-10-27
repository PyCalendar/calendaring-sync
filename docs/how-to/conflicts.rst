.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=================
Resolve conflicts
=================

.. meta::
   :description: Resolve the conflicts a sync stored, one at a time or with a function that decides for each.

A sync stores a :term:`conflict` where the server changed an event you edited too, and carries on with the others. The event waits until you resolve it, and the next sync sends the result. In the examples below, another client moved the meeting while your application renamed it.

Resolve one conflict
====================

.. literalinclude:: ../examples/conflicts.py
   :language: python
   :start-after: # start: one
   :end-before: # end: one

:meth:`~calendaring_sync.replica.Replica.resolve` takes the record you read and a :data:`~calendaring_sync.objects.resolution.Resolution`. If the conflict changed since you read it, it raises :class:`~calendaring_sync.error.StaleConflictError`, so you never decide on news you haven't seen. Each class in :mod:`calendaring_sync.objects.resolution` says what it does.

Resolve every conflict with a function
======================================

.. literalinclude:: ../examples/conflicts.py
   :language: python
   :start-after: # start: merge
   :end-before: # end: merge

:meth:`~calendaring_sync.replica.Replica.resolve_conflicts` calls your function with each open conflict, and ``None`` leaves one open. A :class:`~calendaring_sync.objects.resolution.Merge` needs the fields the adapter reads from content, which :meth:`~calendaring_sync.adapters.base.Adapter.edit_fields` returns. :attr:`Record.conflict <calendaring_sync.objects.record.Record.conflict>` holds the server's side, and :class:`~calendaring_sync.objects.conflict.ConflictKind` says what each side did.
