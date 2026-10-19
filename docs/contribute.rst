.. SPDX-FileCopyrightText: 2026 calendaring-sync contributors
.. SPDX-License-Identifier: AGPL-3.0-or-later

==========
Contribute
==========

This guide describes how to contribute to calendaring-sync.

calendaring-sync follows the Python Calendaring Ecosystem's `Code of Conduct <https://pycal.org/code-of-conduct/>`_.

Development setup
=================

.. code-block:: bash

    git clone https://github.com/pycalendar/calendaring-sync
    cd calendaring-sync
    pip install -e ".[dev]"
    pre-commit install

Running tests
=============

Unit tests, no server required:

.. code-block:: bash

    pytest src/calendaring_sync/tests/test_sync_unit.py src/calendaring_sync/tests/test_classify_unit.py

Tests live inside the package, in ``src/calendaring_sync/tests/``. ``test_classify_unit.py`` holds the classifier's tests, over every combination of its inputs, and ``test_sync_unit.py`` holds the rest. Unit tests make no network calls and need no Docker, so they run anywhere.

Previewing docs
===============

.. code-block:: bash

    pip install -e ".[docs]"
    sphinx-autobuild docs docs/_build/html

Opens a local server that rebuilds and reloads the browser tab automatically as you edit files under ``docs/``. The ``docs`` extra needs Python 3.12 or newer, because Sphinx itself does.

.. _vale-check:

Checking docs prose with Vale
=============================

`Install Vale <https://docs.vale.sh/topics/installation>`_ first; it's a standalone binary, not a Python package. Then:

.. code-block:: bash

    vale sync
    vale docs/

