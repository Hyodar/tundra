"""Tests for tundravm.recipe.load_recipe."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm import Image, ValidationError, load_recipe


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_module_level_img(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\nimg = Image()\nimg.install('curl')\n",
    )
    img = load_recipe(recipe)
    assert isinstance(img, Image)
    assert "curl" in img.state.profiles["default"].packages


def test_loads_build_factory(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\n"
        "def build() -> Image:\n"
        "    img = Image(base='debian/trixie')\n"
        "    return img\n",
    )
    assert load_recipe(recipe).base == "debian/trixie"


def test_discovers_single_build_prefixed_factory(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\n"
        "def helper():\n    return 1\n"
        "def build_surge_prover():\n    return Image(base='debian/sid')\n",
    )
    assert load_recipe(recipe).base == "debian/sid"


def test_discovers_factory_by_return_annotation(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\n"
        "def make_the_thing() -> Image:\n    return Image(base='debian/sid')\n",
    )
    assert load_recipe(recipe).base == "debian/sid"


def test_discovers_single_anonymous_instance(tmp_path: Path) -> None:
    recipe = _write(tmp_path, "recipe.py", "from tundravm import Image\nmy_vm = Image()\n")
    assert isinstance(load_recipe(recipe), Image)


def test_attr_override_selects_instance_or_factory(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\n"
        "img = Image(base='debian/bookworm')\n"
        "other = Image(base='debian/trixie')\n"
        "def third() -> Image:\n    return Image(base='debian/sid')\n",
    )
    assert load_recipe(recipe).base == "debian/bookworm"
    assert load_recipe(recipe, attr="other").base == "debian/trixie"
    assert load_recipe(recipe, attr="third").base == "debian/sid"


def test_attr_missing_lists_candidates(tmp_path: Path) -> None:
    recipe = _write(tmp_path, "recipe.py", "from tundravm import Image\nimg = Image()\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe, attr="nope")
    assert "img" in str(excinfo.value)


def test_ambiguous_instances_error(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\na = Image()\nb = Image()\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "a, b" in str(excinfo.value)
    assert "--attr" in str(excinfo.value)


def test_no_image_error(tmp_path: Path) -> None:
    recipe = _write(tmp_path, "recipe.py", "x = 1\n")
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "does not define a Recipe" in str(excinfo.value)


def test_factory_returning_none_is_explained(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\ndef build():\n    Image().install('curl')\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "returned None" in str(excinfo.value)


def test_factory_with_required_args_is_rejected(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\ndef build(size):\n    return Image()\n",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(recipe)
    assert "requires arguments: size" in str(excinfo.value)


def test_main_guard_does_not_run(tmp_path: Path) -> None:
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from tundravm import Image\nimg = Image()\n"
        "if __name__ == '__main__':\n    raise SystemExit('should not run')\n",
    )
    assert isinstance(load_recipe(recipe), Image)


def test_sibling_imports_resolve(tmp_path: Path) -> None:
    _write(tmp_path, "helper_pkgs.py", "PACKAGES = ('curl', 'jq')\n")
    recipe = _write(
        tmp_path,
        "recipe.py",
        "from helper_pkgs import PACKAGES\nfrom tundravm import Image\n"
        "img = Image()\nimg.install(*PACKAGES)\n",
    )
    img = load_recipe(recipe)
    assert {"curl", "jq"} <= img.state.profiles["default"].packages


def test_missing_file_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_recipe(tmp_path / "missing.py")
    assert "not found" in str(excinfo.value)


def test_surge_example_recipe_loads() -> None:
    recipe = Path(__file__).resolve().parent.parent / "examples" / "surge-tdx-prover" / "image.py"
    img = load_recipe(recipe)
    assert isinstance(img, Image)
    assert {"azure", "gcp", "devtools"} <= set(img.state.profiles)


@pytest.mark.parametrize(
    "name",
    [
        "qemu_basic.py",
        "multi_profile_cloud.py",
        "full_api.py",
        "tdxs_module.py",
        "strict_secrets.py",
    ],
)
def test_small_examples_are_loadable_recipes(name: str) -> None:
    recipe = Path(__file__).resolve().parent.parent / "examples" / name
    img = load_recipe(recipe)
    assert isinstance(img, Image)
    assert img.state.profiles["default"] is not None
