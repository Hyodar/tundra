"""One explicit lockfile through every stage: ``lint(lock=)``, ``lint/ci --lockfile``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.declarative import (
    Build,
    Diagnostic,
    Fragment,
    Git,
    Install,
    Package,
    Policy,
    Recipe,
    lint,
    load,
    lock,
    write_lock,
)

PIN = "a" * 40
RECIPE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import Build, Fragment, Git, Install, Package, Recipe

backend = InProcessBackend()
recipe = Recipe(
    "tool",
    Fragment(
        "tool",
        items=(
            Package("linux-image-amd64"),
            Build(
                "tool",
                Git("https://example.invalid/tool.git", "main"),
                script="make",
                install=(Install("tool", "/usr/bin/tool"),),
            ),
        ),
    ),
)
"""


def _recipe(policy: Policy | None = None) -> Recipe:
    build = Build(
        "tool",
        Git("https://example.invalid/tool.git", "main"),
        script="make",
        install=(Install("tool", "/usr/bin/tool"),),
    )
    return Recipe(
        "tool", Fragment("tool", items=(Package("linux-image-amd64"), build)), policy=policy
    )


def _codes(found: tuple[Diagnostic, ...]) -> list[str]:
    return [d.code for d in found]


@pytest.mark.parametrize("policy", [None, Policy(mutable_ref_policy="error")])
def test_lint_applies_the_lock_before_the_compiler_rules(
    policy: Policy | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no build/tundravm.lock to fall back on
    recipe = _recipe(policy)
    assert "source-unpinned" in _codes(lint(recipe))
    assert lint(recipe, lock=lock(recipe, resolver=lambda source: PIN)) == ()


@pytest.fixture
def custom_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The recipe file and a lockfile pinning its source, away from build/tundravm.lock."""
    path = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    target = tmp_path / "custom.lock"
    write_lock(lock(load(path), resolver=lambda source: PIN), target)
    return path, target


def test_lint_lockfile_applies_its_pins(custom_lock: tuple[Path, Path]) -> None:
    recipe, custom = custom_lock
    code, out = run_main("lint", str(recipe), "--strict")
    assert code == EXIT_FAILURE and "source-unpinned" in out
    code, out = run_main("lint", str(recipe), "--strict", "--lockfile", str(custom))
    assert code == EXIT_OK, out
    assert "source-unpinned" not in out


def test_ci_lockfile_pins_every_step(custom_lock: tuple[Path, Path], tmp_path: Path) -> None:
    recipe, custom = custom_lock
    tree = tmp_path / "mkosi"
    assert run_main("compile", str(recipe), "--out", str(tree), "--lockfile", str(custom))[0] == 0
    assert not (tmp_path / "build" / "tundravm.lock").exists()
    code, out = run_main("ci", str(recipe), "--out", str(tree), "--lockfile", str(custom))
    assert code == EXIT_OK, out
    assert out.splitlines()[-3:] == [
        "ok lint: no findings",
        f"ok compile: {tree} is up to date",
        f"ok lock: {custom} is up to date",
    ]
    code, out = run_main("ci", str(recipe), "--out", str(tree))
    assert code == EXIT_FAILURE
    assert "FAIL lint" in out and "skip compile" in out


def test_lock_lockfile_writes_there(custom_lock: tuple[Path, Path], tmp_path: Path) -> None:
    recipe, custom = custom_lock
    target = tmp_path / "pins.lock"
    target.write_bytes(custom.read_bytes())
    custom.unlink()
    assert run_main("lock", str(recipe), "--lockfile", str(target), "--offline") == (
        EXIT_OK,
        f"locked {target}\n",
    )
    code, out = run_main("lock", str(recipe), "--lockfile", str(target), "--check")
    assert (code, out) == (EXIT_OK, "lock is up to date\n")
    assert not (tmp_path / "build" / "tundravm.lock").exists()
    with pytest.raises(SystemExit) as excinfo:
        run_main("lock", str(recipe), "--path", str(target))
    assert excinfo.value.code == 2
