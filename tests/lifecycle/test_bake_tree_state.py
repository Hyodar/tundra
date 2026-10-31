"""Post-build robustness: mkosi's build state and unreadable paths never break bake or diff."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from tundravm._reporting import Event
from tundravm.backends.base import collect_artifacts, is_mkosi_state, mkosi_setting
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import Package, Recipe, lower
from tundravm.declarative.lifecycle import bake_image, read_tree
from tundravm.diff import diff_trees
from tundravm.models import BakeRequest, BakeResult, OutputTarget

STATE = ("mkosi.tools", "mkosi.cache", "mkosi.builddir", ".mkosi-private")


def _tree(root: Path) -> Path:
    project = root / "default"
    (project / "mkosi.extra" / "etc").mkdir(parents=True)
    (project / "mkosi.conf").write_text("[Output]\nFormat=uki\n")
    (project / "mkosi.extra" / "etc" / "motd").write_text("node\n")
    return project


def test_read_tree_leaves_out_mkosi_build_state(tmp_path: Path) -> None:
    project = _tree(tmp_path)
    clean = read_tree(tmp_path)
    for name in STATE:
        (project / name / "usr").mkdir(parents=True)
    (project / "mkosi.tools.manifest").write_text("{}")
    (tmp_path / "mkosi.cache").mkdir()

    assert read_tree(tmp_path) == clean
    assert [e.path for e in clean.entries] == [
        "default/mkosi.conf",
        "default/mkosi.extra/etc/motd",
    ]


def test_state_names_deeper_in_the_tree_are_content() -> None:
    assert is_mkosi_state("default/mkosi.tools")
    assert is_mkosi_state("mkosi.cache")
    assert not is_mkosi_state("default/mkosi.extra/mkosi.cache")


@pytest.mark.skipif(os.getuid() == 0, reason="root reads everything")
def test_read_tree_skips_unreadable_paths_with_a_warning(tmp_path: Path) -> None:
    project = _tree(tmp_path)
    locked = project / "mkosi.extra" / "secret"
    (locked / "inner").mkdir(parents=True)
    hidden = project / "mkosi.extra" / "etc" / "shadow"
    hidden.write_text("x")
    locked.chmod(0)
    hidden.chmod(0)
    warnings: list[str] = []
    try:
        tree = read_tree(tmp_path, warn=warnings.append)
    finally:
        locked.chmod(0o755)
        hidden.chmod(0o644)

    assert [e.path for e in tree.entries] == ["default/mkosi.conf", "default/mkosi.extra/etc/motd"]
    assert len(warnings) == 2
    assert all(w.startswith("skipped unreadable ") for w in warnings)


def test_diff_ignores_mkosi_build_state(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    _tree(old)
    _tree(new)
    (old / "default" / "mkosi.builddir").mkdir()
    (old / "default" / "mkosi.builddir" / "debian-backports.sources").write_text("deb x\n")
    (old / "default" / "mkosi.tools.manifest").write_text("{}")

    assert diff_trees(old, new).is_clean


ARTIFACTS = ("payload.efi", "qemu.raw", "azure.vhd", "gcp.tar.gz")


@pytest.mark.parametrize(
    ("files", "directories", "selection", "expected"),
    [
        ((), (), ("qemu",), {}),
        (
            ("qemu.raw", "payload.efi"),
            ("qemu.raw.d", "azure.vhd"),
            ("qemu", "azure"),
            {"qemu": "qemu.raw"},
        ),
        (ARTIFACTS, (), ("azure",), {"azure": "azure.vhd"}),
        (ARTIFACTS, (), ("azure", "gcp"), {"azure": "azure.vhd", "gcp": "gcp.tar.gz"}),
    ],
    ids=["missing-output", "directory-is-no-artifact", "azure-only", "azure-and-gcp"],
)
def test_collect_artifacts_takes_selected_files_only(
    tmp_path: Path,
    files: tuple[str, ...],
    directories: tuple[str, ...],
    selection: tuple[OutputTarget, ...],
    expected: dict[str, str],
) -> None:
    """Each selected target whose output is a file, in selection order, with the shared boot
    payload; a directory named like an artifact is skipped, and a missing output is ``{}``."""
    output = tmp_path / "output"
    for name in directories:
        (output / name).mkdir(parents=True)
    for name in files:
        output.mkdir(exist_ok=True)
        (output / name).write_bytes(b"x")
    found = collect_artifacts(output, selection)
    assert list(found) == list(expected)
    assert {t: r.path for t, r in found.items()} == {t: output / n for t, n in expected.items()}
    assert all(r.boot_payload == output / "payload.efi" for r in found.values())


def test_mkosi_setting_reads_every_conf_layer(tmp_path: Path) -> None:
    (tmp_path / "mkosi.conf").write_text("[Build]\nToolsTreeMirror=https://m\n")
    assert mkosi_setting(tmp_path, "ToolsTree") is None
    (tmp_path / "mkosi.conf.d").mkdir()
    (tmp_path / "mkosi.conf.d" / "10-tools.conf").write_text("[Build]\nToolsTree = default\n")
    assert mkosi_setting(tmp_path, "ToolsTree") == "default"


class _Collect:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


@dataclass(slots=True)
class _LeavesRootState(LocalLinuxBackend):
    """Leaves what a sudo mkosi run can: a tools tree and an unreadable directory."""

    def execute(self, request: BakeRequest) -> BakeResult:
        project = request.emit_dir / request.profile
        (project / "mkosi.tools" / "efi").mkdir(parents=True)
        (project / "mkosi.tools" / "efi").chmod(0)
        (project / "mkosi.extra" / "locked").mkdir(parents=True)
        (project / "mkosi.extra" / "locked").chmod(0)
        return LocalLinuxBackend.execute(self, request)


@pytest.mark.skipif(os.getuid() == 0, reason="root reads everything")
def test_bake_warns_about_unreadable_leftovers_instead_of_crashing(tmp_path: Path) -> None:
    recipe = Recipe("node", items=(Package("curl"), Package("linux-image-amd64")))
    reporter = _Collect()
    project = tmp_path / "build" / "mkosi" / "default"
    try:
        _, artifacts = bake_image(
            lower(recipe),
            None,
            locked=None,
            backend=_LeavesRootState(),
            build=tmp_path / "build",
            reporter=reporter,
        )
    finally:
        for path in (project / "mkosi.tools" / "efi", project / "mkosi.extra" / "locked"):
            if path.exists():
                path.chmod(0o755)

    assert [a.variant for a in artifacts] == ["default"]
    warnings = [e.message for e in reporter.events if e.kind == "warning"]
    assert len(warnings) == 1 and "mkosi.extra/locked" in warnings[0]
