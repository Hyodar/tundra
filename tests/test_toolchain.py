"""The build toolchain: the mkosi pin, the tools tree, the lockfile section, drift."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.helpers import write_recipe_file
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend
from tundravm.backends.inprocess import InProcessBackend
from tundravm.cli import EXIT_FAILURE, EXIT_OK, main
from tundravm.measure import MEASURE_VERSION
from tundravm.toolchain import (
    MKOSI_PIN,
    MkosiTool,
    Toolchain,
    ToolsTree,
    compare_toolchains,
    describe_changes,
    installed_mkosi,
    lock_toolchain,
    running_toolchain,
    toolchain_drift,
)

PINNED = MkosiTool(MKOSI_PIN.version, MKOSI_PIN.commit)
TRIXIE = ToolsTree("debian", "trixie", "20251020T000000Z")


def test_the_installed_mkosi_is_the_pinned_commit() -> None:
    assert installed_mkosi() == PINNED


def test_backends_report_the_mkosi_they_run() -> None:
    local = running_toolchain(LocalLinuxBackend())
    lima = running_toolchain(LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB"))

    assert local.mkosi == lima.mkosi == PINNED
    assert local.measure == (
        ("gcp-firmware-table", "dstack-mr-gcp@b16e08b"),
        ("tundravm-measure", MEASURE_VERSION),
    )
    assert running_toolchain(InProcessBackend()).mkosi is None


def test_the_tools_tree_is_the_resolved_one_else_the_locks() -> None:
    backend = LocalLinuxBackend()
    locked = Toolchain("0.1.0", PINNED, TRIXIE)

    assert running_toolchain(backend).tools_tree is None
    assert running_toolchain(backend, locked=locked).tools_tree == TRIXIE
    backend.tools_tree = ToolsTree("debian", "trixie", "20251101T000000Z")
    assert running_toolchain(backend, locked=locked).tools_tree == backend.tools_tree


def test_lock_keeps_the_previous_tools_tree_unless_updated_or_given() -> None:
    previous = Toolchain("0.1.0", PINNED, TRIXIE)
    given = ToolsTree("debian", "forky", "20251201T000000Z")

    assert lock_toolchain(LocalLinuxBackend(), previous=previous).tools_tree == TRIXIE
    assert lock_toolchain(LocalLinuxBackend(), previous=previous, update=True).tools_tree is None
    assert lock_toolchain(LocalLinuxBackend(), tools_tree=given).tools_tree == given


def test_payload_round_trips_and_reads_mkosis_summary() -> None:
    toolchain = Toolchain("0.1.0+abc", PINNED, TRIXIE, (("tundravm-measure", "1"),))

    payload = toolchain.payload()

    assert payload["mkosi"] == {"version": MKOSI_PIN.version, "rev": MKOSI_PIN.commit}
    assert payload["tools_tree"] == {
        "distribution": "debian",
        "release": "trixie",
        "snapshot": "20251020T000000Z",
    }
    assert "nixpkgs" not in payload and "systemd" not in payload
    assert Toolchain.from_payload(payload) == toolchain
    summary = {"Distribution": "debian", "Release": "trixie", "Snapshot": "20251020T000000Z"}
    assert ToolsTree.from_summary(summary) == TRIXIE
    assert ToolsTree.from_summary(None) is None
    unpinned = ToolsTree.from_summary({"Distribution": "debian", "Release": "trixie"})
    assert unpinned is not None and unpinned.label() == "debian/trixie (unpinned snapshot)"


def test_compare_names_each_differing_field() -> None:
    a = Toolchain("0.1.0", MkosiTool("27.1", "a" * 40), TRIXIE)
    b = Toolchain("0.1.0", MkosiTool("26"), ToolsTree("debian", "trixie", "20250101T000000Z"))

    changes = compare_toolchains(a, b)

    assert describe_changes(changes) == (
        f"different (mkosi 27.1 @{'a' * 12} vs 26, tools_tree "
        "debian/trixie@20251020T000000Z vs debian/trixie@20250101T000000Z)"
    )
    assert describe_changes(compare_toolchains(a, a)) == "same"
    assert {d.severity for d in toolchain_drift(a, b)} == {"warning"}
    assert {d.severity for d in toolchain_drift(a, b, strict=True)} == {"error"}
    assert {d.code for d in toolchain_drift(a, b)} == {"toolchain-drift"}


def recipe_file(tmp_path: Path) -> Path:
    return write_recipe_file(
        tmp_path,
        """
from tundravm.backends import LocalLinuxBackend
from tundravm.declarative import Package, Recipe

backend = LocalLinuxBackend()
recipe = Recipe("app", items=(Package("linux-image-amd64"),))
""",
    )


def cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def _other_mkosi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tundravm.backends.local_linux.installed_mkosi", lambda: MkosiTool("28", "b" * 40)
    )


def test_lock_records_the_toolchain_and_lint_reports_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = recipe_file(tmp_path)
    lock = tmp_path / "app.lock"

    code, text = cli("lock", str(recipe), "--lockfile", str(lock))
    assert code == EXIT_OK
    assert f"toolchain: mkosi {MKOSI_PIN.version} @{MKOSI_PIN.commit[:12]}, gcp-firmware" in text
    assert f"tundravm-measure {MEASURE_VERSION}" in text
    recorded = json.loads(lock.read_text(encoding="utf-8"))
    assert recorded["version"] == 5
    assert recorded["toolchain"]["mkosi"] == {"version": "27.1", "rev": MKOSI_PIN.commit}

    _other_mkosi(monkeypatch)
    code, text = cli("lock", str(recipe), "--lockfile", str(lock), "--check")
    assert code == EXIT_OK
    assert (
        f"warning: toolchain-drift: toolchain mkosi: locked 27.1 @{MKOSI_PIN.commit[:12]}, "
        f"running 28 @{'b' * 12}"
    ) in text
    code, _ = cli("lock", str(recipe), "--lockfile", str(lock), "--check", "--strict")
    assert code == EXIT_FAILURE

    args = ("lint", str(recipe), "--lockfile", str(lock), "--format", "json")
    code, text = cli(*args)
    drift = [d for d in json.loads(text)["diagnostics"] if d["code"] == "toolchain-drift"]
    assert [d["severity"] for d in drift] == ["warning"]
    assert code == EXIT_OK
    assert cli(*args, "--strict")[0] == EXIT_FAILURE


def test_strict_toolchain_in_pyproject_makes_drift_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = recipe_file(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "recipe.py"\nlockfile = "app.lock"\nstrict_toolchain = true\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli("lock")[0] == EXIT_OK
    _other_mkosi(monkeypatch)

    code, text = cli("lock", "--check")

    assert code == EXIT_FAILURE
    assert "error: toolchain-drift: toolchain mkosi: locked 27.1" in text
    assert recipe.is_file()
