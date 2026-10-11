.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

======
Design
======

.. code-block:: text

    Your application
           |
           v
    calendaring-sync
      sync state, local copy, conflict detection
           |
           v
    Adapters

This page explains the model every part of calendaring-sync follows. :doc:`adapters` explains how each protocol maps onto it.

Why calendaring-sync doesn't talk to servers
============================================

calendaring-sync only keeps sync state: it doesn't send requests, schedule syncs, or run a sync loop. Your application decides when to sync, and an adapter fetches what changed. Keeping network access out of the core means the same state handling works for any server and any protocol an adapter supports.

Why each protocol is an adapter
===============================

The core describes sync in its own terms: a sync token for "what changed since last time," a version for "did this object change," and an identity for each object. An adapter maps one protocol's equivalents onto those terms and translates its change reports. Supporting a new protocol means writing a new adapter, not changing the core.

Why sync state is kept per scope
================================

A scope is whatever one sync token covers, as the adapter defines it: one calendar, or one data type across a whole account. The core keeps one token per scope and one record per object, holding:

- the object id, unique within the scope
- a version, if the protocol has one, compared only for equality
- a fingerprint of the content
- the content, exactly as the server sent it
- the containers the object belongs to, so a removal still says which calendars it affected
- an optional logical key, such as the iCalendar UID

Each record also gets a local key from the core, so an object you create has an identity before the server assigns one and keeps it if the server renames the object.

Why identity isn't the calendar UID
===================================

A UID names a whole recurrence set, and a server can hold several objects with one UID, for example one per occurrence a person was invited to on its own. Some servers also accept unrelated objects with the same UID. The object id is the identity; the logical key is only for finding objects, which returns every match, and for recognizing your own create after its result was lost.

Why changes are compared by fingerprint
=======================================

A version says something changed, not what, and some protocols have no per-object version. The core stores a fingerprint of each object's content: SHA-256 of the text by default, or an adapter's own function where the text isn't stable, such as JSON with keys in any order. A change with the same fingerprint as the stored one only updates the version. It isn't reported, and it never starts a conflict.

Each scope stores the name of its fingerprint scheme. If the scheme changes, the scope is listed again from scratch.

The core never parses content. It stores what the server sent and pushes what you recorded.

Why a change set is applied all at once
=======================================

An adapter hands the core a change set for one scope. It names the token it was fetched against and the token it leads to, and says whether it's incremental or a full listing, and complete or one truncated page. Each object id maps to one of three outcomes: changed (with version, fingerprint, content, containers and logical key), removed, or unchanged since your version. An object appears once; one created and removed in the same report counts as removed.

Ingesting is one transaction. Every change is applied, stored as a conflict, or ignored, and the token advances in the same transaction. If anything fails, nothing changes, so the token is never ahead of the data.

A change set fetched against a token the scope no longer has is refused. That's what lets two processes share one store: the second one to ingest gets an error instead of applying the changes twice.

Truncated pages differ by kind:

- an incremental page advances the token to its own checkpoint
- a full listing advances the token only on its final page, the first point where an object it never mentioned is known to be gone

Removing an object the scope doesn't know does nothing.

Why an invalid token is routine
===============================

Servers expire tokens, lose history, and refuse tokens they issued. calendaring-sync treats that as normal: the adapter resets the scope and fetches a full listing. Unchanged versions aren't fetched again, and pending edits and open conflicts survive. An interrupted listing starts over on the next run.

You can also ask for a full listing at any time, and each scope records when its last sync and last full listing finished. A listing repairs whatever an incremental report missed: objects it doesn't mention are removed, and objects with a different version are fetched. :doc:`../reference/server-compatibility` lists the servers where this matters.

Why local edits are recorded, not discovered
============================================

Your application tells calendaring-sync about each edit as it happens: a change, a delete, or a create, which returns the new record's local key. Repeated edits to one object collapse into one, an edit back to the synced content cancels itself, and you can discard an edit. An object with an open conflict can't be edited until the conflict is resolved.

Diffing everything at every sync instead would cost a full comparison each time, and a difference can't say whether the application edited an object or never loaded its latest version.

Why a sync reads before it writes
=================================

