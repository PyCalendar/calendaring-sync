.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

========
Glossary
========

.. meta::
   :description: The sync terms calendaring-sync's documentation uses, each defined once.

These are the sync terms calendaring-sync's pages use. :doc:`../explanation/design` explains why each part works the way it does, and :doc:`../explanation/adapters` explains what each term means for a given protocol.

.. glossary::
   :sorted:

   adapter
      The part that connects calendaring-sync to one kind of server. It asks the server what changed, hands the answer to the :term:`core` as a :term:`change set`, and sends your :term:`pushes <push>`.

   change set
      What an adapter hands the core after asking the server what changed in one :term:`scope`: the objects that changed or were removed, the :term:`sync token` it was fetched from, and the token it leads to.

   change feed
      The numbered list of every change the core made to the store, which your application reads from its :term:`cursor`.

   checkpoint
      The sync token a :term:`truncated page` ends at. The next request continues from it.

   conflict
      An object where your :term:`pending edit` and the server disagree: both sides changed it, one side deleted it while the other edited it, or the server refused your create because another object holds its :term:`UID`. calendaring-sync reports it and leaves the decision to you.

   conflict record
      What the core stores for one :term:`conflict`: the :term:`synced content`, your edit, the server's side, and the kind of conflict. It stays open until you resolve it or it resolves itself.

   container
      A calendar, or another grouping, that an object belongs to. One object can belong to more than one.

   content
      An object's data, exactly as the server sent it or as you recorded it in an edit.

   core
      calendaring-sync itself: the part that keeps sync state and knows nothing about any server or protocol.

   cursor
      The number of the last :term:`change feed` entry a part of your application has handled.

   drop
      To delete a :term:`scope` and everything the :term:`store` holds for it.

   fingerprint
      A value computed from an object's :term:`content`, leaving out what a server rewrites on its own. Two copies with the same fingerprint differ at most in what a server rewrites.

   fingerprint scheme
      The name of the function that computed a scope's fingerprints. Fingerprints from different schemes can't be compared.

   full listing
      A :term:`change set` that lists every object in a scope, not only the ones that changed.

   in flight
      The state of a :term:`push` the core has marked as sent, before the adapter reports how it ended.

   incremental change set
      A :term:`change set` that holds only what changed since the scope's :term:`sync token`.

   ingest
      To apply a :term:`change set` to the :term:`store` in one transaction. An ingest is complete when it applies a complete change set, or the last page of a truncated one.

   local key
      An identifier the core gives each record, which never changes.

   logical key
      An identifier taken from an object's content, such as its :term:`UID`. Unlike an :term:`object id`, several objects can share one.

   object
      One item a scope holds, such as one event or one task.

   object id
      The identifier the server uses for an object, unique within its :term:`scope`. It's the object's identity.

   pending edit
   local edit
   edit
      A change, delete or create you've recorded for an object that hasn't reached the server yet. The next sync pushes it.

   purge
      To delete removed records from the store for good, up to a point your application chooses.

   push
      To send one :term:`pending edit` to the server. :doc:`../explanation/design` describes how a push can end.

   record
      Everything the core stores about one :term:`object`, including its current state.

   reset
      To forget a scope's :term:`sync token`, so that the next sync of that scope is a :term:`full listing`.

   resolver
      A function you write and pass to ``resolve_conflicts()``. It receives each open :term:`conflict record` and says how to resolve it.

   scope
      The set of objects one :term:`sync token` covers, such as one calendar.

   session
      One sync of one :term:`scope`: it :term:`ingests <ingest>` every page first, then :term:`pushes <push>` your pending edits.

   store
      Where the core keeps each scope's sync token, its records, your pending edits, open conflicts, and the :term:`change feed`.

   sync token
   token
      A marker the server gives out that stands for one point in a scope's history. The adapter sends it back to ask what changed since then. The core only stores and compares it.

   synced content
      An object's content as both sides last agreed on it, at the end of the last sync.

   truncated page
   page
      One part of a change set that arrives in several parts. The last page completes the change set.

   UID
      The identifier an event or task carries inside its own data. Copies of it in other calendars carry the same UID.

   version
      A marker the server gives each object, which changes when the object does. The core only checks whether two versions are equal.
