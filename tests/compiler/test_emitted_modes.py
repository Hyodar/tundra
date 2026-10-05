"""Emitted trees do not depend on the umask: every path the emitter writes gets its mode set.

Files are ``0644``, generated scripts ``0755``, declared files keep their declared
mode and directories are ``0755``, so a tree and its digest are the same on a
host with umask ``002`` as on CI with ``022``.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT, SURGE_EXAMPLE
from tundravm.declarative import (
    Debloat,
    File,
    Fragment,
    Git,
    Hook,
    Kernel,
    Mkosi,
    Package,
    Recipe,
    Repository,
    Service,
    Template,
    Variant,
    compile,
    load,
    lower,
)
from tundravm.declarative._compile import emit
from tundravm.declarative.lifecycle import read_tree
from tundravm.testing import assert_tree

UMASKS = (0o002, 0o022, 0o077)


@contextmanager
def umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def modes(root: Path) -> dict[str, int]:
    """Every path under *root* with its permission bits."""
    return {
        path.relative_to(root).as_posix(): stat.S_IMODE(path.lstat().st_mode)
        for path in sorted(root.rglob("*"))
    }


def emit_all(recipe: Recipe, out: Path) -> None:
    """Emit every variant of *recipe* to *out*."""
    names = tuple(variant.name for variant in recipe.variants)
    emit(lower(recipe, variants=names).select(names), out)


def rich_recipe(kernel_config: Path, *, layout: str = "per_directory") -> Recipe:
    """A recipe that makes the emitter write every kind of path it writes."""
    return Recipe(
        "modes",
        Fragment(
            "common",
            items=(
                Package("curl"),
                File("/etc/motd", "hello\n"),
                File("/etc/app/secret.key", "key\n", mode=0o600),
                File("/usr/bin/start", "#!/bin/sh\n", mode=0o755),
                Template("/etc/app/app.toml", "net={net}\n", variables=(("net", "main"),)),
                Service("app", "/usr/bin/app"),
                Hook("prep", "prepare", "echo prep"),
                Repository("extra", "https://repo.example/debian", "stable", priority=10),
                Debloat(enabled=True),
                Kernel("6.1.2", Git("https://example.com/linux", "v6.1.2"), config=kernel_config),
            ),
        ),
        variants=(
            Variant("default", target="qemu"),
            Variant("gcp", target="gcp", add=Fragment("gcp", items=(Package("jq"),))),
        ),
        base="debian/bookworm",
        mkosi=Mkosi(layout="native") if layout == "native" else Mkosi(),
    )


@pytest.fixture
def kernel_config(tmp_path: Path) -> Path:
    path = tmp_path / "kernel.config"
    path.write_text("CONFIG_X=y\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("layout", ["per_directory", "native"])
def test_emitted_tree_is_identical_under_every_umask(
    tmp_path: Path, kernel_config: Path, layout: str
) -> None:
    recipe = rich_recipe(kernel_config, layout=layout)
    seen = set()
    for mask in UMASKS:
        out = tmp_path / f"out-{mask:03o}"
        with umask(mask):
            emit_all(recipe, out)
            digest = compile(recipe).digest
        assert read_tree(out).digest == digest
        seen.add((digest, tuple(modes(out).items())))
    assert len(seen) == 1


def test_emitted_paths_get_explicit_modes(tmp_path: Path, kernel_config: Path) -> None:
    out = tmp_path / "out"
    with umask(0o077):
        emit_all(rich_recipe(kernel_config), out)
    found = modes(out)

    assert {mode for path, mode in found.items() if (out / path).is_dir()} == {0o755}
    expected = {
        "default/mkosi.conf": 0o644,
        "default/mkosi.extra/etc/motd": 0o644,
        "default/mkosi.extra/etc/app/secret.key": 0o600,
        "default/mkosi.extra/usr/bin/start": 0o755,
        "default/mkosi.extra/etc/app/app.toml": 0o644,
        "default/mkosi.extra/usr/lib/systemd/system/app.service": 0o644,
        "default/mkosi.skeleton/etc/systemd/system/minimal.target": 0o644,
        "default/mkosi.skeleton/etc/apt/preferences.d/extra.pref": 0o644,
        "default/mkosi.sandbox/etc/apt/sources.list.d/extra.sources": 0o644,
        "default/kernel/kernel.config": 0o644,
        "default/scripts/03-prepare.sh": 0o755,
        "default/scripts/04-build.sh": 0o755,
        "gcp/scripts/gcp-postoutput.sh": 0o755,
    }
    assert {path: found.get(path) for path in expected} == expected


@pytest.mark.parametrize("mask", UMASKS)
def test_surge_matches_its_golden_tree_under_any_umask(mask: int) -> None:
    surge = load(SURGE_EXAMPLE / "image.py", extra_paths=[REPO_ROOT])
    with umask(mask):
        tree = compile(surge)
    assert_tree(tree, SURGE_EXAMPLE / "mkosi", update=False)


def test_tree_digest_covers_the_exec_bit_alone(tmp_path: Path) -> None:
    def tree_with(file_mode: int, dir_mode: int) -> str:
        root = Path(tempfile.mkdtemp(dir=tmp_path))
        (root / "empty").mkdir()
        (root / "empty").chmod(dir_mode)
        (root / "file").write_text("x\n", encoding="utf-8")
        (root / "file").chmod(file_mode)
        return read_tree(root).digest

    assert tree_with(0o644, 0o755) == tree_with(0o664, 0o775) == tree_with(0o600, 0o700)
    assert tree_with(0o755, 0o755) == tree_with(0o775, 0o775) == tree_with(0o700, 0o700)
    assert tree_with(0o644, 0o755) != tree_with(0o755, 0o755)