A sync session for one scope ingests every page, then pushes local edits. Pushing is refused while pages remain, since a later page could hold the change that should stop the push. Ingesting after a push in the same session is refused too, since a change set fetched before your write could overwrite it. Each scope has its own session.

A push is claimed in the store before its request goes out, with one conditional update, so two processes can't send the same edit. Each push has one of five outcomes:

- applied: the edit becomes the synced content
- rejected: the server's precondition failed, and the edit stays pending
- gone: the object no longer exists on the server
- refused because another object holds its logical key, which becomes a conflict
- unknown: the request may or may not have reached the server

A push whose process stopped before it finished is unknown when the store is next opened. The next complete ingest settles each unknown push:

- a create that comes back under its chosen id, or with its client-generated logical key, landed, and the server's copy is stored
- an update that comes back with your content landed; one that comes back different is a conflict, since a server's rewrite and another client's edit look the same
- a delete that comes back removed landed
- anything not mentioned didn't land, and is pending again

Treating an unknown push as failed could create the same object twice.

Why the server's answer to a write isn't trusted
================================================

Some servers rewrite what they store and still answer the write with a version. So an applied push clears the record's version instead of keeping the one returned. The next incremental report includes your write, and the object is fetched once and stored as the server holds it.

A rejected push isn't a conflict by itself, because some protocols only check whether anything changed since the last sync, not whether this object did. The next ingest decides: if this object changed, it's a conflict; if not, the edit is pushed again. An edit stays pending until a push is applied, so your content isn't lost.

Why an object has five states
=============================

Every record is in one state at a time:

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
     - Edit back to the synced content, or discarded
     - Synced
   * - pending
     - Remote change or removal ingested
     - Conflict, unless both sides arrived at the same content or both deleted
   * - pending
     - Push claimed
     - In flight
   * - in flight
     - Applied
     - Synced, with no version until the next ingest
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
     - Conflict with the new remote side, or synced if that now equals your edit
   * - conflict
     - Resolved
     - Synced, pending, or removed, depending on the resolution
   * - any
     - Scope reset
     - Unchanged; pending edits and conflicts survive the listing that follows

A session goes ``idle``, ``ingesting`` while pages remain, ``ingested`` once the token is final, ``pushing``, then ``idle`` again.

Why conflicts are reported, not resolved
========================================

When an object changed both locally and on the server, or was deleted on one side and edited on the other, calendaring-sync reports the conflict and leaves the decision to your code. Only the application knows whether the local edit, the server's version, or a merge is right.

Classifying a change depends only on the last synced state, the remote change and the local edit. The result is one of six classes:

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
     - Both sides acted; a conflict record is stored

A conflict record has a kind: both modified, removed on the server but edited locally, deleted locally but edited on the server, both created at the same reserved id before your create was sent, or refused because another object holds the UID. It keeps the synced, local and remote sides. Later remote changes update the remote side, and if it becomes equal to your edit, the conflict resolves itself.

Conflicts are stored rather than raised: an exception for one object would abort every other change in the sync, and a resolver called during a sync would run again on every retry. You resolve a record with your edit, the server's version, merged content, or a delete, and the result is pushed with the remote version as its base. A create refused for its UID can't be pushed again as is, so it can be discarded, pushed as an update to the object holding the UID, or replaced by merged content, possibly with a new UID.

``resolve_conflicts(resolver)`` passes each open record to your resolver, outside any sync. Each resolution is its own transaction: ``None`` leaves a record open, and an exception stops the loop and keeps the resolutions before it.

A removal against a local edit is always a conflict, never resolved automatically, because that would delete your edit without asking.

Why a removed scope is yours to drop
====================================

If a calendar or account disappears from the server, the adapter raises an error and the store stays as it was. Treating it as an empty listing would turn every pending edit into a conflict after one transient error. You drop the scope yourself, and the call returns the pending edits and open conflicts it discards.

Why storage is synchronous
==========================

Storage is a typed interface, ``StateStore``, with an in-memory implementation for tests and a SQLite one for real use. One store holds many scopes. A key-value interface couldn't apply a change set and advance a token in one transaction, and an asynchronous store would push synchronous callers through an event loop. Adapters come in synchronous and asynchronous variants that share their logic; the asynchronous ones call the core directly, since each store call is one short transaction.
