"""The build toolchain: the mkosi pin, the tools tree, the lockfile section, drift."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.helpers import write_recipe_file
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend
from tundravm.cli import EXIT_OK, main
from tundravm.measure import MEASURE_VERSION
from tundravm.toolchain import (
    MKOSI_PIN,
    MkosiTool,
    Toolchain,
    ToolsTree,
    compare_toolchains,
    lock_toolchain,
    running_toolchain,
    toolchain_drift,
)

PINNED = MkosiTool(MKOSI_PIN.version, MKOSI_PIN.commit)
TRIXIE = ToolsTree("debian", "trixie", "20251020T000000Z")


def test_backends_report_the_mkosi_they_run() -> None:
    local = running_toolchain(LocalLinuxBackend())
    lima = running_toolchain(LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB"))

    assert local.mkosi == lima.mkosi == PINNED
    assert local.measure == (
        ("gcp-firmware-table", "dstack-mr-gcp@b16e08b"),
        ("tundravm-measure", MEASURE_VERSION),
    )


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

    assert [change.render() for change in changes] == [
        f"mkosi 27.1 @{'a' * 12} vs 26",
        "tools_tree debian/trixie@20251020T000000Z vs debian/trixie@20250101T000000Z",
    ]
    assert not compare_toolchains(a, a)
    assert {d.severity for d in toolchain_drift(a, b)} == {"warning"}
    assert {d.code for d in toolchain_drift(a, b)} == {"toolchain-drift"}


def image_file(tmp_path: Path) -> Path:
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


def test_lock_records_the_toolchain_and_check_reports_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = image_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TUNDRAVM_BACKEND")  # the file's own LocalLinuxBackend, not in-process
    lock = tmp_path / "build" / "tundravm.lock"

    code, text = cli("lock", str(recipe))
    assert code == EXIT_OK
    # the tools tree is the one the tree pins: the platform's release (no snapshot here)
    assert (
        f"toolchain: mkosi {MKOSI_PIN.version} @{MKOSI_PIN.commit[:12]}, "
        "tools debian/trixie (unpinned snapshot), gcp-firmware"
    ) in text
    assert f"tundravm-measure {MEASURE_VERSION}" in text
    recorded = json.loads(lock.read_text(encoding="utf-8"))
    assert recorded["version"] == 7
    assert recorded["toolchain"]["mkosi"] == {"version": "27.1", "rev": MKOSI_PIN.commit}
    assert recorded["toolchain"]["tools_tree"] == {
        "distribution": "debian",
        "release": "trixie",
        "snapshot": None,
    }

    _other_mkosi(monkeypatch)
    assert cli("lock", str(recipe), "--check")[0] == EXIT_OK  # the lock itself is current
    code, text = cli("check", str(recipe), "--json")
    drift = [d for d in json.loads(text)["diagnostics"] if d["code"] == "toolchain-drift"]
    assert [d["severity"] for d in drift] == ["warning"]
    assert code == EXIT_OK
    assert drift[0]["message"].startswith(
        f"toolchain mkosi: locked 27.1 @{MKOSI_PIN.commit[:12]}, running 28 @{'b' * 12}"
    )
