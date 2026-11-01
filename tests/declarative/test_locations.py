"""Declarations know where they were written: diagnostics and origins point at the line."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import write_recipe_file
from tundravm import _location
from tundravm.check import render, render_github
from tundravm.declarative import Diagnostic, Recipe, lint, lower
from tundravm.declarative.resolve import provenance, resolve
from tundravm.errors import ValidationError
from tundravm.recipe import load_recipe

RECIPE = """
from tundravm.declarative import BootStep, Debian, Package, Recipe, Service, Unit, Variant

app = Service("app", "/usr/bin/app", user="ghost")
recipe = Recipe(
    "located", platform=Debian("bookworm"),
    items=(
        Package("curl"),
        app,
    ),
    variants=(
        Variant("prod", outputs=("qemu",)),
        Variant("dev", inherits="prod", changes=(Unit.ref("gone.service").remove(),)),
    ),
)
"""
VALID = RECIPE.replace('Unit.ref("gone.service")', 'Package.ref("curl")')
"""The same recipe, every variant resolving (lowering resolves every variant)."""


def _finding(recipe: Recipe, code: str) -> Diagnostic:
    return next(d for d in lint(recipe, variants=["prod"]) if d.code == code)


def _write(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture
def recipe_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


def test_resolution_and_compiler_diagnostics_carry_the_recipe_line(recipe_path: Path) -> None:
    recipe = load_recipe(recipe_path)
    (missing,) = lint(recipe, variants=["dev"])
    assert (missing.code, missing.location) == ("remove-missing", "recipe.py:13")
    with pytest.raises(ValidationError, match=r"which it does not inherit at recipe\.py:13"):
        resolve(recipe, variant="dev")
    # the dev variant shapes the tree prod builds too: its error stops prod's lint
    assert [d.code for d in lint(recipe, variants=["prod"])] == ["remove-missing"]
    (recipe_path.parent / "valid").mkdir()
    valid = load_recipe(_write(recipe_path.parent / "valid" / "recipe.py", VALID))
    ghost = _finding(valid, "service-user-missing")
    assert (ghost.code, ghost.location) == ("service-user-missing", "valid/recipe.py:4")
    assert "    at: valid/recipe.py:4\n" in render([ghost])


def test_github_annotations_name_the_file_and_line(recipe_path: Path) -> None:
    path = _write(recipe_path.parent / "recipe.py", VALID)
    ghost = _finding(load_recipe(path), "service-user-missing")
    first = render_github([ghost], recipe_path).splitlines()[0]
    assert first.startswith("::error file=recipe.py,line=4,title=service-user-missing::")


def test_moving_a_declaration_keeps_equality_and_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = load_recipe(write_recipe_file(tmp_path, VALID, monkeypatch))
    moved_source = VALID.replace("app = Service(", "\n\n\napp = Service(")
    moved = load_recipe(_write(tmp_path / "moved.py", moved_source))
    assert here.items[1].declared_at.endswith(":4")
    assert moved.items[1].declared_at.endswith(":7")
    assert here == moved and hash(here.items[1]) == hash(moved.items[1])
    assert repr(here.items[1]) == repr(moved.items[1])
    assert lower(here, variants=["prod"]).digest() == lower(moved, variants=["prod"]).digest()


def test_edits_keep_the_declaration_line_and_record_their_own(recipe_path: Path) -> None:
    source = RECIPE.replace(
        'changes=(Unit.ref("gone.service").remove(),)', "changes=(app.ref.edit(user=None),)"
    )
    edited_path = _write(recipe_path.parent / "edited.py", source)
    recipe = load_recipe(edited_path)
    (item,) = (i for i in resolve(recipe, variant="dev").items if i.ref.kind == "Service")
    assert item.declared_at == f"{edited_path}:4"
    declared, edited = provenance(recipe, variant="dev")[("Service", "app.service")]
    assert declared.describe() == "declared at edited.py:4 in the image"
    assert edited.describe() == "user edited at edited.py:13 in profile dev"


def test_a_step_cycle_points_at_a_step_in_it(tmp_path: Path) -> None:
    source = RECIPE.replace(
        "        app,\n",
        '        BootStep("a", "true", after=["b"]),\n'
        '        BootStep("b", "true", after=["a"]),\n',
    )
    cycle = _finding(load_recipe(_write(tmp_path / "cycle.py", source)), "step-cycle")
    assert cycle.location == f"{tmp_path}/cycle.py:9"  # outside the working directory


def test_a_skipped_frame_reached_through_dotdot_stays_skipped() -> None:
    # A console script started as `../.venv/bin/tundravm` runs under an unnormalized path.
    package = Path(_location.__file__).parent
    code = compile("found = caller_location()", f"{package}/../tundravm/cli.py", "exec")
    namespace: dict[str, object] = {"caller_location": _location.caller_location}
    exec(code, namespace)
    assert str(namespace["found"]).startswith(f"{__file__}:")
