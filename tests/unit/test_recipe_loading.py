"""Tests for tundravm.recipe.load_recipe / load_image."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import EXAMPLE_RECIPES, REPO_ROOT, SURGE_EXAMPLE
from tundravm import Package, Recipe, ValidationError, load_recipe
from tundravm.recipe import load_image

HEADER = "from tundravm import Fragment, Package, Recipe\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_module_level_recipe(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "recipe = Recipe('r', Fragment('common', items=(Package('curl'),)))\n",
    )
    loaded = load_recipe(recipe)
    assert isinstance(loaded, Recipe)
    assert Package("curl") in loaded.common.items
    assert "curl" in load_image(recipe).state.profiles["default"].packages


def test_loads_build_factory(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def build() -> Recipe:\n"
        "    return Recipe('r', Fragment('common'), base='debian/trixie')\n",
    )
    assert load_recipe(recipe).base == "debian/trixie"


def test_discovers_single_build_prefixed_factory(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def helper():\n    return 1\n"
        "def build_surge_prover():\n    return Recipe('r', Fragment('c'), base='debian/sid')\n",
    )
    assert load_recipe(recipe).base == "debian/sid"


def test_discovers_factory_by_return_annotation(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def make_the_thing() -> Recipe:\n"
        "    return Recipe('r', Fragment('c'), base='debian/sid')\n",
    )
    assert load_recipe(recipe).base == "debian/sid"


def test_discovers_single_anonymous_instance(tmp_path: Path) -> None:
    recipe = _write(tmp_path, "recipe.py", HEADER + "my_vm = Recipe('vm', Fragment('c'))\n")
    assert load_recipe(recipe).name == "vm"


def test_attr_override_selects_instance_or_factory(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "recipe = Recipe('r', Fragment('c'), base='debian/bookworm')\n"
        "other = Recipe('r', Fragment('c'), base='debian/trixie')\n"
        "def third() -> Recipe:\n    return Recipe('r', Fragment('c'), base='debian/sid')\n",
    )
    assert load_recipe(recipe).base == "debian/bookworm"
    assert load_recipe(recipe, attr="other").base == "debian/trixie"
    assert load_recipe(recipe, attr="third").base == "debian/sid"
    assert load_image(recipe, attr="third").state.base == "debian/sid"


def test_attr_missing_lists_candidates(tmp_path: Path) -> None:
    recipe = _write(tmp_path, "recipe.py", HEADER + "recipe = Recipe('r', Fragment('c'))\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe, attr="nope")
    assert "recipe" in str(excinfo.value)


def test_ambiguous_instances_error(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "a = Recipe('a', Fragment('c'))\nb = Recipe('b', Fragment('c'))\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "a, b" in str(excinfo.value)
    assert "--attr" in str(excinfo.value)


def test_no_recipe_error(tmp_path: Path) -> None:
    for body in ("x = 1\n", "recipe = {'name': 'not a Recipe'}\n"):
        recipe = _write(tmp_path, "recipe.py", body)
        with pytest.raises(ValidationError) as excinfo:
            load_recipe(recipe)
        assert "does not define a Recipe" in str(excinfo.value)


def test_factory_returning_none_is_explained(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def build():\n    Recipe('r', Fragment('c'))\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "returned None" in str(excinfo.value)

    _write(tmp_path, "recipe.py", HEADER + "def build():\n    return Fragment('c')\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "returned Fragment, expected Recipe" in str(excinfo.value)


def test_factory_with_required_args_is_rejected(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "def build(size):\n    return Recipe('r', Fragment('c'))\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "requires arguments: size" in str(excinfo.value)


def test_main_guard_does_not_run(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        HEADER + "recipe = Recipe('r', Fragment('c'))\n"
        "if __name__ == '__main__':\n    raise SystemExit('should not run')\n",
    )
    assert isinstance(load_recipe(recipe), Recipe)


def test_sibling_imports_resolve(tmp_path: Path) -> None:
    _write(tmp_path, "helper_pkgs.py", "PACKAGES = ('curl', 'jq')\n")
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from helper_pkgs import PACKAGES\n" + HEADER + "recipe = Recipe('r', "
        "Fragment('common', items=tuple(Package(name) for name in PACKAGES)))\n",
    )
    assert load_recipe(recipe).common.items == (Package("curl"), Package("jq"))
    assert {"curl", "jq"} <= load_image(recipe).state.profiles["default"].packages


def test_missing_file_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(tmp_path / "missing.py")
    assert "not found" in str(excinfo.value)


def test_surge_example_recipe_loads() -> None:
    recipe = SURGE_EXAMPLE / "image.py"
    loaded = load_recipe(recipe, extra_paths=[REPO_ROOT])
    assert isinstance(loaded, Recipe)
    assert {v.name for v in loaded.variants} == {"default", "azure", "gcp", "devtools"}
    img = load_image(recipe, extra_paths=[REPO_ROOT])
    assert {"default", "azure", "gcp", "devtools"} == set(img.state.profiles)


@pytest.mark.parametrize("path", EXAMPLE_RECIPES, ids=lambda path: path.name)
def test_examples_are_loadable_recipes(path: Path) -> None:
    assert isinstance(load_recipe(path, extra_paths=[REPO_ROOT]), Recipe)
    img = load_image(path, extra_paths=[REPO_ROOT])
    assert img.state.profiles
