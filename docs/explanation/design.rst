.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

======
Design
======

.. meta::
   :description: How calendaring-sync keeps sync state between your application and a server, and why it works the way it does.

calendaring-sync keeps a local copy of the calendar data your application syncs, and remembers how far each sync got. Your application reads and edits that copy, and calendaring-sync works out what to take from the server, what to send back, and where both sides changed the same object. An :term:`adapter` does the talking to the server.

.. mermaid::

   flowchart LR
       accTitle: How the parts fit
       accDescr: Your application edits and reads through the replica, which keeps its state in a store; an adapter syncs the replica with a server through a client library.
       app[Your application] -- "edits, reads, resolves" --> replica[Replica]
       replica --> store[(Store)]
       adapter[Adapter] -- "change sets, push outcomes" --> replica
       adapter -- requests --> client[Client library]
       client --> server[(Server)]

One sync of one :term:`scope`, such as one calendar, goes like this:

#. As you edit objects, you record each change in calendaring-sync as a :term:`pending edit`.
#. When your application decides to sync, the adapter asks the server what changed since the scope's :term:`sync token`, and hands the answer to calendaring-sync as a :term:`change set`.
#. calendaring-sync :term:`ingests <ingest>` the change set in one step: it stores each changed :term:`object`, removes the ones the server deleted, records a :term:`conflict` where the server changed something you also edited, saves the new sync token, and writes each change to the :term:`change feed`.
#. The adapter then :term:`pushes <push>` your pending edits to the server, and calendaring-sync records how each push ended.
#. Your application reads the change feed to learn what changed, resolves any conflicts when it's ready, and the next sync pushes the result.

The :doc:`../reference/glossary` defines every sync term, and :doc:`adapters` explains how each protocol fits these steps.

Why calendaring-sync doesn't talk to servers
============================================

calendaring-sync only keeps sync state: it doesn't send requests, schedule syncs, or run a sync loop. Your application decides when to sync, and an :term:`adapter` fetches what changed. Keeping network access out of the :term:`core` means the same state handling works for any server and any protocol an adapter supports.

Why each protocol is an adapter
===============================

The :term:`core` describes sync in its own terms, such as a :term:`sync token`, a :term:`version`, and an :term:`object id`. Each :term:`adapter` maps one protocol's equivalents onto those terms, and turns what the server sends into :term:`change sets <change set>`. Supporting a new protocol means writing a new adapter, not changing the core.

Why the parts around the core can be swapped
============================================

The :term:`core` holds every sync rule and meets the rest through narrow interfaces, so each part around it can change without the core or the other parts changing:

- where the state lives: a SQLite file, memory, or a :term:`storage` of your own, behind one interface, as :doc:`../how-to/store` shows
- where changes come from: an adapter that ships with calendaring-sync, one you write, or your own code handing the core :term:`change sets <change set>`, as :doc:`../how-to/write-an-adapter` and :doc:`../how-to/without-adapter` show
- what reads and edits the data: the core never reads inside :term:`content`, so your application uses whichever library suits the format
- how a sync runs: with a synchronous or an asynchronous client, for one :term:`scope` or many across servers and accounts, from one thread or several, as :doc:`../how-to/async` and :doc:`../how-to/parallel` show

Why sync state is kept per scope
================================

A :term:`sync token` only describes the objects it covers, so the core keeps one token per :term:`scope`. The adapter decides what a scope is: one calendar, or all events in one account. For each object in a scope, the core keeps one :class:`~calendaring_sync.objects.record.Record`, which holds the object's :term:`containers <container>` too, so a removal still tells you which calendars it affected.

Each record also gets a :term:`local key` from the core, so an object you create has an identity before the server assigns one, and keeps it if the server renames the object.

Why identity isn't the calendar UID
===================================

A calendar :term:`UID` names a recurring event with all its occurrences, and a server can hold several objects with one UID, for example one for each occurrence that someone got a separate invitation to. Some servers also accept unrelated objects with the same UID. So the :term:`object id` is the identity. The :term:`logical key` has two uses only: finding objects, where a search returns every match, and recognizing your own create when the server's answer to it never arrived, which works only where the adapter says the key is unique.

.. _design-fingerprints:

Why changes are compared by fingerprint
=======================================

A :term:`version` says something changed, not what, and some protocols have no version for each object. So the core stores a :term:`fingerprint` of each object's content. An adapter computes it so that what a server rewrites on its own doesn't count: the order of fields, how lines are wrapped, and values the server stamps itself, such as when the object was last saved. A change with the same fingerprint as the stored one only updates the version. The core doesn't report it, and it never starts a :term:`conflict`. The price is that an edit to nothing but those server-stamped values is never pushed.

