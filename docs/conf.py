# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sphinx configuration for calendaring-sync documentation."""

import datetime as dt
import os

project = "calendaring-sync"
this_year = dt.date.today().year  # noqa: DTZ011
copyright = f"{this_year}, calendaring-sync contributors"  # noqa: A001
author = "calendaring-sync contributors"

extensions = [
    "notfound.extension",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
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
    "footer_start": ["copyright"],
    "footer_end": ["nlnet"],
    "logo": {"text": "calendaring-sync"},
    "show_toc_level": 2,
    "navbar_align": "content",
    "show_nav_level": 1,
    "navigation_with_keys": True,
    "collapse_navigation": False,
    "search_bar_text": "Search documentation",
    "secondary_sidebar_items": ["page-toc", "edit-this-page", "sourcelink"],
    "use_edit_page_button": True,
    "article_footer_items": ["last-updated"],
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
html_static_path = ["_static"]
# Custom fa-custom/fa-pypi icon used in icon_links above; sync-bootstrap-theme
# mirrors data-theme onto data-bs-theme so Bootstrap's own dark-mode variables
# (dropdown menus, tooltips) activate instead of staying stuck in light mode.
html_js_files = [
    ("js/custom-icons.js", {"defer": "defer"}),
    ("js/sync-bootstrap-theme.js", {"defer": "defer"}),
]
# Loaded as a separate <link> rather than @import inside pycal-theme.css:
# an @import must be the first rule in a stylesheet, and if that external
# request is ever blocked or slow (an ad blocker, a privacy extension, a
# flaky network), browsers can fail to parse the rest of the file along
# with it, taking every color override down with the font.
html_css_files = [
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@700&family=Cascadia+Code&display=swap",
    "css/pycal-theme.css",
    "css/content.css",
]

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

autodoc_default_options = {
    "members": True,
    # Each module's source is grouped by area, so its page reads the same way.
    "member-order": "bysource",
    "show-inheritance": True,
}


def _public_bases(app, name, obj, options, bases):
    """Leave private base classes, such as the one every resolution shares, out of "Bases"."""
    bases[:] = [base for base in bases if not base.__name__.startswith("_")]


def setup(app):
    app.connect("autodoc-process-bases", _public_bases)
