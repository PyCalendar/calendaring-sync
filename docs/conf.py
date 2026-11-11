# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sphinx configuration for calendaring-sync documentation."""

import datetime as dt
import importlib
import importlib.metadata
import inspect
import os
import pkgutil
import re

from packaging.version import Version

import calendaring_sync

project = "calendaring-sync"
# The navbar shows the version without a development build's suffix, so a build
# between releases shows the next version hatch-vcs guesses.
release = importlib.metadata.version("calendaring-sync")
version = Version(release).base_version
this_year = dt.date.today().year  # noqa: DTZ011
copyright = f"{this_year}, calendaring-sync contributors"  # noqa: A001
author = "calendaring-sync contributors"

extensions = [
    "notfound.extension",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    # Must come after napoleon: https://github.com/tox-dev/sphinx-autodoc-typehints/issues/15
    "sphinx_autodoc_typehints",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_copybutton",
    "sphinx_design",
    "sphinx_issues",
    "sphinx_last_updated_by_git",
    "sphinx_llms_txt",
    "sphinxcontrib.mermaid",
    "sphinxext.opengraph",
]

# Read the Docs gives each build its address with the language and version,
# so the links built from it, in social cards and llms.txt, reach real pages.
html_baseurl = os.environ.get(
    "READTHEDOCS_CANONICAL_URL", "https://calendaring-sync.readthedocs.io/en/stable/"
)

ogp_site_url = html_baseurl
ogp_site_name = "calendaring-sync"
ogp_description_length = 200
ogp_type = "website"
ogp_social_cards = {
    "image": "_static/img/pycal-icon.png",
    "image_mini": "_static/img/pycal-icon.png",
    "line_color": "#0f766e",
    "background_color": "#f8f7f4",
}

# sphinx_issues configuration: enables :issue:`N`, :pr:`N`, :user:`name` roles
issues_github_path = "PyCalendar/calendaring-sync"

html_theme = "pydata_sphinx_theme"
html_theme_options = {
    "icon_links": [
        {
            "name": "GitHub",
            "url": "https://github.com/PyCalendar/calendaring-sync",
            "icon": "fa-brands fa-square-github",
            "type": "fontawesome",
            "attributes": {"target": "_blank", "rel": "noopener me"},
        },
        {
            "name": "PyPI",
            "url": "https://pypi.org/project/calendaring-sync",
            "icon": "fa-custom fa-pypi",
            "type": "fontawesome",
            "attributes": {"target": "_blank", "rel": "noopener me"},
        },
    ],
    "article_footer_items": ["last-updated"],
    "footer_start": ["nlnet", "donate"],
    "footer_center": "copyright",
    "footer_end": ["rtd-pr-previews", "theme-version", "sphinx-version"],
    "logo": {"text": "calendaring-sync"},
    "navbar_start": ["navbar-logo", "version"],
    "navigation_with_keys": True,
    "search_bar_text": "Search",
    "secondary_sidebar_items": ["edit-this-page", "page-toc", "sourcelink"],
    "show_nav_level": 2,
    "show_toc_level": 2,
    "use_edit_page_button": True,
}
html_last_updated_fmt = "%Y-%m-%d"
# sphinx-last-updated-by-git hides the source of a page git doesn't track yet,
# and sphinx-llms-txt needs every page's source, so a new page would fail the
# build until it's committed.
git_untracked_show_sourcelink = True

html_context = {
    "github_user": "PyCalendar",
    "github_repo": "calendaring-sync",
    "github_version": "main",
    "doc_path": "docs",
}

templates_path = ["_templates"]
# The index of every generated reference page, which the reference section
# links through the package's own page instead.
exclude_patterns = ["reference/api/modules.rst"]
html_static_path = ["_static"]
# The fa-custom fa-pypi icon in icon_links above.
html_js_files = [("js/custom-icons.js", {"defer": "defer"})]
# Tables and the state diagrams.
html_css_files = ["css/content.css"]

notfound_template = "404.html"
# Defaults to READTHEDOCS_CANONICAL_URL's path on RTD; outside RTD that env
# var isn't set and the extension falls back to a hardcoded "/en/latest/",
# which 404s every asset on a local build. Use root-relative paths instead
# when not building on RTD.
if not os.environ.get("READTHEDOCS"):
    notfound_urls_prefix = "/"

