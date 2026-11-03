.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

===============
Security policy
===============

.. meta::
   :description: How to report a vulnerability in calendaring-sync, which versions get fixes, and the risks to know: what the store keeps, server data, connections, and stores you write.

Report a vulnerability using `GitHub's private vulnerability reporting <https://github.com/PyCalendar/calendaring-sync/security/advisories/new>`_ (the "Report a vulnerability" button on the Security tab), rather than a public issue.

Supported versions
==================

Security fixes target the latest release. There's no long-term support branch.

Dependencies
============

:doc:`how-to/install` lists what calendaring-sync and each of its extras depend on, and ``pyproject.toml`` gives the versions.

Known risks and design notes
============================

What the store keeps
--------------------

A :term:`store` keeps the content of every synced object, and your edits to it, as they are. Calendar data holds personal information, such as names, places and what a meeting is about. Each scope's id is in the store too, and an adapter's default scope id holds the server's address, without a login. The library never stores a password or another credential: those stay with the client you give the adapter. :class:`~calendaring_sync.store.sqlite.SQLiteStore` doesn't encrypt its file, so give it the permissions you give the calendar data itself, such as ``chmod 600``.

Data from the server
--------------------

The core never parses content: it stores and compares the bytes a server sent. The adapters that ship with calendaring-sync read what they need from it with their protocol's parsers. The CalDAV adapter reads iCalendar content lines with icalendar, and the JMAP adapter reads JSON with the standard library. python-caldav parses a server's XML error body with entity expansion and network access turned off, so a hostile server can't make it read local files or fetch other addresses.

Connections and credentials
---------------------------

calendaring-sync opens no connection of its own: the client library you give an adapter makes every request, with its own certificate checks and login. Point it at a server you trust, over HTTPS, as the client library's documentation describes.

Stores you write yourself
-------------------------

:class:`~calendaring_sync.store.memory.MemoryStore`'s description shows a store that saves its data with :mod:`pickle`. Loading a pickle runs code, so load such a file only if your application wrote it and no one else can change it.
