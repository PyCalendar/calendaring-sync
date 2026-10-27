.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

=============
How-to guides
=============

.. meta::
   :description: Guides for the tasks of an application that syncs calendar data with calendaring-sync, from connecting to a server to running in production.

Guides for one task each. :doc:`../tutorials/quickstart` comes first if you haven't synced a calendar yet. Many examples that need no server start from a ``replica`` that synced one event, ``meeting``, through the ``adapter`` that :doc:`write-an-adapter` builds; the others set up what they need themselves.

.. toctree::
   :maxdepth: 1
   :caption: Connect to a server

   install
   caldav
   jmap
   every-calendar
   async

.. toctree::
   :maxdepth: 1
   :caption: Work with synced data

   local-edits
   change-feed
   mirror
   conflicts

.. toctree::
   :maxdepth: 1
   :caption: Run in production

   store
   parallel
   errors
   inspect

.. toctree::
   :maxdepth: 1
   :caption: Build on calendaring-sync

   without-adapter
   write-an-adapter
   testing
