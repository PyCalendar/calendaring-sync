.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

==============
When to use it
==============

.. meta::
   :description: What calendaring-sync is for, what it leaves to your application, and how it compares with vdirsyncer and pimsync.

calendaring-sync is for a Python application that keeps and edits its own copy of calendar data from a server. Examples are a calendar app that works offline, a service that mirrors events into its database, and a tool that changes events in bulk. It remembers how far each sync got, tells your application what changed, keeps your application's edits until a sync sends them, and stores a :term:`conflict` where both sides changed the same event.

What it leaves to you
=====================

calendaring-sync is a library, not a sync tool. Your application decides when to sync, how to show and convert events, and which side of a conflict wins; :doc:`design` gives the reason for each. :doc:`../reference/guarantees` lists what it promises in return, and its limits.

Compared with vdirsyncer and pimsync
====================================

vdirsyncer is a command line tool, written in Python, that keeps two places in step, such as a calendar on a server and a folder of files. It compares both sides in full on every run: asking a server only for what changed since a :term:`sync token` has been an open issue since 2014 (`pimutils/vdirsyncer#26 <https://github.com/pimutils/vdirsyncer/issues/26>`_).

pimsync does the same job in Rust. Its library, vstorage, reads and writes items wherever they're kept, and a planner compares the two sides to decide what to copy.

calendaring-sync works inside your application instead. One side is a server, and the other is your application, which reads and edits the synced data through it. It reads only what changed since the last sync, where the server supports that, and keeps everything a sync needs to continue after a stop. It doesn't talk to the server itself; an :term:`adapter` does. The core keeps the same state, with the same rules, whichever adapter does the talking, including one you write.
