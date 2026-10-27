.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

==========
Contribute
==========

.. meta::
   :description: Set up calendaring-sync for development, find your way around the code, run the checks CI runs, and send a pull request the way the project reviews it.

calendaring-sync takes bug reports, fixes, documentation, and new adapters. This page covers what you need to send one: the setup, how the code is laid out, the checks a pull request must pass, and the conventions reviewers hold it to.

Before you start
================

Report a bug or propose a change in an `issue <https://github.com/PyCalendar/calendaring-sync/issues>`_, and ask questions in `Discussions <https://github.com/PyCalendar/calendaring-sync/discussions>`_. For anything beyond a small fix, open an issue first and agree on the approach before you write the code. Report a vulnerability privately, as :doc:`security` describes.

Everyone taking part follows the Python Calendaring Ecosystem's `Code of Conduct <https://pycal.org/code-of-conduct/>`_.

.. _artificial-intelligence-policy:

Artificial intelligence policy
------------------------------

calendaring-sync follows the Python Calendaring Ecosystem's `AI policy <https://pycal.org/ai-policy/>`_. Read it before you use AI to help write a pull request.

Set up
======

.. code-block:: bash

    git clone https://github.com/PyCalendar/calendaring-sync
    cd calendaring-sync
    pip install -e ".[dev]"
    pre-commit install

The ``dev`` extra installs every adapter's client library and the test servers that run embedded. To build the docs, also install the ``docs`` extra, which needs Python 3.13 or later.

How the code is laid out
========================

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Where
     - What it holds
   * - ``replica.py``, ``session.py``, ``objects/``, ``_classify.py``
     - The core: the replica, sync sessions, the classifier, and the values they take and return
   * - ``store/``
     - The store interface, and the stores that ship
   * - ``adapters/base.py``
     - The adapter base classes
   * - ``adapters/<protocol>.py``
     - Each adapter that ships, behind an extra of the same name
   * - ``tests/``
     - The unit and integration tests, the recorded server exchanges, and the benchmark

The core imports no protocol's client library and names no protocol, a store decides nothing about sync, and an adapter persists nothing itself. A change that crosses one of those lines gets redesigned in review, so raise it in the issue first. :doc:`explanation/design` explains why the layers are split this way.

A new adapter can live in its own package, built on :mod:`calendaring_sync.adapters.base` as :doc:`how-to/write-an-adapter` shows. To propose one for this repository, open an issue with the protocol, its client library, and the servers you can test it against.

.. _running-tests:

Run the tests
=============

Unit tests need no server, network, or Docker. CI runs these four files on every supported Python version:

.. code-block:: bash

    pytest src/calendaring_sync/tests/test_sync_unit.py src/calendaring_sync/tests/test_classify_unit.py src/calendaring_sync/tests/test_caldav_unit.py src/calendaring_sync/tests/test_jmap_unit.py

``test_sync_unit.py`` covers the core, the stores, and every docs example that needs no server. ``test_classify_unit.py`` covers the classifier over every combination of its inputs. The two adapter files replay HTTP exchanges recorded from real servers, kept in ``src/calendaring_sync/tests/data/``.

Integration tests run each adapter, and the docs examples that need a server, against real servers:

.. code-block:: bash

    bash tests/docker/stalwart/start.sh
    pytest src/calendaring_sync/tests/test_caldav_integration.py src/calendaring_sync/tests/test_jmap_integration.py

Radicale and Xandikos run embedded. Each other server is tested when it answers and skipped when it doesn't; the recorder each integration module's docstring names says how to start it. CI runs them only on a release tag or when a maintainer starts them, not on each pull request, so run them yourself when you change an adapter, against every server you can start.

Record server exchanges
-----------------------

After a client library upgrade, or for a new scenario, record the exchanges again:

.. code-block:: bash

    python -m calendaring_sync.tests.record_caldav SERVER [SCENARIO...]
    python -m calendaring_sync.tests.record_jmap SERVER [SCENARIO...]

Each scenario checks what it records, so a recording that doesn't show its behavior fails instead of being saved. The JMAP scenarios delete every event in the account they use, so record them with a test account. A rule no tested server shows on its own is tested by editing one exchange of a recording inside the test.

Measure performance
-------------------

If your change touches ingesting, local edits, pushes, a store, or a fingerprint, run the benchmark on ``main`` and on your branch, on the same machine, and compare:

.. code-block:: bash

    python -m calendaring_sync.tests.benchmark

It prints the best time of each scenario, each on a fresh store. ``--size`` and ``--runs`` change the scope's size and the number of runs. Explain any slowdown beyond run-to-run noise in your pull request.

Check your change
=================

