# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The package as a whole: its exports, type checks, doctests, extras, recorders and benchmarks."""

from __future__ import annotations

import dataclasses
import doctest
import importlib
import inspect
import pkgutil
import runpy
import sys
from collections.abc import Callable
from typing import Literal
from unittest.mock import patch

import pytest

import calendaring_sync
from calendaring_sync import (
    Applied,
    Changed,
    ChangeSet,
    EditFields,
    FeedEntryKind,
    LogicalKeyTaken,
    MemoryStore,
    Merge,
    RecordState,
    Replica,
    Session,
    SQLiteStore,
    StateStore,
)
from calendaring_sync._checks import _CHECKED_WHEN_BUILT, _fields, argument_check
from calendaring_sync.adapters.base import Adapter, AsyncAdapter, SyncAdapter
from calendaring_sync.adapters.caldav import AsyncCalDAVAdapter, CalDAVAdapter
from calendaring_sync.adapters.jmap import AsyncJMAPAdapter, JMAPAdapter
from calendaring_sync.tests import benchmark
from calendaring_sync.tests._tape import Tape

# Examples that continue from the replica the Replica class example builds.
_CONTINUING = ("calendaring_sync.replica.Replica.", "calendaring_sync.session.Session")


def _docstring_examples() -> list[doctest.DocTest]:
    """Every docstring example in the package; a replica method's start from the class's."""
    finder = doctest.DocTestFinder()
    tests = [
        test
        for module in pkgutil.walk_packages(calendaring_sync.__path__, "calendaring_sync.")
        if ".tests" not in module.name
        for test in finder.find(importlib.import_module(module.name))
        if test.examples
    ]
    start = next(t for t in tests if t.name == "calendaring_sync.replica.Replica").examples
    for test in tests:
        if test.name.startswith(_CONTINUING):
            test.examples = start + test.examples
    return tests


class TestPackage:
    """The package as a whole."""

    def test_exports_resolve(self):
        for name in calendaring_sync.__all__:
            assert getattr(calendaring_sync, name) is not None

    def test_version_is_set(self):
        assert isinstance(calendaring_sync.__version__, str)
        assert calendaring_sync.__version__

    @pytest.mark.parametrize("test", _docstring_examples(), ids=lambda test: test.name)
    def test_every_docstring_example_runs(self, test):
        assert doctest.DocTestRunner().run(test).failed == 0

    def test_every_entry_point_checks_its_types(self):
        for cls in (
            Replica,
            Session,
            StateStore,
            MemoryStore,
            SQLiteStore,
            Adapter,
            SyncAdapter,
            AsyncAdapter,
            CalDAVAdapter,
            AsyncCalDAVAdapter,
            JMAPAdapter,
            AsyncJMAPAdapter,
        ):
            for name, func in vars(cls).items():
                if name.startswith("_") and name != "__init__":
                    continue
                if isinstance(func, property):
                    # A property that only reads takes no argument to check.
                    assert func.fset is None, f"{cls.__name__}.{name} has a setter"
                    continue
                assert inspect.isfunction(func), f"{cls.__name__}.{name} isn't checked"
                assert getattr(func, "_checks_arguments", False), f"{cls.__name__}.{name}"
                # Builds the check; an annotation it can't read raises here.
                argument_check(inspect.unwrap(func))
        exported = [getattr(calendaring_sync, name) for name in calendaring_sync.__all__]
        for cls in exported:
            if isinstance(cls, type) and dataclasses.is_dataclass(cls):
                _fields(cls)
        # The values an adapter or application builds check themselves.
        assert _CHECKED_WHEN_BUILT == {
            Changed,
            ChangeSet,
            Applied,
            LogicalKeyTaken,
            Merge,
            EditFields,
        }

    def test_a_check_refuses_what_it_cant_read(self):
        def takes_many(*values: str) -> None: ...

        def takes_a_literal(kind: Literal["a"]) -> None: ...

        def takes_anything(value) -> None: ...

        refused: list[tuple[Callable[..., object], str]] = [
            (takes_many, r"takes \*values, which isn't checked"),
            (takes_a_literal, "^Can't check a value against"),
            (takes_anything, "takes value without a type to check$"),
        ]
        for func, match in refused:
            with pytest.raises(TypeError, match=match):
                argument_check(func)

    @pytest.mark.parametrize(
        "adapter, dependency, extra",
        [
            ("calendaring_sync.adapters.caldav", "caldav.calendarobjectresource", "caldav"),
            ("calendaring_sync.adapters.jmap", "calendaring_jmap", "jmap"),
        ],
    )
    def test_an_adapter_without_its_extra_names_it_on_import(self, adapter, dependency, extra):
        # A module set to None in sys.modules can't be imported, as if it
        # weren't installed; the adapter is imported again under that.
        importlib.import_module(adapter)
        with patch.dict(sys.modules, {dependency: None, adapter: None}):
            del sys.modules[adapter]
            with pytest.raises(ImportError, match=rf"pip install 'calendaring-sync\[{extra}\]'"):
                importlib.import_module(adapter)

    def test_benchmarks_run_and_measure_what_they_say(self, monkeypatch, capsys):
        size = 200
        # Run the module as the contributing guide says to, at a small size.
        monkeypatch.setattr(sys, "argv", ["benchmark", "--size", str(size), "--runs", "1"])
        monkeypatch.delitem(sys.modules, benchmark.__name__)
        runpy.run_module("calendaring_sync.tests.benchmark", run_name="__main__")
        printed = capsys.readouterr().out.splitlines()
        assert [line.split(",")[0] for line in printed] == list(benchmark.SCENARIOS)
        edited = size // benchmark.EDITED_SHARE
        conflicted = edited // benchmark.CONFLICTED_SHARE
        assert benchmark.states_over_edits(size) == {
            RecordState.SYNCED: size - edited,
            RecordState.PENDING: edited - conflicted,
            RecordState.CONFLICT: conflicted,
        }
        assert benchmark.entries_after_pushes(size) == {FeedEntryKind.PUSHED: edited}

    @pytest.mark.parametrize("protocol, server", [("caldav", "cyrus"), ("jmap", "stalwart")])
    def test_a_recorder_records_every_recording_of_a_server(self, monkeypatch, protocol, server):
        # Run the module as its docstring says to.
        recorder = f"calendaring_sync.tests.record_{protocol}"
        scenarios = importlib.import_module(f"calendaring_sync.tests._{protocol}_scenarios")
        importlib.import_module(recorder)
        monkeypatch.setattr(sys, "argv", [recorder, server])
        monkeypatch.delitem(sys.modules, recorder)
        with patch("calendaring_sync.tests._scenarios.run") as recorded, patch.object(Tape, "save"):
            runpy.run_module(recorder, run_name="__main__")
        assert [c.args[2] for c in recorded.call_args_list] == [
            s for name, s in scenarios.RECORDINGS if name == server
        ]