Each scope remembers its :term:`fingerprint scheme`. If the scheme changes, the next sync of that scope is a :term:`full listing`.

.. _lone-surrogates:

The core never reads inside :term:`content`: it's bytes, stored exactly as the server sent them and pushed exactly as you recorded them, so nothing is decoded and any media type works. Every other value the core stores, such as an id, a token or a logical key, is a string, kept exactly as it's given, empty strings and whitespace included. The one exception is a string holding a lone surrogate, such as ``"\ud800"``: half of the pair UTF-16 uses for characters such as emoji. Python allows one in a string, but it isn't a character and UTF-8 can't encode it, so a store that writes UTF-8 would fail on it where one that keeps Python objects wouldn't. The core refuses such a string with :class:`~calendaring_sync.error.LoneSurrogateError` wherever it takes one, so every store behaves the same.

.. _design-change-sets:

Why a change set is applied all at once
=======================================

An :term:`adapter` hands the core one :term:`change set` at a time. Each one is either an :term:`incremental change set` or a :term:`full listing`, and either complete or one :term:`truncated page`. Each object in it is :class:`~calendaring_sync.objects.change.Changed`, :class:`~calendaring_sync.objects.change.Removed`, or :class:`~calendaring_sync.objects.change.Unchanged` since your version. An object created and removed between two syncs counts as removed.

The core :term:`ingests <ingest>` a change set in one transaction: it applies each change, records a conflict for it, or ignores it, and moves the :term:`sync token` forward in the same transaction. If anything fails, nothing changes, so the token is never ahead of the data.

The core refuses a change set fetched from a token the scope no longer has. That's what lets two processes share one :term:`store`: the second one to ingest gets an error instead of applying the changes twice.

Truncated pages differ by kind:

- an incremental page moves the token forward to its own :term:`checkpoint`
- a full listing moves the token only on its final page, the first point where the core knows that an object the listing never mentioned is gone

Removing an object the scope doesn't know does nothing.

.. _design-invalid-tokens:

Why an invalid token is routine
===============================

Servers forget old tokens, lose their change history, and sometimes refuse tokens they issued themselves. calendaring-sync treats that as normal: the adapter :term:`resets <reset>` the scope and fetches a :term:`full listing`. The listing doesn't fetch objects whose :term:`version` hasn't changed, and :term:`pending edits <pending edit>` and open :term:`conflicts <conflict>` survive. An interrupted listing starts over on the next sync.

You can also ask for a full listing at any time, and each scope records when its last sync and last full listing finished. A listing repairs whatever an incremental change set missed: the core removes objects the listing doesn't mention, and fetches objects whose version differs. :doc:`../reference/server-compatibility` lists the servers where this matters.

Why local edits are recorded, not discovered
============================================

You tell calendaring-sync about each :term:`local edit` as you make it: a change, a delete, or a create, which returns the new record's :term:`local key`. Repeated edits to one object collapse into one, an edit back to the :term:`synced copy` cancels itself, and you can discard an edit. You can't edit an object with an open :term:`conflict` until you resolve it.

Comparing every object at every sync instead would cost a full comparison each time, and a difference can't tell whether your application edited an object or never loaded its latest version.

.. _design-reads-first:

Why a sync reads before it writes
=================================

Each :term:`session` ingests every page before it pushes anything. The core refuses a :term:`push` while pages remain, since a later page could hold the change that should stop the push. It also refuses to ingest after a push in the same session, since a change set fetched before your write could overwrite it. Each scope has its own session.

A session is ``ingesting`` while pages remain and ``ingested`` once the token is final. A change set that arrives complete takes it straight from ``idle`` to ``ingested``, and an interrupted session continues from the stored token on the next sync:

.. mermaid::

   stateDiagram-v2
       accTitle: Session states
       accDescr: A session ingests every page before it pushes, then returns to idle.
       [*] --> idle
       idle --> ingesting: truncated page
       ingesting --> ingesting: another page
       ingesting --> ingested: final page
       ingesting --> idle: interrupted
       idle --> ingested: complete change set
       ingested --> pushing: push starts
       ingested --> idle: nothing to push
       pushing --> idle: pushes done
       note right of ingesting: no pushes while pages remain
       note right of pushing: no ingests until it ends

Before a push goes out, the adapter :term:`claims <claim>` it: the core marks it :term:`in flight` in the store, in one step that fails if another process got there first, so two processes can't send the same edit. The adapter then reports how the push ended as a :data:`~calendaring_sync.objects.push.PushOutcome`.

