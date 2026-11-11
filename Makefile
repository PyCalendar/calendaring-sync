# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
.DEFAULT_GOAL   = help
SHELL           = bash

# You can set these variables from the command line.
SPHINXOPTS      ?=
TOWNCRIEROPTS   ?=
BENCHMARKOPTS   ?=
# The Python a new environment uses, by default the one in .python-version, and
# the dependency groups it installs. The docs need Python 3.13 or later, so the
# dev group leaves them out on an older one; CI's jobs each install only what they
# run, for example `make init PYTHONVERSION=3.11 UV_GROUPS="--no-default-groups --group test"`.
PYTHONVERSION   ?= $(shell cat .python-version)
UV_GROUPS       ?= --group dev

# Internal variables.
# Every tool runs through uv, which finds the environment on any platform, and
# without syncing, so a test environment never gains the docs group.
RUN             = uv run --no-sync --python "$(PYTHONVERSION)" $(UV_GROUPS)
SYNC            = uv sync --python "$(PYTHONVERSION)" $(UV_GROUPS)
DOCS_DIR        = ./docs/
BUILDDIR        = ../_build
ALLSPHINXOPTS   = -W -d $(BUILDDIR)/doctrees $(SPHINXOPTS) .
SOURCE          = ../src/calendaring_sync/
APIBUILDDIR     = reference/api
# A make variable isn't in the recipe's environment, so sphinx-apidoc gets the
# options through --automodule-options instead of SPHINX_APIDOC_OPTIONS. A field
# is documented by its class's Attributes section, so undoc-members would list it
# a second time.
SPHINX_APIDOC_OPTIONS = ignore-module-all,members,show-inheritance
TESTS           = src/calendaring_sync/tests
INTEGRATION     = $(TESTS)/test_caldav_integration.py $(TESTS)/test_jmap_integration.py
FIXTURES        = stalwart cyrus baikal
VALEFILES       := $(shell find $(DOCS_DIR) -type f -name "*.rst" -print)
VALEOPTS        ?=

.PHONY: help
help:  ## This help message
	@grep -E '^[a-zA-Z_.-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


# environment management
.venv:  ## Install Python, create the virtual environment, and install the dependency groups
	@uv python install "$(PYTHONVERSION)"
	@uv venv --python "$(PYTHONVERSION)"
	@$(SYNC)
	$(if $(findstring dev,$(UV_GROUPS)),@$(RUN) pre-commit install)

.PHONY: sync
sync:  ## Install the dependency groups again, after pyproject.toml changes
	@$(SYNC)

.PHONY: init
init: clean clean-python .venv  ## Clean the docs build and the virtual environment, then create the environment again

.PHONY: clean
clean:  ## Clean the docs build directory
	cd $(DOCS_DIR) && rm -rf $(BUILDDIR)/

.PHONY: clean-python
clean-python: clean
	rm -rf .venv/
# /environment management


# documentation builders
.PHONY: html
html: .venv apidoc  ## Build the HTML documentation
	cd $(DOCS_DIR) && $(RUN) sphinx-build -b html $(ALLSPHINXOPTS) $(BUILDDIR)/html
	@echo
	@echo "Build finished. The HTML pages are in $(BUILDDIR)/html."

.PHONY: apidoc
apidoc: .venv  ## Build the .rst stub pages of the API reference from the source
	cd $(DOCS_DIR) && $(RUN) sphinx-apidoc \
		--templatedir=_templates/apidoc \
		--force \
		--remove-old \
		--separate \
		--module-first \
		--automodule-options=$(SPHINX_APIDOC_OPTIONS) \
		-o $(APIBUILDDIR) \
		$(SOURCE) \
		$(SOURCE)tests
	@echo
	@echo "Build finished. The .rst stub pages are in $(DOCS_DIR)$(APIBUILDDIR)."

.PHONY: livehtml
livehtml: .venv apidoc  ## Rebuild the documentation on changes, with live reload in the browser
	cd $(DOCS_DIR) && $(RUN) sphinx-autobuild \
		--watch "../src/calendaring_sync/" \
		--ignore "../src/calendaring_sync/tests" \
		--port 8050 \
		-b html . "$(BUILDDIR)/html" $(SPHINXOPTS)
# /documentation builders


