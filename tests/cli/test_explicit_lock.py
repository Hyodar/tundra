"""One explicit lockfile through every stage: ``lint(lock=)``, ``lint/ci --lockfile``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
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
    read_lock,
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


def test_lint_lockfile_reports_drift_as_lint_lock_does(custom_lock: tuple[Path, Path]) -> None:
    recipe, custom = custom_lock
    path = Path(recipe)
    path.write_text(
        path.read_text().replace(
            'Package("linux-image-amd64"),', 'Package("linux-image-amd64"), Package("jq"),'
        ),
        encoding="utf-8",
    )
    expected = _codes(lint(load(path), lock=read_lock(custom)))
    assert any(code.startswith("lock-") for code in expected)
    code, out = run_main("lint", str(path), "--lockfile", str(custom), "--format", "json")
    found = [d["code"] for d in json.loads(out)["diagnostics"]]
    assert sorted(found) == sorted(expected)


def _lint_counts(*argv: str) -> dict[str, object]:
    code, out = run_main("status", *argv, "--format", "json")
    assert code == EXIT_OK, out
    lint_item: dict[str, object] = json.loads(out)["lint"]
    return lint_item


def test_status_lints_with_the_lockfile_it_selected(
    custom_lock: tuple[Path, Path], tmp_path: Path
) -> None:
    recipe, custom = custom_lock
    unpinned = _lint_counts(str(recipe))
    assert unpinned["warnings"] == 1  # source-unpinned: no build/tundravm.lock
    assert _lint_counts(str(recipe), "--lockfile", str(custom)) == {
        "verdict": "ok",
        "detail": "0 errors, 0 warnings, 0 infos",
        "errors": 0,
        "warnings": 0,
        "infos": 0,
    }
    default = tmp_path / "build" / "tundravm.lock"
    default.parent.mkdir()
    default.write_bytes(custom.read_bytes())
    # a selected lockfile that does not exist is no lock: build/tundravm.lock hides nothing
    assert _lint_counts(str(recipe), "--lockfile", str(tmp_path / "absent.lock")) == unpinned


@pytest.fixture
def configured(custom_lock: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    """A project whose table names ``release.lock`` (absent); build/tundravm.lock pins all."""
    recipe, custom = custom_lock
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "recipe.py"\nlockfile = "release.lock"\n', encoding="utf-8"
    )
    default = tmp_path / "build" / "tundravm.lock"
    default.parent.mkdir()
    default.write_bytes(custom.read_bytes())
    return recipe, custom


MISSING = "configured lockfile release.lock does not exist; run `tundravm lock` to create it"


def test_a_missing_configured_lockfile_is_reported_never_replaced(
    configured: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_main("lint", "--strict")
    assert code == EXIT_FAILURE and "source-unpinned" in out
    assert f"note: {MISSING}" in capsys.readouterr().err
    assert run_main("compile", "--out", str(tmp_path / "mkosi"))[0] == EXIT_OK
    assert f"note: {MISSING}" in capsys.readouterr().err
    status = json.loads(run_main("status", "--format", "json")[1])
    assert status["lock"]["path"] == "release.lock" and not status["lock"]["present"]
    assert status["lock"]["detail"] == (
        "configured lockfile release.lock does not exist; run tundravm lock"
    )
    assert status["lint"]["warnings"] == 1
    assert run_main("bake", "-q")[0] == EXIT_SDK_ERROR
    assert "Configured lockfile release.lock does not exist." in capsys.readouterr().err
    values = json.loads(run_main("config", "--format", "json")[1])["values"]
    assert values["lockfile"] == {"value": "release.lock", "origin": "pyproject", "exists": False}
    assert values["backend"] == {"value": "inprocess", "origin": "recipe"}


def test_a_configured_lockfile_applies_its_pins_and_drift_as_the_flag_does(
    configured: tuple[Path, Path], tmp_path: Path
) -> None:
    recipe, custom = configured
    custom.rename(tmp_path / "release.lock")
    assert run_main("lint", "--strict") == (EXIT_OK, "no findings\n")
    recipe.write_text(
        recipe.read_text().replace(
            'Package("linux-image-amd64"),', 'Package("linux-image-amd64"), Package("jq"),'
        ),
        encoding="utf-8",
    )
    configured_codes = json.loads(run_main("lint", "--format", "json")[1])["diagnostics"]
    flag = run_main("lint", "--lockfile", "release.lock", "--format", "json")[1]
    assert any(d["code"].startswith("lock-") for d in configured_codes)
    assert configured_codes == json.loads(flag)["diagnostics"]
