# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Running the documentation's examples as tests.

Every Python block on a docs page comes from a file in ``docs/examples``.
The unit tests run the examples that need no server; the integration tests
run the others against a running server, each with the server's details in
place of the example's placeholder ones.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

#: Where the examples are.
EXAMPLES = Path(__file__).resolve().parents[3] / "docs" / "examples"
#: The examples that talk to a server, run by the integration tests.
LIVE = frozenset({"quickstart", "caldav_sync", "async_sync", "jmap_sync", "every_calendar"})
#: The modules other examples import, not run on their own.
SHARED = frozenset({"memory_adapter", "start"})


def names() -> list[str]:
    """Every example's name."""
    return sorted(path.stem for path in EXAMPLES.glob("*.py"))


def load(name: str) -> ModuleType:
    """Import one of the shared example modules, as the examples do."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        # No bytecode is left in the docs folder.
        writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = writes
    return sys.modules[name]


def run(
    name: str, monkeypatch: pytest.MonkeyPatch, folder: Path, **settings: object
) -> dict[str, Any]:
    """Run an example in ``folder`` and return the names it left.

    Each keyword replaces the example's top-level assignment of that name,
    such as a server's URL.
    """
    source = (EXAMPLES / f"{name}.py").read_text(encoding="utf-8")
    for setting, value in settings.items():
        source, found = re.subn(
            rf"^{setting} = .*$", f"{setting} = {value!r}", source, flags=re.MULTILINE
        )
        assert found == 1, f"{name} has no {setting} to replace"
    monkeypatch.chdir(folder)
    monkeypatch.syspath_prepend(str(EXAMPLES))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    load("memory_adapter")
    # The starting state is new for every example.
    monkeypatch.delitem(sys.modules, "start", raising=False)
    namespace: dict[str, Any] = {"__name__": "__main__"}
    exec(compile(source, str(EXAMPLES / f"{name}.py"), "exec"), namespace)
    return namespace
