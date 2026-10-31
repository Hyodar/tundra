"""Tests for tundravm.recipe.load_recipe / load_image."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tundravm.declarative import Package, Recipe
from tundravm.errors import ValidationError
from tundravm.recipe import load_image, load_recipe

HEADER = "from tundravm.declarative import Debian, Fragment, Package, Recipe\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_module_level_recipe(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "recipe = Recipe('r', items=(Package('curl'),))\n",
    )
    loaded = load_recipe(recipe)
    assert isinstance(loaded, Recipe)
    assert Package("curl") in loaded.items
    assert "curl" in load_image(recipe).state.profiles["default"].packages


def test_attr_selects_an_image_or_a_factory_returning_one(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "image.py",
        "from tundravm import Debian, Image\n"
        "image = Image('r', packages=['busybox'])\n"
        "other = Image('r', base=Debian('sid'), packages=['busybox'])\n"
        "def third():\n    return Image('r', base=Debian('bookworm'), packages=['busybox'])\n",
    )
    assert load_image(recipe).state.base == "debian/trixie"
    assert load_image(recipe, attribute="other").state.base == "debian/sid"
    assert load_image(recipe, attribute="third").state.base == "debian/bookworm"
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe, attribute="nope")
    assert "image, other, third" in str(excinfo.value)


def test_factory_returning_none_is_explained(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def build():\n    Recipe('r')\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "returned None" in str(excinfo.value)

    _write(tmp_path, "recipe.py", HEADER + "def build():\n    return Fragment('c')\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "returned Fragment, not an Image" in str(excinfo.value)


def test_factory_with_required_args_is_rejected(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def build(size):\n    return Recipe('r')\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "requires arguments: size" in str(excinfo.value)


def test_main_guard_does_not_run(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "recipe = Recipe('r')\n"
        "if __name__ == '__main__':\n    raise SystemExit('should not run')\n",
    )
    assert isinstance(load_recipe(recipe), Recipe)


def test_sibling_imports_resolve(tmp_path: Path) -> None:
    _write(tmp_path, "helper_pkgs.py", "PACKAGES = ('curl', 'jq')\n")
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from helper_pkgs import PACKAGES\n"
        + HEADER
        + "recipe = Recipe('r', items=tuple(Package(name) for name in PACKAGES))\n",
    )
    assert load_recipe(recipe).items == (Package("curl"), Package("jq"))
    assert {"curl", "jq"} <= load_image(recipe).state.profiles["default"].packages


def test_missing_file_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(tmp_path / "missing.py")
    assert "not found" in str(excinfo.value)


# ── import failures become E_VALIDATION ──────────────────────────────────


@pytest.mark.parametrize(
    ("body", "line", "error"),
    [
        (HEADER + "recipe = Recipe('r',\n", 2, "SyntaxError: '(' was never closed"),
        ("import tundravm\nimport not_a_module_xyz\n", 2, "ModuleNotFoundError: No module"),
        (HEADER + "\nVALUE = 1 / 0\n", 3, "ZeroDivisionError: division by zero"),
        (HEADER + "def build() -> Recipe:\n    raise RuntimeError('no')\n", 3, "RuntimeError: no"),
    ],
    ids=["syntax", "import", "runtime", "factory"],
)
def test_recipe_failures_are_validation_errors_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, line: int, error: str
) -> None:
    monkeypatch.chdir(tmp_path)
    recipe = _write(tmp_path, "recipe.py", body)
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    exc = excinfo.value
    assert exc.code == "E_VALIDATION"
    assert exc.context["location"] == f"recipe.py:{line}"
    assert exc.context["error"].startswith(error)
    assert exc.hint is not None and "python recipe.py" in exc.hint
    assert exc.__cause__ is not None
    if "def build" not in body:  # a failed import is not left registered
        assert not any(getattr(m, "__file__", None) == str(recipe) for m in sys.modules.values())


def test_errors_in_a_helper_module_point_at_the_helper(tmp_path: Path) -> None:
    _write(tmp_path, "helpers.py", "def items():\n    return [][0]\n")
    recipe = _write(tmp_path, "recipe.py", HEADER + "import helpers\nITEMS = helpers.items()\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert excinfo.value.context["location"].endswith("helpers.py:2")
    assert excinfo.value.context["error"].startswith("IndexError")


def test_sdk_errors_raised_by_the_recipe_pass_through(tmp_path: Path) -> None:
    recipe = _write(tmp_path, "recipe.py", HEADER + "recipe = Recipe('r', platform=Debian(1))\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "location" not in excinfo.value.context


def test_recipe_edits_are_never_hidden_by_stale_bytecode(tmp_path: Path) -> None:
    body = HEADER + "recipe = Recipe('r', items=(Package('{}'),))\n"
    recipe = _write(tmp_path, "recipe.py", body.format("aaaa"))
    stamp = recipe.stat().st_mtime_ns
    assert Package("aaaa") in load_recipe(recipe).items

    recipe.write_text(body.format("bbbb"), encoding="utf-8")  # same size
    os.utime(recipe, ns=(stamp, stamp))  # and the same mtime

    assert Package("bbbb") in load_recipe(recipe).items
    assert not (tmp_path / "__pycache__").exists()