CI runs every check below on each pull request. Run them first, and fix what they report before you ask for review.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Check
     - Command
   * - Lint and format
     - ``ruff check .`` and ``ruff format --check .``
   * - Types
     - ``mypy src/calendaring_sync docs/examples --ignore-missing-imports``
   * - Unit tests
     - The command in :ref:`running-tests`
   * - Docs build
     - ``sphinx-build -W -b html docs docs/_build/html``
   * - Links
     - ``sphinx-build -b linkcheck docs docs/_build/linkcheck``
   * - Docs prose
     - ``vale sync`` then ``vale docs/``, as :ref:`vale-check` describes
   * - Licensing
     - ``reuse lint``
   * - Change log
     - ``towncrier check --compare-with origin/main``

``pre-commit`` runs the lint, format, and licensing checks on each commit. The docs build fails on any warning, including a cross-reference that doesn't resolve, so a renamed class or a mistyped ``:rfc:`` role fails the build instead of shipping a broken link.

.. _code-style:

Write code
==========

``ruff`` and ``mypy`` don't catch the rules below, so review your own diff against them.

No duplication
    Before you write a function, test, or fixture, look for one that already does the job, and reuse or extend it. A test that checks what an existing test checks extends that test instead.

No hardcoding
    A literal with a meaning, such as a status value or a default path, is a named constant defined once. A literal used once with no meaning beyond it, such as a test's id, isn't.

Argument types
    The public methods of the replica, the session, the stores and the adapters, and the values an adapter builds, check their argument types at run time from their annotations, so annotate every parameter of a new public method. A built-in type such as ``str`` or ``int`` must match exactly, so ``True`` isn't taken as ``1``. The unit tests build every check, and fail on a parameter with no annotation or one the checks can't read.

Errors
    Every failure a caller can act on raises a subclass of :class:`~calendaring_sync.error.CalendaringSyncError`, and failures a caller handles differently are different classes. A public method documents what it raises, and every error path has a test that checks the store is left as it was.

Stored data
    A store file outlives the release that wrote it. A change to what :class:`~calendaring_sync.store.sqlite.SQLiteStore` writes, such as a table, a column, or what a value means, bumps :data:`~calendaring_sync.constants.SCHEMA_VERSION` and migrates a file of the old version when it's opened, with a test that opens one.

Public names
    Renaming or removing a public name follows the deprecation rule in :doc:`release`.

RFC citations
    The core cites no RFC, as :ref:`write-about-sync` says. In an adapter's docstrings, cite a published RFC with the ``:rfc:`` role and the section inside the role, such as ``:rfc:`5545#section-3.6.1```. Written as ``:rfc:`5545` section 3.6.1``, it links only to the RFC's first page. Check the section number against the RFC's text before you cite it.

    An Internet-Draft has no RFC number, so cite it as plain text with its name and section, such as "draft-ietf-jmap-calendars-33 section 5.9". Comments, tests, and private modules aren't rendered, so cite RFCs there as plain text too, such as "RFC 6578 section 3.2".

.. _prose-docstrings-comments:

Docstrings and comments
    Say what's true about this code, specifically. A comment explains what the code can't show, such as a spec rule or a server's behavior, and never restates the code next to it. Use the punctuation a sentence needs: no em dash or spaced hyphen in place of a colon, comma, or period, and "produces" or "maps to" instead of an arrow. Leave out filler such as delve, leverage, utilize, robust, seamless, and pivotal.

Dead code
    If your change makes a function unreachable, remove it in the same pull request.

.. _writing-documentation:

Write documentation
===================

This section covers the pages in ``docs/``. Docstrings follow :ref:`code-style`.

Choose a Diátaxis category
    The docs follow the `Diátaxis framework <https://diataxis.fr/>`_. Each category answers one question, so put your page where its reader's question is:

    - **Tutorial** (``tutorials/``): one guided path a newcomer follows from start to finish. New content rarely belongs here.
    - **How-to guide** (``how-to/``): the steps for one task, for a reader who knows the basics. It links to an explanation page for the why.
    - **Reference** (``reference/``): the API, generated from docstrings. Each page renders its module's docstring as its introduction, so write the introduction there.
    - **Explanation** (``explanation/``): why calendaring-sync works the way it does, under "Why" headings, as :doc:`explanation/design` does.

.. _one-place:

Say each thing in one place
    Each fact, rule, and reason lives on one page or in one docstring, and every other place links to it with ``:doc:``, ``:ref:``, ``:term:``, ``:class:``, or ``:meth:``. What a class or method does belongs in its docstring, why it does it on an explanation page, and how to do a task on a how-to page.

.. _write-about-sync:

Write about sync, not about protocols
    The core is protocol-agnostic, and so are its docs. Core pages and core docstrings describe sync in its own terms: sync tokens, versions, changes, and conflicts. Don't explain a core behavior through a protocol or a client library, even in an aside, and don't count or list the adapters in shared text: adding an adapter must never mean editing a core page. Protocol names, RFC numbers, and protocol fields belong in that adapter's own pages and docstrings.

