.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

===================
Sync a JMAP account
===================

.. meta::
   :description: Connect JMAPAdapter to the calendar events of a JMAP account.

:class:`~calendaring_sync.adapters.jmap.JMAPAdapter` syncs the calendar events of one JMAP account through a calendaring-jmap client.

.. literalinclude:: ../examples/jmap_sync.py
   :language: python
   :start-after: # start: sync
   :end-before: # end: sync

``client.get_account_id()`` returns the session's default account. To sync an account someone shared with you, pass its id instead. Each account is a :term:`scope` of its own.