# test
.PHONY: linkcheck
linkcheck: .venv apidoc  ## Check every link in the documentation
	cd $(DOCS_DIR) && $(RUN) sphinx-build -b linkcheck $(ALLSPHINXOPTS) $(BUILDDIR)/linkcheck
	@echo
	@echo "Link check complete; look for any errors in the above output or in $(BUILDDIR)/linkcheck/."

# Passes only when sphinx-build succeeds and grep finds no broken or redirected link.
# sphinx-build colors its output on Windows even into a pipe, which grep wouldn't match.
.PHONY: linkcheckbroken
linkcheckbroken: .venv apidoc  ## Check every link in the documentation, and show only the broken ones
	cd $(DOCS_DIR) && $(RUN) sphinx-build --no-color -b linkcheck $(ALLSPHINXOPTS) $(BUILDDIR)/linkcheck | GREP_COLORS='0;31' grep -wi "[^.]broken\|redirect" --color=always; \
		status=("$${PIPESTATUS[@]}"); test "$${status[0]}" = 0 && test "$${status[1]}" = 1
	@echo
	@echo "Link check complete; look for any errors in the above output or in $(BUILDDIR)/linkcheck/."

.PHONY: vale
vale: .venv apidoc  ## Run Vale style, grammar, and spell checks
	@$(RUN) vale sync
	@$(RUN) vale --no-wrap $(VALEOPTS) $(VALEFILES); \
	if [ $$? = 0 ]; then \
		echo; \
		echo "Vale passed!"; \
	else \
		echo; \
		echo "Vale spell, style, and grammar check failed."; \
		echo "Read the error messages above to see what didn't pass."; \
		echo "For guidance on correcting the errors, see:"; \
		echo "https://calendaring-sync.readthedocs.io/en/stable/contribute.html#check-prose-with-vale"; \
		exit 1; \
	fi

.PHONY: docs-all
docs-all: .venv apidoc clean vale html linkcheckbroken  ## Clean the docs build, then run Vale, build the HTML and check the links

.PHONY: test
test: .venv  ## Run the unit tests and the integration tests, skipping servers that aren't running
	$(RUN) pytest

.PHONY: test-unit
test-unit: .venv  ## Run the unit tests
	$(RUN) pytest $(TESTS) --ignore-glob="*_integration.py"

.PHONY: test-coverage
test-coverage: .venv  ## Run the unit tests with coverage, as CI does
	$(RUN) pytest --cov --cov-branch --cov-report=xml \
		--junitxml=junit.xml -o junit_family=legacy \
		$(TESTS) --ignore-glob="*_integration.py"

.PHONY: test-integration
test-integration: .venv  ## Start the server fixtures, then run the integration tests
	bash tests/docker/start.sh $(FIXTURES)
	$(RUN) pytest $(INTEGRATION)

.PHONY: type-check
type-check: .venv  ## Run type checking
	$(RUN) mypy src/calendaring_sync docs/examples tests/docker

.PHONY: lint
lint: .venv  ## Check the code with ruff, changing nothing
	$(RUN) ruff check .
	$(RUN) ruff format --check .

.PHONY: benchmark
benchmark: .venv  ## Run the benchmark; compare it with main on the same machine
	$(RUN) python -m calendaring_sync.tests.benchmark $(BENCHMARKOPTS)
# /test


# development
.PHONY: dev
dev: .venv  ## Install Python, create the virtual environment, and install every dependency group

.PHONY: format
format: .venv  ## Format the code base with ruff, and fix what it can
	$(RUN) ruff format
	$(RUN) ruff check --fix
# /development


# deployment
.PHONY: rtd-prepare
rtd-prepare:  ## Install uv on Read the Docs
	asdf plugin add uv
	asdf install uv latest
	asdf global uv latest

.PHONY: rtd-pr-preview
rtd-pr-preview: rtd-prepare .venv apidoc  ## Build the documentation on Read the Docs
	cd $(DOCS_DIR) && $(RUN) sphinx-build -b html $(ALLSPHINXOPTS) $(READTHEDOCS_OUTPUT)/html/
# /deployment


# release
.PHONY: changes-check
changes-check: .venv  ## Check the branch has a news fragment
	$(RUN) towncrier check $(TOWNCRIEROPTS)
# /release