An :class:`~calendaring_sync.objects.push.Unknown` push stays in flight, and so does one whose process stopped before the answer arrived. The next complete :term:`ingest` of its scope, in any process, settles it:

- a create that comes back with what you sent, under the id it chose, reached the server. Where the server picks the id, a new object with the same logical key and what you sent counts the same, but only if the adapter says that key is unique. A different object at the id the create chose is a conflict, and one that only shares its logical key is another object, so the create goes out again
- an update that comes back with your fingerprint reached the server, and one that comes back with another fingerprint is a conflict, since someone else changed the object too
- a delete that comes back removed reached the server
- a push the change set doesn't mention didn't reach the server, and its edit is pending again

Treating an unknown push as failed could create the same object twice. An outcome that arrives after an ingest settled its push still counts while the record holds the same edit and no one has claimed it again, and changes nothing otherwise. Two processes syncing one scope at the same moment can therefore send one edit twice. A second change or delete is rejected where the adapter guards it with the object's version. A create whose id the server picks has no such guard, so the server can end up with the object twice, unless the adapter says logical keys are unique: then the server refuses the second create for its key, which becomes a conflict. Nothing you edited is lost either way.

.. _design-server-answer:

Why the server's answer to a write isn't trusted
================================================

Some servers rewrite what they store and still answer the write with a version. So an applied push clears the record's :term:`version` instead of keeping the one the server returned. The next incremental change set includes your write, so the adapter fetches the object once and the core stores it as the server holds it. Because the fingerprint ignores what the server rewrites, that copy counts as your own write coming back, not as a remote change. Where the adapter guards each push with the object's version, a new edit to a record whose version was cleared waits until an ingest gives it one again, so it never goes out unguarded.

A rejected push isn't a :term:`conflict` by itself, because some protocols only check whether anything in the scope changed since the last sync, not whether this object did. The next ingest decides: if this object changed, it's a conflict, and if not, the adapter pushes the edit again. Where the check covers the whole scope, one rejection means every later push in that sync would be rejected too, so they wait, unsent, for the next sync. An edit stays pending until the server applies it, so your content isn't lost.

.. _design-states:

Why an object has five states
=============================

Every :term:`record` is in one of five states at a time: synced, pending, in flight, conflict, or removed. The diagram shows the main ways a record moves between them:

.. mermaid::

   stateDiagram-v2
       accTitle: Object states
       accDescr: The main ways a record moves between synced, pending, in flight, conflict, and removed.
       state "in flight" as in_flight
       [*] --> synced: new on the server
       [*] --> pending: create recorded
       synced --> pending: local edit
       synced --> removed: removed on server
       removed --> synced: back on server
       pending --> synced: edit undone
       pending --> in_flight: push sent
       pending --> conflict: server changed it too
       in_flight --> synced: applied
       in_flight --> removed: delete done
       in_flight --> pending: rejected
       in_flight --> conflict: gone, or UID taken
       conflict --> synced: resolved: server's copy
       conflict --> pending: resolved: push it
       conflict --> removed: resolved: removal
       removed --> [*]: purged

The table lists every transition with its conditions, including these, which the diagram leaves out:

- an unknown push
- a create recorded at a removed record's object id
- a conflict the server settles on its own
- both sides reaching the same content, or both deleting
- a scope :term:`reset`, which leaves every record's state as it is

.. list-table::
   :header-rows: 1
   :widths: 22 38 40

   * - State
     - Event
     - Result
   * - synced
     - Remote change, or remote removal
     - Synced with the new content, or removed
   * - synced
     - Local edit recorded
     - Pending
   * - pending
     - Edit back to the synced copy, or discarded
     - Synced; a create the server never had is deleted from the store instead
   * - pending
     - Remote change or removal ingested
     - Conflict, unless both sides arrived at the same content or both deleted
   * - pending
     - Push claimed
     - In flight
   * - in flight
     - Applied
     - Synced, with no version until the next ingest, or removed if it was a delete
   * - in flight
     - Rejected
     - Pending; the next ingest decides
   * - in flight
     - Gone
     - Removed if it was a delete, otherwise a conflict
   * - in flight
     - Refused because another object holds its logical key
     - Conflict
   * - in flight
     - Unknown, or the process stopped
     - Settled by the next complete ingest
   * - conflict
     - Remote change ingested
     - Conflict with the new server's copy; synced if it now equals your edit, pending again if the server is back at the synced copy, or removed if both sides deleted the object
   * - conflict
     - The object holding the logical key an edit was refused for stops holding it, removed or changed on the server or by your own push
     - Pending again, unless another object in the scope still holds the key
   * - conflict
     - Resolved
     - Synced, pending, or removed, depending on the resolution; a create the server refused is deleted from the store when you discard it or push it as an update to the object holding its logical key
   * - removed
     - Remote change: the server holds an object at that id again
     - Synced with the new content, as the same record
   * - removed
     - Create recorded at its object id
     - Pending, as the same record, holding the create
   * - removed
     - Purged by your application
     - Gone from the store; its feed entries stay until trimmed
   * - any
     - Scope reset
     - Unchanged; pending edits and conflicts survive the listing that follows