# Diagrams render in the reader's browser with mermaid.js from jsdelivr, so
# building the docs needs no Node.js. Pin the release so rendering can't change
# under us.
mermaid_output_format = "raw"
mermaid_version = "12.1.0"
# Measure labels in the site's own font, so they fit the boxes drawn for them.
mermaid_init_config = {"startOnLoad": False, "fontFamily": "var(--pst-font-family-base)"}

# The link check fails on a broken link. GitHub sends anyone signed out of
# the private vulnerability form to its login page, which isn't broken.
linkcheck_ignore = [r"https://github\.com/PyCalendar/calendaring-sync/security/advisories/new"]
linkcheck_timeout = 10
linkcheck_retries = 2

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "caldav": ("https://caldav.readthedocs.io/stable/", None),
    "calendaring_jmap": ("https://calendaring-jmap.readthedocs.io/en/stable/", None),
}

# Every reference must resolve, so a renamed class or method can't leave a
# link that quietly renders as plain text.
nitpicky = True
nitpick_ignore = [
    # The type variable inside the Steps alias, which has no page of its own.
    ("py:obj", "calendaring_sync.adapters.base._T"),
]

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
# Each "Attributes:" entry becomes the field's own entry, with its type and a
# link target, and autodoc leaves out members without a docstring, so a
# dataclass field is listed once.
napoleon_use_ivar = False


def _public_ancestors():
    """The name of every public class a class of the package inherits from.

    A private base has no page, so what it defines, such as the CalDAV adapters'
    ``needs_full_listings``, shows on each public class built on it; members a
    public class defines show on that class's own page, and only there.
    """
    names = {"object"}
    for module_info in pkgutil.walk_packages(calendaring_sync.__path__, "calendaring_sync."):
        if any(part.startswith("_") or part == "tests" for part in module_info.name.split(".")):
            continue
        module = importlib.import_module(module_info.name)
        for value in vars(module).values():
            if inspect.isclass(value):
                names.update(c.__name__ for c in value.__mro__ if not c.__name__.startswith("_"))
    return names


autodoc_default_options = {
    "members": True,
    # Each module's source is grouped by area, so its page reads the same way.
    "member-order": "bysource",
    "show-inheritance": True,
    # Members inherited from these classes are left out, which leaves those of private bases.
    "inherited-members": ",".join(sorted(_public_ancestors())),
}

# The hooks an adapter implements, which the adapter base documents for its authors.
_ADAPTER_HOOKS = frozenset(
    {"_read", "_send", "_waits", "_new_object_id", "_edit_fields", "_gone", "_ask"}
)
autodoc_default_options["private-members"] = ",".join(sorted(_ADAPTER_HOOKS))
# Opaque values, documented without the fields their constructor takes.
_OPAQUE = frozenset({"calendaring_sync.store.memory.MemoryData"})


# A base as sphinx_autodoc_typehints hands it over, a rendered role such as
# :py:class:`~calendaring_sync.objects.push._Outcome`, whose last name is private.
_PRIVATE_BASE_RE = re.compile(r":py:class:`~?(?:\w+\.)*(_\w+)`")


def _public_bases(app, name, obj, options, bases):
    """Leave private base classes, such as the one every resolution shares, out of "Bases".

    Falls back to ``object`` when every base is private, so "Bases" is never empty.
    """
    bases[:] = [base for base in bases if not _is_private(base)] or [":py:class:`object`"]


def _is_private(base):
    """Whether a base is private, given as a class or, by sphinx_autodoc_typehints, a role."""
    if isinstance(base, str):
        return _PRIVATE_BASE_RE.fullmatch(base) is not None
    return getattr(base, "__name__", "").startswith("_")


def _adapter_hooks(app, what, name, obj, skip, options):
    """Show the adapter hooks on the adapter base only, not where an adapter implements them."""
    if name in _ADAPTER_HOOKS and getattr(obj, "__qualname__", "") != f"Adapter.{name}":
        return True
    return None


def _opaque_signature(app, what, name, obj, options, signature, return_annotation):
    """Show an opaque value's constructor without the fields it takes."""
    if name in _OPAQUE:
        return "()", return_annotation
    return None


def setup(app):
    app.connect("autodoc-process-bases", _public_bases)
    app.connect("autodoc-skip-member", _adapter_hooks)
    # Ahead of sphinx_autodoc_typehints, whose signature Sphinx would keep otherwise.
    app.connect("autodoc-process-signature", _opaque_signature, priority=400)
