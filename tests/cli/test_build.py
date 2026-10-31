"""``tundravm build``: the immutable build directory, the lock it freezes against, the
targets, ``--tree-only``, ``--reproduce``, ``--offline``."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.cli.conftest import STARTER, Cli
from tundravm import builds
from tundravm.builds import Build
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import lifecycle
from tundravm.errors import StateError
from tundravm.manifest import sha256_file

LAYOUT = {
    "payload.efi",
    "qemu.raw",
    "measurements.json",
    "sbom.spdx.json",
    "manifest.json",
    "observations.json",
    "inputs/tundravm.lock",
    "inputs/summary.json",
    "inputs/sources.json",
    "inputs/toolchain.json",
    "inputs/mkosi-argv.json",
}


def _files(root: Path) -> dict[str, tuple[bytes, float, int]]:
    """Every file under *root* (symlinks as their target): bytes, mtime, mode."""
    found: dict[str, tuple[bytes, float, int]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            found[rel] = (os.readlink(path).encode(), info.st_mtime, info.st_mode)
        elif path.is_file():
            found[rel] = (path.read_bytes(), info.st_mtime, info.st_mode)
    return found


def test_the_build_directory_layout(built: Build, project: Path) -> None:
    files = set(_files(built.path))
    assert LAYOUT <= files
    assert any(name.startswith("tree/payload/") for name in files)
    assert not {"azure.vhd", "gcp.tar.gz", "manifest.sig", "packages.json"} & files
    assert built.path.parent == project / "build" / "demo" / "production"
    assert built.path.name == built.id
    latest = project / "build" / "demo" / "latest-production"
    assert latest.is_symlink() and os.readlink(latest) == f"production/{built.id}"
    assert built.kind == "production" and built.profile == "production"
    assert built.targets == ("qemu",)


def test_every_file_is_read_only(built: Build) -> None:
    for path in built.path.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert not stat.S_IMODE(path.stat().st_mode) & 0o222, path


def test_the_output_names_the_directory_and_what_it_does_not_check(project: Path, cli: Cli) -> None:
    result = cli("build")
    assert result.code == EXIT_OK, result.err
    lines = result.out.splitlines()
    assert lines[0] == "lock written: build/tundravm.lock (commit it)"
    assert lines[1].startswith("built build/demo/production/")
    assert any(line.startswith("  qemu.raw ") for line in lines)
    assert "  unchecked: measurements qemu/uki: unknown" in lines  # the fake UKI is no PE
    assert "[production]" in result.err  # progress goes to stderr


def test_an_identical_build_leaves_the_directory_untouched(
    built: Build, project: Path, cli: Cli
) -> None:
    before = _files(built.path)
    observations = (built.path / "observations.json").read_bytes()
    again = cli("build")
    assert again.code == EXIT_OK
    assert (
        f"already built (identical; left untouched) {built.path.relative_to(project)}" in again.out
    )
    assert "lock written" not in again.out
    assert _files(built.path) == before
    assert (built.path / "observations.json").read_bytes() == observations
    assert [p.name for p in (project / "build/demo/production").iterdir()] == [built.id]


def test_a_stale_lock_is_refused_and_left_alone(built: Build, project: Path, cli: Cli) -> None:
    lock = project / "build" / "tundravm.lock"
    before = lock.read_bytes()
    (project / "demo.py").write_text(STARTER.replace('"busybox"]', '"busybox", "jq"]'))
    result = cli("build")
    assert result.code == EXIT_SDK_ERROR
    assert result.err.startswith("error [E_LOCKFILE]: The lockfile build/tundravm.lock is stale")
    assert "A build never updates the lock" in result.err
    assert lock.read_bytes() == before
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    assert len(list((project / "build/demo/production").iterdir())) == 2


def test_the_dev_profile_is_a_development_build(dev_built: Build, project: Path) -> None:
    assert dev_built.kind == "development" and dev_built.profile == "dev"
    assert dev_built.path.parent == project / "build" / "demo" / "development"
    latest = project / "build" / "demo" / "latest-development"
    assert os.readlink(latest) == f"development/{dev_built.id}"
    assert "development build: never production evidence" in dev_built.manifest["unchecked"]
    argv = json.loads((dev_built.path / "inputs" / "mkosi-argv.json").read_text())
    assert argv == {"argv": ["--profile=dev"]}


def test_profile_and_dev_are_exclusive(project: Path, cli: Cli) -> None:
    with pytest.raises(SystemExit):
        cli("build", "--profile", "production", "--dev")


def test_an_unknown_profile_is_refused(project: Path, cli: Cli) -> None:
    result = cli("build", "--profile", "staging")
    assert result.code == EXIT_SDK_ERROR
    assert "Unknown profile(s): staging" in result.err


def test_targets_add_the_cloud_disks(project: Path, cli: Cli) -> None:
    refused = cli("build", "--target", "gcp")  # the starter targets qemu only
    assert refused.code == EXIT_SDK_ERROR and "does not target gcp" in refused.err
    (project / "demo.py").write_text(EVERY_CLOUD, encoding="utf-8")
    result = cli("build", "--target", "azure,gcp")
    assert result.code == EXIT_OK, result.err
    build = Build.open((project / "build/demo/latest-production").resolve())
    assert build.targets == ("qemu", "azure", "gcp")
    for name in ("qemu.raw", "azure.vhd", "gcp.tar.gz"):
        assert (build.path / name).is_file()
        assert name in build.manifest["artifacts"]
    assert build.artifact("gcp") == build.path / "gcp.tar.gz"
    repeated = cli("build", "--target", "gcp", "--target", "azure")
    assert repeated.code == EXIT_OK
    assert "already built" in repeated.out  # the order --target names them in is not identity
    again = Build.open((project / "build/demo/latest-production").resolve())
    assert again.targets == ("qemu", "azure", "gcp") and again.id == build.id


def test_an_unknown_target_is_refused(project: Path, cli: Cli) -> None:
    result = cli("build", "--target", "aws")
    assert result.code == EXIT_SDK_ERROR
    assert "Unknown target(s): aws" in result.err


def test_tree_only_writes_a_tree_and_no_build(project: Path, cli: Cli) -> None:
    result = cli("build", "--tree-only", "tree-out", "--dev")
    assert result.code == EXIT_OK, result.err
    assert (project / "tree-out" / "mkosi.conf").is_file()
    assert (project / "tree-out" / "payload" / "mkosi.profiles" / "dev").is_dir()
    assert result.out.splitlines()[1] == "  dev: mkosi -C tree-out --profile=dev build"
    assert not (project / "build" / "demo").exists()
    refused = cli("build", "--tree-only", "x", "--reproduce")
    assert refused.code == EXIT_SDK_ERROR and "--tree-only builds nothing" in refused.err


def test_reproduce_records_a_match(project: Path, cli: Cli) -> None:
    result = cli("build", "--reproduce")
    assert result.code == EXIT_OK, result.err
    assert "reproduction: matched" in result.out
    build = Build.open((project / "build/demo/latest-production").resolve())
    observed = json.loads((build.path / "observations.json").read_text())
    record = observed["reproduction"]
    assert record["state"] == "matched" and record["differ"] == []
    assert set(record["artifacts"]) == {"payload.efi", "qemu.raw"}
    assert all(pair["first"] == pair["second"] for pair in record["artifacts"].values())
    assert not (project / "build" / "demo" / builds.REPRODUCE).exists()


def test_a_second_workspace_that_cannot_be_removed_is_kept_and_said(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sudo build leaves root-owned files in its workspace; a matched reproduction keeps
    it then, records where, and the next --reproduce names the fix."""
    remove = builds._remove

    def stubborn(path: Path) -> None:
        if path.name == builds.REPRODUCE:
            raise StateError(f"Cannot remove {path}.", hint=f"sudo rm -rf -- {path}")
        remove(path)

    monkeypatch.setattr(builds, "_remove", stubborn)
    result = cli("build", "--reproduce")
    assert result.code == EXIT_OK, result.err
    build = Build.open((project / "build/demo/latest-production").resolve())
    record = json.loads((build.path / "observations.json").read_text())["reproduction"]
    assert record["state"] == "matched" and record["workspace"] == builds.REPRODUCE
    again = cli("build", "--reproduce")
    assert again.code == EXIT_SDK_ERROR and "sudo rm -rf" in again.err


