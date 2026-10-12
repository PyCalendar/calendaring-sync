# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sphinx configuration for calendaring-sync documentation."""

import datetime
import os

project = "calendaring-sync"
this_year = datetime.date.today().year  # noqa: DTZ011
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
    "sphinxcontrib.mermaid",
    "sphinxext.opengraph",
]

html_baseurl = "https://calendaring-sync.readthedocs.io/"

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
issues_github_path = "pycalendar/calendaring-sync"

html_theme = "pydata_sphinx_theme"
html_theme_options = {
    "icon_links": [
        {
            "name": "GitHub",
            "url": "https://github.com/pycalendar/calendaring-sync",
            "icon": "fa-brands fa-square-github",
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
    "secondary_sidebar_items": ["page-toc"],
    "article_footer_items": ["last-updated"],
}
html_last_updated_fmt = "%Y-%m-%d"

html_context = {
    "github_user": "pycalendar",
    "github_repo": "calendaring-sync",
    "github_version": "main",
    "doc_path": "docs",
}

templates_path = ["_templates"]
html_static_path = ["_static"]
# sync-bootstrap-theme mirrors data-theme onto data-bs-theme so Bootstrap's own
# dark-mode variables (dropdown menus, tooltips) activate instead of staying
# stuck in light mode.
html_js_files = [
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

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
# Render "Attributes:" as :ivar: field-list entries instead of standalone
# attribute directives, which collide with autodoc's own dataclass field
# introspection and produce "duplicate object description" warnings.
napoleon_use_ivar = True

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
