"""The fragment-plan examples in docs/testing.md and docs/module-authoring.md run as written."""

from __future__ import annotations

import inspect
import re
import sys
import types

import pytest

from tests.helpers import REPO_ROOT

DOCS = REPO_ROOT / "docs"
BLOCK = re.compile(r"^```python\n(.*?)^```", re.M | re.S)


def _blocks(name: str) -> list[str]:
    return BLOCK.findall((DOCS / name).read_text(encoding="utf-8"))


def _run_tests(source: str, origin: str) -> list[str]:
    """Execute *source* and call each zero-argument ``test_*`` function it defines."""
    namespace: dict[str, object] = {"__name__": origin}
    exec(compile(source, origin, "exec"), namespace)
    ran = []
    for name, value in namespace.items():
        if name.startswith("test_") and callable(value):
            if not inspect.signature(value).parameters:
                value()
                ran.append(name)
    return ran


def test_testing_md_plan_examples_run() -> None:
    ran = [
        test
        for block in _blocks("testing.md")
        if "def test_" in block and ("fragment_plan(" in block or "plan(RECIPE" in block)
        for test in _run_tests(block, "testing.md")
    ]
    assert {
        "test_app_is_clean",
        "test_app_ships_and_wires_its_service",
        "test_only_azure_ships_jq",
        "test_disk_without_its_key",
        "test_service_needs_its_user",
    } <= set(ran)


def test_module_authoring_testing_example_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    blocks = _blocks("module-authoring.md")
    (source,) = [b for b in blocks if b.startswith("# exporter.py")]
    exporter = types.ModuleType("exporter")
    monkeypatch.setitem(sys.modules, "exporter", exporter)
    exec(compile(source, "exporter.py", "exec"), exporter.__dict__)
    (tests,) = [b for b in blocks if "def test_" in b and "fragment_plan(" in b]
    assert len(_run_tests(tests, "test_exporter.py")) == 5
