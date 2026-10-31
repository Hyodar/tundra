"""Loading declarative recipe files, and the CLI running on them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import Recipe, load
from tundravm.errors import ValidationError
from tundravm.recipe import load_image, load_recipe

RECIPE = """
from tundravm.declarative import Debian, File, Fragment, Package, Recipe, Unit, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()

recipe = Recipe(
    name="loaded",
    platform=Debian("bookworm", boot=None),
    items=(
        Package("curl"),
        File("/etc/motd", "hello\\n"),
        Unit("dropbear.service", enabled=True),
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("azure", inherits="default", outputs=("azure",)),
    ),
)
"""


@pytest.fixture
def recipe_file(tmp_path: Path) -> Path:
    path = tmp_path / "loaded.py"
    path.write_text(RECIPE, encoding="utf-8")
    return path


def test_load_returns_the_recipe(recipe_file: Path) -> None:
    recipe = load(recipe_file)
    assert isinstance(recipe, Recipe)
    assert [v.name for v in recipe.variants] == ["default", "azure"]
    assert load(recipe_file, attribute=None) == recipe


def test_load_is_load_recipe() -> None:
    assert load is load_recipe


def test_load_discovers_like_the_cli_and_rejects_other_values(tmp_path: Path) -> None:
    factory = tmp_path / "factory.py"
    factory.write_text(
        "from tundravm.declarative import Fragment, Recipe\n"
        "def build() -> Recipe:\n    return Recipe('f')\n",
        encoding="utf-8",
    )
    assert load(factory).name == "f"
    assert load(factory, attribute="build").name == "f"
    with pytest.raises(ValidationError, match="no attribute 'recipe'"):
        load(factory, attribute="recipe")
    upper = tmp_path / "upper.py"
    upper.write_text(
        "from tundravm.declarative import Fragment, Recipe\nRECIPE = Recipe('u')\n",
        encoding="utf-8",
    )
    assert load(upper).name == "u"
    nothing = tmp_path / "nothing.py"
    nothing.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="does not define an Image"):
        load(nothing, attribute=None)


def test_load_image_lowers_and_wires_the_backend(recipe_file: Path) -> None:
    img = load_image(recipe_file)
    assert img.profile_names == ("azure", "default")
    assert img.backend is not None and img.backend.name == "inprocess"
    assert img.targets["azure"] == ("azure",) and img.clouds("azure") == ("azure",)


def test_cli_shows_and_compiles_a_declarative_recipe(recipe_file: Path, tmp_path: Path) -> None:
    code, out = run_main("show", str(recipe_file), "--json")
    assert code == EXIT_OK
    payload = json.loads(out)["variants"]
    assert set(payload) == {"default", "azure"}
    assert payload["default"]["base"] == "debian/bookworm"

    code, out = run_main("show", str(recipe_file), "--profile", "azure")
    assert code == EXIT_OK and "azure" in out

    code, _ = run_main("build", str(recipe_file), "--tree-only", str(tmp_path / "tree"))
    assert code == EXIT_OK
    motd = tmp_path / "tree" / "payload" / "mkosi.extra" / "etc" / "motd"
    assert motd.read_text() == "hello\n"


def test_cli_reports_resolution_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text(
        "from tundravm.declarative import Disk, Fragment, Key, Recipe\n"
        "recipe = Recipe('b', items=(Disk('d', '/data', key=Key('k')),))\n",
        encoding="utf-8",
    )
    code, _ = run_main("show", str(broken))
    assert code == EXIT_SDK_ERROR
    assert "disk-key-undefined" in capsys.readouterr().err

    code, out = run_main("check", str(broken))
    assert code == EXIT_FAILURE
    assert "error disk-key-undefined [default]" in out
