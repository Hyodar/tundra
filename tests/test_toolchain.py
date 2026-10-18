"""The build toolchain: probes, the lockfile's toolchain section, drift and comparison."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.conftest import FAKE_NIXPKGS_REV
from tests.helpers import write_recipe_file
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend
from tundravm.backends.inprocess import InProcessBackend
from tundravm.cli import EXIT_FAILURE, EXIT_OK, main
from tundravm.measure import MEASURE_VERSION
from tundravm.toolchain import (
    MkosiTool,
    NixpkgsPin,
    Toolchain,
    compare_toolchains,
    describe_changes,
    lock_toolchain,
    running_toolchain,
    toolchain_drift,
    version_token,
)

VERSIONS = {"mkosi": "mkosi 25.3", "ukify": "ukify 257 (257.4-1)", "measured-boot": "v1.2.0"}


def host(versions: dict[str, str]):  # type: ignore[no-untyped-def]
    """A probe runner answering ``<tool> --version`` from *versions*; others are missing."""

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        text = versions.get(argv[0])
        if text is None:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(list(argv), 0, stdout=text + "\n", stderr="")

    return run


@pytest.fixture(autouse=True)
def _no_pip_mkosi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tundravm.toolchain.installed_version", lambda name: None)


def test_version_token_reads_common_version_lines() -> None:
    assert version_token("mkosi 25.3") == "25.3"
    assert version_token("systemd 257 (257.4-1)\n+PAM +AUDIT") == "257"
    assert version_token("v1.2.0") == "1.2.0"


def test_local_backend_probes_host_mkosi_systemd_and_measure_tools() -> None:
    found = running_toolchain(LocalLinuxBackend(), runner=host(VERSIONS))

    assert found.mkosi == MkosiTool("25.3", "host")
    assert found.systemd == "257"
    assert found.measure == (
        ("gcp-firmware-table", "dstack-mr-gcp@b16e08b"),
        ("tundravm-measure", MEASURE_VERSION),
    )
    assert found.nixpkgs is None


def test_nix_backend_lock_resolves_nixpkgs_then_keeps_the_pin() -> None:
    first = lock_toolchain(NixMkosiBackend(), runner=host({}))
    assert first.nixpkgs is not None and first.nixpkgs.rev == FAKE_NIXPKGS_REV
    assert first.mkosi == MkosiTool("25.3", "nix", FAKE_NIXPKGS_REV)
    assert first.systemd == "257"

    older = Toolchain("0.1.0", MkosiTool("24", "nix", "2" * 40), "256", NixpkgsPin("2" * 40, "h"))
    kept = lock_toolchain(NixMkosiBackend(), previous=older, runner=host({}))
    assert kept.nixpkgs == older.nixpkgs and kept.mkosi == older.mkosi

    updated = lock_toolchain(NixMkosiBackend(), previous=older, update=True, runner=host({}))
    assert updated.nixpkgs is not None and updated.nixpkgs.rev == FAKE_NIXPKGS_REV


def test_offline_nix_lock_records_why_nixpkgs_is_not_pinned() -> None:
    found = lock_toolchain(NixMkosiBackend(), offline=True, runner=host({}))

    assert found.nixpkgs is None
    assert found.unpinned is not None and "offline" in found.unpinned
    assert Toolchain.from_payload(found.payload()) == found


def test_lima_rosetta_is_recorded_as_not_reproducible() -> None:
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB", rosetta=True)
    found = running_toolchain(backend, runner=host({}))

    assert found.rosetta
    assert found.payload()["reproducible"] == "not guaranteed (rosetta)"
    assert running_toolchain(InProcessBackend(), runner=host({})).mkosi is None


def test_compare_names_each_differing_field() -> None:
    a = Toolchain("0.1.0", MkosiTool("26", "nix"), "257")
    b = Toolchain("0.1.0", MkosiTool("25", "nix"), "256")

    changes = compare_toolchains(a, b)

    assert describe_changes(changes) == "different (mkosi 26 (nix) vs 25 (nix), systemd 257 vs 256)"
    assert describe_changes(compare_toolchains(a, a)) == "same"
    assert {d.severity for d in toolchain_drift(a, b)} == {"warning"}
    assert {d.severity for d in toolchain_drift(a, b, strict=True)} == {"error"}
    assert {d.code for d in toolchain_drift(a, b)} == {"toolchain-drift"}


def recipe_file(tmp_path: Path, backend: str) -> Path:
    return write_recipe_file(
        tmp_path,
        f"""