def test_a_reproduction_that_differs_exits_1_and_is_still_published(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = builds._digests
    calls: list[int] = []

    def digests(artifacts: Any) -> dict[str, str]:
        found = real(artifacts)
        calls.append(1)
        if len(calls) == 2:  # the second build's
            found["qemu.raw"] = "0" * 64
        return found

    monkeypatch.setattr(builds, "_digests", digests)
    result = cli("build", "--reproduce")
    assert result.code == EXIT_FAILURE
    assert "reproduction: different (qemu.raw)" in result.out
    build = Build.open((project / "build/demo/latest-production").resolve())
    observed = json.loads((build.path / "observations.json").read_text())
    assert observed["reproduction"]["state"] == "different"
    assert observed["reproduction"]["differ"] == ["qemu.raw"]
    assert observed["reproduction"]["workspace"] == builds.REPRODUCE
    assert (project / "build" / "demo" / builds.REPRODUCE).is_dir()  # kept for comparison


def test_offline_without_a_lock_is_refused(project: Path, cli: Cli) -> None:
    result = cli("build", "--offline")
    assert result.code == EXIT_SDK_ERROR
    assert "an offline build builds only what the lock pins" in result.err
    assert not (project / "build" / "tundravm.lock").exists()


def test_offline_builds_from_what_an_online_build_fetched(project: Path, cli: Cli) -> None:
    assert cli("lock").code == EXIT_OK
    refused = cli("build", "--offline")
    assert refused.code == EXIT_SDK_ERROR
    assert "Cannot fetch 'tdxs': the build is offline." in refused.err
    assert cli("build").code == EXIT_OK
    result = cli("build", "--offline")
    assert result.code == EXIT_OK, result.err


def test_the_build_root_follows_the_environment(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUNDRAVM_BUILD", "elsewhere")
    assert cli("build").code == EXIT_OK
    assert (project / "elsewhere" / "tundravm.lock").is_file()
    assert (project / "elsewhere" / "demo" / "latest-production" / "manifest.json").is_file()


def test_clean_cache_drops_the_workspace(built: Build, project: Path, cli: Cli) -> None:
    marker = project / "build" / "demo" / builds.WORK / ".sources" / "marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("x")
    assert cli("build").code == EXIT_OK
    assert marker.exists()  # an ordinary build keeps the fetched sources
    assert cli("build", "--clean-cache").code == EXIT_OK
    assert not marker.exists()


def test_the_tools_tree_beside_the_config_is_kept_and_never_published(
    built: Build, project: Path, cli: Cli
) -> None:
    """mkosi leaves its tools tree in the tree's directory (``mkosi.tools``): a build keeps
    it for the next one and leaves it out of the build directory's ``tree/``."""
    tools = project / "build" / "demo" / builds.WORK / "mkosi" / "mkosi.tools" / "usr" / "bin"
    tools.mkdir(parents=True)
    (tools / "ukify").write_text("#!/bin/sh\n")
    stale = project / "build" / "demo" / builds.WORK / "mkosi" / "payload" / "stale.conf"
    stale.write_text("x")
    (project / "demo.py").write_text(STARTER.replace("<h1>demo</h1>", "<h1>again</h1>"))
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    again = Build.open((project / "build/demo/latest-production").resolve())
    assert again.id != built.id
    assert not (again.path / "tree" / "mkosi.tools").exists()
    assert not (again.path / "tree" / "payload" / "stale.conf").exists()
    assert (tools / "ukify").is_file()


def test_the_payloads_package_manifest_is_bound(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkosi names the payload subimage's manifest ``payload.manifest``: the build copies it
    to ``packages.json`` and the SBOM lists its packages, bound."""
    bake = lifecycle.bake_image
    payload = {
        "manifest_version": 1,
        "config": {"name": "demo"},
        "packages": [
            {"type": "deb", "name": "busybox", "version": "1:1.37.0-6", "architecture": "amd64"}
        ],
    }

    def baked(*args: Any, **kwargs: Any) -> Any:
        result, artifacts = bake(*args, **kwargs)
        (artifacts[0].path.parent / "payload.manifest").write_text(json.dumps(payload))
        return result, artifacts

    monkeypatch.setattr("tundravm.builds.bake_image", baked)
    assert cli("build").code == EXIT_OK
    build = Build.open((project / "build/demo/latest-production").resolve())
    assert build.manifest["packages"]["count"] == 1
    assert json.loads((build.path / "packages.json").read_text()) == payload
    assert build.manifest["sbom"]["bound"] is True
    assert "1:1.37.0-6" in (build.path / "sbom.spdx.json").read_text()


CLOUD_ONLY = STARTER.replace("    dev=DevAccess", '    targets=("gcp",),\n    dev=DevAccess')
EVERY_CLOUD = STARTER.replace(
    "    dev=DevAccess", '    targets=("qemu", "azure", "gcp"),\n    dev=DevAccess'
)


def test_qemu_is_built_even_when_the_image_does_not_declare_it(project: Path, cli: Cli) -> None:
    (project / "demo.py").write_text(CLOUD_ONLY, encoding="utf-8")
    result = cli("build")
    assert result.code == EXIT_OK, result.err
    build = Build.open((project / "build/demo/latest-production").resolve())
    assert build.targets == ("qemu", "gcp")
    project_lock = (project / "build" / "tundravm.lock").read_bytes()
    frozen = (build.path / "inputs" / "tundravm.lock").read_bytes()
    assert frozen != project_lock  # the outputs differ, so the digests do
    assert build.manifest["lock"]["project_sha256"] == sha256_file(
        project / "build" / "tundravm.lock"
    )
    assert cli("lock", "--check").code == EXIT_OK  # the project lock is the declared image's
    again = cli("build")
    assert again.code == EXIT_OK and "already built" in again.out


def test_target_narrowing_keeps_the_project_lock(project: Path, cli: Cli) -> None:
    (project / "demo.py").write_text(EVERY_CLOUD, encoding="utf-8")
    assert cli("lock").code == EXIT_OK
    before = (project / "build" / "tundravm.lock").read_bytes()
    result = cli("build", "--target", "gcp")
    assert result.code == EXIT_OK, result.err
    build = Build.open((project / "build/demo/latest-production").resolve())
    assert build.targets == ("qemu", "gcp")
    assert not (build.path / "azure.vhd").exists()
    assert (project / "build" / "tundravm.lock").read_bytes() == before
    full = cli("build")
    assert full.code == EXIT_OK, full.err
    every = Build.open((project / "build/demo/latest-production").resolve())
    assert every.targets == ("qemu", "azure", "gcp")
    assert (every.path / "inputs" / "tundravm.lock").read_bytes() == before