Prose style
    Pages follow the `Microsoft Writing Style Guide <https://learn.microsoft.com/en-us/style-guide/welcome/>`_: plain language, active voice, second person, present tense, sentence-case headings, and no exclamation marks. Contractions are fine. Write "for example" and "that is", not "e.g." and "i.e.", and "such as", not "like", to introduce an example. Leave out "simple", "just", "very", and "easy": to a reader who can't complete the task as described, they read as condescending. :ref:`prose-docstrings-comments` applies here too.

    Write each paragraph as one line. reStructuredText ignores line breaks inside a paragraph, and every page in this repository is written this way.

Cross-references
    Link between pages with ``:doc:`` and a relative path, such as ``:doc:`../explanation/design```, and to a class or method with ``:class:`` or ``:meth:`` and a leading ``~`` for the short name. Link a sync term to :doc:`reference/glossary` with ``:term:`` the first time a section uses it, and define a new term there, not inline.

Code examples
    Each Python example on a page is a file in ``docs/examples/``, included with ``literalinclude`` and its ``:start-after:`` and ``:end-before:`` markers, so the code a page shows is the code a test runs. An example that needs no server gets a test in ``TestExamples``, and starts from ``start.py`` when it needs a replica that synced. One that needs a server is listed in ``LIVE`` in ``_examples.py``, keeps its server details in top-level assignments, and gets an integration test, which replaces those details. Readers copy these files, so they pass the type check too. A shell command goes in a ``.. code-block:: bash``, and a tutorial ends with a "Next steps" section.

Diagrams and headings
    Sphinx doesn't check the syntax inside a ``.. mermaid::`` diagram, so render a changed diagram before you open a pull request. Make each heading's underline exactly as long as the heading: Sphinx warns only about a page title's.

Preview the docs
----------------

.. code-block:: bash

    pip install -e ".[docs]"
    sphinx-autobuild docs docs/_build/html

This serves the docs locally and rebuilds them as you edit.

.. _vale-check:

Check prose with Vale
---------------------

`Install Vale <https://docs.vale.sh/topics/installation>`_, a standalone program rather than a Python package, then run:

.. code-block:: bash

    vale sync
    vale docs/

It runs the Microsoft style package and `signs-of-ai-writing <https://github.com/ammil-industries/vale-signs-of-ai-writing>`_, which catch many of the rules above. CI runs it as a check that warns without failing. The choice of Diátaxis category, and whether a fact is stated in one place, still need your own read of the diff.

.. _commits-and-prs:

Commits and pull requests
=========================

Commit messages follow Conventional Commits: ``type: subject``, with a lowercase type, the imperative mood, and no period, such as ``fix: keep the previous sync token when a sync fails``. The usual types are ``feat``, ``fix``, ``test``, ``docs``, ``chore``, and ``refactor``. Add a scope in parentheses only when it adds information. A commit message is one line, with no body or trailers.

Every change reaches ``main`` through a pull request, except the change log a maintainer builds for a release. Each commit makes one change. A bug fix and an unrelated refactor are two commits, or two pull requests when they don't belong together.

Write the pull request description in your own words: what you found and what you changed. Don't restate the linked issue, and don't write a checklist; the template has the one that counts.

.. _change-log:

Change log
==========

A pull request that changes behavior adds a news fragment. Changes to CI or internal code that users can't see don't need one.

.. code-block:: bash

    touch news/<issue-number>.<type>.rst

``<type>`` is one of ``breaking``, ``removal``, ``feature``, ``bugfix``, ``documentation``, ``deps``, ``internal``, ``chore``, or ``security``.

In the file, say in one or two sentences what changed for users and, for a fix, what happened before. Name a class, method or error with a role, such as ``:class:`~calendaring_sync.replica.Replica```, so the entry links to its reference. How it works and how you found it belong in the pull request. The file name's number links the issue. A change with no issue uses ``+`` instead of a number, such as ``+resync-after-invalid-token.bugfix.rst``. Write one fragment per change, numbered when an issue has several (``11.feature.1.rst``, ``11.feature.2.rst``). Don't edit ``docs/changelog.rst``: :doc:`release` builds it from the fragments.

If you used AI to help write the change, disclose it briefly in the fragment, as the :ref:`artificial-intelligence-policy` asks.

To preview the change log:

.. code-block:: bash

    towncrier build --draft --version 0.0.0

If you're unsure whether your pull request needs a fragment, ask a maintainer for the ``skip-changelog`` label.

License
=======

By contributing, you agree that your contributions are licensed under AGPL-3.0-or-later, the same as the rest of the project.
