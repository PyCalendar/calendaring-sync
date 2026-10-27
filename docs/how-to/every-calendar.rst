.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=================================
Sync every calendar in an account
=================================

.. meta::
   :description: Sync many scopes into one store, such as every calendar of an account, and drop the scopes of calendars the account no longer has.

One store holds as many :term:`scopes <scope>` as you sync. Where each calendar is a scope of its own, as with :class:`~calendaring_sync.adapters.caldav.CalDAVAdapter`, make an adapter for each calendar, sync each one, and drop the scopes of calendars that are gone.

.. literalinclude:: ../examples/every_calendar.py
   :language: python
   :start-after: # start: sync
   :end-before: # end: sync

Each calendar keeps its scope from run to run, through the adapter's default ``scope_id``. A calendar deleted on the server no longer appears in the account's list, so the last line drops its scope: :meth:`~calendaring_sync.replica.Replica.drop_scope` returns the edits it discards, which you can show or save. Keep one store for each account, so the scopes it drops are only that account's.

What a scope covers is each adapter's choice: :doc:`../explanation/adapters` gives it for each adapter that ships with calendaring-sync. Where one scope already covers the whole account, one adapter syncs it all.
