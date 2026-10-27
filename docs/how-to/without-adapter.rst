.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

================================
Track changes without an adapter
================================

.. meta::
   :description: Use the core on its own: hand it everything a source holds, and it reports what's new, what changed, and what's gone since the last run.

The :term:`core` needs no :term:`adapter` and no protocol. Hand it everything a source holds, by id, and it works out what changed since the last run: what's new, what changed, and what's gone. The source can be anything you can read in full, such as a published calendar file, an export, or a table in another system.

.. literalinclude:: ../examples/snapshots.py
   :language: python
   :start-after: # start: track
   :end-before: # end: track

Each run is a :term:`full listing`. The source has no :term:`version` for each object, so the example uses the content's hash as one. An object whose hash the store already holds is reported :class:`~calendaring_sync.objects.change.Unchanged`, as :meth:`~calendaring_sync.replica.Replica.known_versions` describes, and isn't written again. An object the listing doesn't mention is removed. The source has no :term:`sync token` either, so each run gets a new one.

.. literalinclude:: ../examples/snapshots.py
   :language: python
   :start-after: # start: run
   :end-before: # end: run

The :term:`change feed` reports each change once: both objects arriving, then the meeting moving and the lunch going. The second run found the source as it was and added nothing.

Keep the state between runs in a SQLite file, as :doc:`store` shows, and read the feed from a :term:`cursor`, as :doc:`change-feed` shows. To send edits back to the source too, write an adapter, as :doc:`write-an-adapter` shows: the base classes hold the order of a two-way sync, so you write only how your source reads and writes.