from tundravm.backends import {backend}
from tundravm.declarative import Package, Recipe

backend = {backend}()
recipe = Recipe("app", items=(Package("linux-image-amd64"),))
""",
    )


def cli(*argv: str, versions: dict[str, str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out, runner=host(versions))
    return code, out.getvalue()


def test_lock_records_the_toolchain_and_lint_reports_drift(tmp_path: Path) -> None:
    recipe = recipe_file(tmp_path, "LocalLinuxBackend")
    lock = tmp_path / "app.lock"

    code, text = cli("lock", str(recipe), "--lockfile", str(lock), versions=VERSIONS)
    assert code == EXIT_OK
    assert "toolchain: mkosi 25.3 (host), systemd 257, gcp-firmware-table" in text
    assert f"tundravm-measure {MEASURE_VERSION}" in text
    recorded = json.loads(lock.read_text(encoding="utf-8"))
    assert recorded["version"] == 5
    assert recorded["toolchain"]["mkosi"] == {"source": "host", "version": "25.3"}
    assert recorded["toolchain"]["systemd"] == "257"

    newer = {**VERSIONS, "mkosi": "mkosi 26"}
    code, text = cli("lock", str(recipe), "--lockfile", str(lock), "--check", versions=newer)
    assert code == EXIT_OK
    assert (
        "warning: toolchain-drift: toolchain mkosi: locked 25.3 (host), running 26 (host)" in text
    )
    code, _ = cli(
        "lock", str(recipe), "--lockfile", str(lock), "--check", "--strict", versions=newer
    )
    assert code == EXIT_FAILURE

    args = ("lint", str(recipe), "--lockfile", str(lock), "--format", "json")
    code, text = cli(*args, versions=newer)
    drift = [d for d in json.loads(text)["diagnostics"] if d["code"] == "toolchain-drift"]
    assert [d["severity"] for d in drift] == ["warning"]
    assert code == EXIT_OK
    assert cli(*args, "--strict", versions=newer)[0] == EXIT_FAILURE
    assert cli(*args, versions=VERSIONS)[0] == EXIT_OK


def test_strict_toolchain_in_pyproject_makes_drift_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = recipe_file(tmp_path, "LocalLinuxBackend")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "recipe.py"\nlockfile = "app.lock"\nstrict_toolchain = true\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli("lock", versions=VERSIONS)[0] == EXIT_OK

    code, text = cli("lock", "--check", versions={**VERSIONS, "ukify": "ukify 258"})

    assert code == EXIT_FAILURE
    assert "error: toolchain-drift: toolchain systemd: locked 257, running 258" in text
    assert recipe.is_file()


def test_nix_lock_pins_nixpkgs_and_update_toolchain_resolves_again(tmp_path: Path) -> None:
    recipe = recipe_file(tmp_path, "NixMkosiBackend")
    lock = tmp_path / "app.lock"

    code, text = cli("lock", str(recipe), "--lockfile", str(lock), versions={})

    assert code == EXIT_OK
    assert f"mkosi 25.3 (nix @{FAKE_NIXPKGS_REV[:12]}), systemd 257" in text
    toolchain = json.loads(lock.read_text(encoding="utf-8"))["toolchain"]
    assert toolchain["nixpkgs"]["rev"] == FAKE_NIXPKGS_REV
    assert toolchain["nixpkgs"]["narHash"].startswith("sha256-")
    code, text = cli("lock", str(recipe), "--lockfile", str(lock), "--check", versions={})
    assert (code, "toolchain-drift" in text) == (EXIT_OK, False)
    code, _ = cli(
        "lock", str(recipe), "--lockfile", str(lock), "--update", "toolchain", versions={}
    )
    assert code == EXIT_OK