.. _design-conflicts:

Why conflicts are reported, not resolved
========================================

When a :term:`conflict` comes up, calendaring-sync reports it and leaves the decision to your code. Only your application knows whether the local edit, the server's copy, or a merge is right. A removal on the server against a local edit is always a conflict, never resolved automatically, because that would delete your edit without asking.

The core classifies each change by looking only at the :term:`synced copy`, the server's change, and your edit. The result is one of six classes:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Class
     - Meaning
   * - ``UNCHANGED``
     - Nothing to do: no change, a version-only change, or both sides arrived at the same content
   * - ``REMOTE_ONLY``
     - The server changed or created the object, with no local edit; it's applied
   * - ``LOCAL_ONLY``
     - There's a local edit and the server didn't change the object; it's pushed
   * - ``DELETED_REMOTE``
     - The server removed the object with no local edit, or both sides deleted it; it's removed
   * - ``DELETED_LOCAL``
     - You deleted the object and the server didn't change it; the delete is pushed
   * - ``CONFLICT``
     - Both sides acted; a conflict record is stored, of one :class:`~calendaring_sync.objects.conflict.ConflictKind`

Later remote changes update the record's :term:`server's copy`. The conflict resolves itself when the server reaches your edit, goes back to the synced copy, or removes an object you deleted too. An edit refused for its logical key goes back to pending once the object holding that key no longer holds it.

The core stores conflicts instead of raising them: an exception for one object would abort every other change in the sync, and a :term:`resolver` called during a sync would run again on every retry. So you resolve them when you choose, outside any sync, each with a :data:`~calendaring_sync.objects.resolution.Resolution`: one with :meth:`~calendaring_sync.replica.Replica.resolve`, or every open one through your :term:`resolver` with :meth:`~calendaring_sync.replica.Replica.resolve_conflicts`. Anything but the server's copy becomes a pending edit over the server's current copy, so its push is checked against the server's current version. :meth:`~calendaring_sync.replica.Replica.resolve` refuses a record whose conflict changed since you read it, so a decision is never applied to news you haven't seen.

.. _design-feed:

Why changes are read from a feed
================================

Everything a sync, a resolution or a dropped scope changes in the store also goes into a :term:`change feed`, in the same transaction: a remote change or removal applied, a conflict stored, updated or resolved, a push's outcome, and a scope dropped. Recording an edit writes no entry; the edit reaches the feed when its push ends. Each entry has a number that only grows. Your application keeps the number of the last entry it handled, its :term:`cursor`, and asks for the entries after it, so it learns what changed even if it stopped right after a sync. Several parts of an application can each keep their own cursor.

A removed record stays in the store, marked removed, so a removal can always be read. It stays until your application :term:`purges <purge>` it, or until the object comes back, on the server or as a create at its id. You trim the feed and purge removed records up to a point you choose. A cursor older than the oldest entry kept gets an error, and the application reads its records again from the store instead.

.. _design-dropped-scope:

Why a removed scope is yours to drop
====================================

If a calendar or account disappears from the server, the adapter raises an error and the :term:`store` stays as it was. Treating it as an empty listing would turn every pending edit into a conflict after one transient error. You :term:`drop` the scope yourself, and the call returns the pending edits and open conflicts it discards.

.. _design-storage:

Why storage is synchronous
==========================

A :term:`store` is a :class:`~calendaring_sync.store.base.StateStore` over a :term:`storage`, a typed interface that a SQLite file, memory or storage of your own implements. The store holds every rule about transactions, threads, checks and errors once, so a storage only reads and writes. One store holds many scopes. A key-value interface couldn't apply a change set and advance a token in one transaction, and an asynchronous store would push synchronous callers through an event loop. Adapters come in synchronous and asynchronous variants that share their logic. The asynchronous ones run each store call in a worker thread, since ingesting a large full listing is one long transaction and would otherwise block the event loop. Reads from other threads don't wait for that transaction, at most for its commit: until then they see what the store held before it began, so your application can show its records while a sync runs.