CI runs this the same way, as a warn-only check (a failure doesn't block a PR yet): it catches a real subset of :ref:`writing-documentation`'s rules mechanically, but it isn't a substitute for reading your own diff.

.. _artificial-intelligence-policy:

Artificial intelligence policy
==============================

calendaring-sync follows the Python Calendaring Ecosystem's `AI policy <https://pycal.org/ai-policy/>`_. Read it before using AI to help draft a pull request.

.. _commits-and-prs:

Commits and pull requests
=========================

Commit messages follow Conventional Commits: ``type: subject``, lowercase type, imperative mood, no period, for example ``fix: keep the previous sync token when a sync fails``. Common types in this repo: ``feat``, ``fix``, ``test``, ``docs``, ``chore``, ``refactor``. A scope in parens is fine when it adds real information, skip it otherwise. One line is enough; this project doesn't use commit bodies or trailers.

Keep each commit to one concern. A PR that both fixes a bug and refactors an unrelated helper should be two commits, not one with an unrelated diff mixed in; split it into two PRs instead if the two concerns don't obviously belong together (a broad cleanup PR touching many small, related things is fine as one PR with several commits inside it).

Write the PR description in your own words, as a short account of what you found and did, not a restatement of the linked issue's own text back at its author, and not a bullet-by-bullet checklist. If the work turned up real bugs along the way, describe the fix; don't frame the PR as "here's how many bugs I found," even when true, that framing reads as padding a body's length rather than reporting the work.

Before opening a PR: run the full local test suite, ``ruff check`` and ``ruff format --check``, ``mypy``, ``reuse lint``, and :ref:`vale-check` and ``sphinx-build -W -b html docs docs/_build/html`` if you touched any ``.rst`` page. Fix everything that comes back before asking for review, rather than leaving a known-red check for the reviewer to raise. Vale runs warn-only in CI for now, so it won't block the PR on its own, but treat its findings the same as the others. CI also builds the docs with Sphinx's warnings-as-errors flag, so a broken ``:rfc:`` role, cross-reference, or ``automodule``/``autoclass`` directive fails the PR there rather than silently deploying a broken page to Read the Docs.

.. _change-log:

Change log
==========

If your PR changes behavior, add a news fragment. CI-only and internal-refactor PRs don't need one.

.. code-block:: bash

    touch news/<issue-number>.<type>.rst

Where ``<type>`` is one of: ``breaking``, ``removal``, ``feature``, ``bugfix``, ``documentation``, ``deps``, ``internal``, ``chore``, ``security``.

Write a short, user-facing description of the change inside the file: state what changed and, if it's a fix, what happened before. The issue link is generated automatically from the filename's number, so don't add one yourself. If your PR isn't tied to an issue, prefix the filename with ``+`` instead of a number (for example ``+resync-after-invalid-token.bugfix.rst``); towncrier accepts orphan fragments this way and just omits the issue link. Fragments are collected into ``docs/changelog.rst`` at release time. Don't edit that file directly. See :doc:`release` for the maintainer-only steps that actually cut a release.

Keep each fragment to one or two sentences. State the change; don't explain how it works internally, why it was needed, or how it was found, that belongs in the PR description, not the changelog. If your PR touches more than one distinct behavior (two separate bugs, or a feature plus an unrelated parameter addition), write one fragment per behavior (``11.feature.1.rst``, ``11.feature.2.rst``, and further numbered fragments as needed) instead of one fragment covering all of them. See any ``bugfix`` fragment in ``news/`` for the length and tone to aim for.

If you used AI to help write the change, briefly disclose it in the fragment, per the :ref:`artificial-intelligence-policy`.

To preview what the change log will look like:

.. code-block:: bash

    towncrier build --draft --version 0.0.0

If you're unsure whether your PR needs a fragment, ask a maintainer for the ``skip-changelog`` label rather than skipping silently.

.. _code-style:

Code style
==========

``ruff`` and ``mypy`` catch syntax and typing issues; they don't catch everything below, so review your own diff for it before opening a PR.

No duplication
    If you're about to write a function, block, or test fixture that's the same shape as one that already exists elsewhere (even with different variable names), extract a shared helper instead. This applies to tests as much as source.

No hardcoding
    A literal that means something (a status value, a format string, a default path) belongs in a named constant defined once, not inlined at every call site. A one-off literal with no reused meaning (a test fixture's arbitrary ID) doesn't need this.

RFC citations
    The core doesn't cite RFCs; see :ref:`write-about-sync`. In adapter docstrings (not comments, not test files, see below), cite a real, finalized RFC with Sphinx's ``:rfc:`` role, and always merge the section number into the role itself: ``:rfc:`5545#section-3.6.1```, not ``:rfc:`5545` section 3.6.1``. The latter renders as two disconnected pieces, a link to the RFC's front page and plain text next to it, so a reader has to manually find the section after clicking through. Verify the section number against the actual RFC text before citing it; don't guess from a related section or trust an existing comment's number without checking.

    A spec that has no RFC number yet (an Internet-Draft) can't use ``:rfc:``, since the role links to ``datatracker.ietf.org/doc/html/rfcNNNN.html`` and there's no such page yet. Cite it as plain text with its draft name and section until it's assigned a number, then convert it.

    ``:rfc:`` only renders correctly in docstrings that Sphinx's ``automodule``/autodoc actually pulls in (check ``docs/reference/*.rst`` for which modules that is; private, underscore-prefixed modules aren't autodoc'd, so a role there is only for source readers, not the built docs). Plain ``#``-comments are never RST-parsed, and neither are docstrings inside ``tests/``, so use plain "RFC NNNN section N.N" text in both, not the role.

.. _prose-docstrings-comments:

Prose in docstrings and comments
    Write plainly and specifically: say what's actually true about this code, not a general description that could apply to any project. Pick the punctuation a sentence actually needs instead of leaning on an em dash or a spaced hyphen (``word - word``) as a stand-in for a colon, comma, or period; the same goes for the Unicode arrow ``→`` used as shorthand for "produces" or "maps to," which reads clearer written out, or as ASCII ``->`` inside a code example where alignment matters.

    A few words read as filler more often than not and are usually worth cutting: delve, leverage, utilize (say "use"), robust, seamless, pivotal, cutting-edge, game-changer. If a sentence is true and specific without one of these, it almost never needs it back in.

Dead code
    Before adding a new helper, check whether an existing one already does the job. If your change makes a function unreachable, remove it in the same PR rather than leaving it for a future cleanup pass; check the milestone tracker first in case a near-term feature already claims it.

.. _writing-documentation:

Writing documentation
=====================

This section covers ``docs/*.rst`` page prose: how-to guides, tutorials, reference pages, and explanation pages. For docstring and comment conventions, see :ref:`code-style` above; this section cross-references those rules rather than restating them.

Choosing a Diataxis category
    calendaring-sync's docs are organized into four categories: tutorials, how-to guides, reference, and explanation. This split comes from the `Diátaxis framework <https://diataxis.fr/>`_. Django's docs use the same four-way split, under different section names; pytest's docs use nearly these same names. Each category answers a different question, so pick the one matching what the reader actually needs:

    - **Tutorial** (``tutorials/``): a single guided path a newcomer follows start to finish, learning by doing. New content almost never belongs here: a tutorial teaches someone who knows nothing yet, which is a narrow, rarely-needed case, and most new content is really a how-to.
    - **How-to guide** (``how-to/``): a terse recipe for someone who already knows the basics and wants to do one specific thing. No "why" content inline: cross-reference :doc:`explanation/design` instead.
    - **Reference** (``reference/``): generated, factual lookup material. This is almost entirely autodoc output; hand-written prose here should be limited to a short page intro, not an explanation of design choices.
    - **Explanation** (``explanation/``): the why, background, and design rationale, kept separate so how-to pages stay scannable. ``explanation/design.rst``'s existing "Why X" section headings are the pattern to match.

.. _write-about-sync:

Write about sync, not about protocols
    calendaring-sync's core is protocol-agnostic, and its docs are too. Core pages and core docstrings describe sync on its own terms: sync tokens, object versions, changes, and conflicts. Don't explain a core behavior by contrasting it against a specific protocol or library, even as a single clarifying aside. Protocol names, RFC numbers, and protocol-specific fields belong only in an adapter's own pages and docstrings.

Prose style for pages
    Page prose follows the `Microsoft Writing Style Guide <https://learn.microsoft.com/style-guide/welcome/>`_: plain language, active voice, second person ("you," not "the user"), sentence-case headings, no exclamation points. Contractions are fine. Avoid Latin abbreviations in prose ("for example," not "e.g."; "that is," not "i.e."). This is a different, more conversational register than :ref:`prose-docstrings-comments`'s rule, which governs terse, factual docstring/comment text; that rule's bans apply here too, unchanged.

    Write each paragraph as one line, not hard-wrapped at a fixed column width. RST collapses whitespace, so wrapping serves no rendering purpose; every existing page in this repo is written this way.

    Avoid "simple," "just," "very," and "easy": a reader who can't complete the task as described finds these words condescending rather than reassuring. Prefer "such as" over "like" when introducing an example, "select" over "check"/"tick" for a UI action, and "verify" over "be sure" when asking the reader to confirm something.

    Spell these terms consistently: GitHub, add-on (not addon), plug-in (not plugin), reST or reStructuredText (not RST, except as the file extension or in a code span like ``.rst``).

    :ref:`vale-check` runs the Microsoft Vale style package plus `signs-of-ai-writing <https://github.com/ammil-industries/vale-signs-of-ai-writing>`_ (patterns from Wikipedia's `Signs of AI writing <https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing>`_ essay) in CI, catching a real subset of these rules mechanically: contractions, passive voice, dash spacing, sentence-case headings, hedging clusters, chatbot phrases, and the AI-typical vocabulary and symbolic-language patterns the essay documents. It doesn't catch everything: the Diataxis category choice and whether a how-to page pushed "why" content where it belongs still need a reviewer's judgment.

Cross-referencing and code examples
    Link between pages with ``:doc:`` and an explicit relative path (``:doc:`../explanation/design```). Reference a class or method with ``:class:``/``:meth:`` and a leading ``~`` for a short display name. Cite an RFC the same way :ref:`code-style`'s "RFC citations" rule describes for docstrings, merging the section into the role.

    Link a sync term to :doc:`reference/glossary` with ``:term:`` the first time a section uses it, and define a new term there rather than inline. Sphinx doesn't check the syntax inside a ``.. mermaid::`` diagram, so render a changed diagram before opening a PR.

    Use ``.. code-block:: python`` or ``.. code-block:: bash``, with a blank line before and after the directive and the indented block. A tutorial ends with an explicit "Next steps" section linking forward to relevant how-to and explanation pages.

Title underlines
    RST title underlines must match the title's exact character width. Sphinx only warns about this for a page title's overline/underline pair, not for an ordinary section heading's underline alone, so a mismatched section underline builds silently; count it yourself rather than assume a clean build means it's correct.

License
=======

By contributing, you agree your contributions are licensed under AGPL-3.0-or-later, same as the rest of the project.
