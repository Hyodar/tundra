"""Loading declarative recipe files, and the CLI running on them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm import Recipe, load
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
from tundravm.errors import ValidationError
from tundravm.recipe import load_image

RECIPE = """
from tundravm import File, Fragment, Package, Recipe, Unit, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()

recipe = Recipe(
    name="loaded",
    base="debian/bookworm",
    common=Fragment(
        "loaded",
        items=(
            Package("curl"),
            File("/etc/motd", "hello\\n"),
            Unit("dropbear.service", enabled=True),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
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


def test_load_discovers_a_build_factory_and_rejects_other_values(tmp_path: Path) -> None:
    factory = tmp_path / "factory.py"
    factory.write_text(
        "from tundravm import Fragment, Recipe\n"
        "def build() -> Recipe:\n"
        "    return Recipe('f', Fragment('c'))\n",
        encoding="utf-8",
    )
    assert load(factory, attribute=None).name == "f"
    assert load(factory, attribute="build").name == "f"
    with pytest.raises(ValidationError, match="no attribute 'recipe'"):
        load(factory)
    nothing = tmp_path / "nothing.py"
    nothing.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="does not define a Recipe"):
        load(nothing, attribute=None)


def test_load_image_lowers_and_wires_the_backend(recipe_file: Path) -> None:
    img = load_image(recipe_file)
    assert img.profile_names == ("azure", "default")
    assert img.backend is not None and img.backend.name == "inprocess"
    assert img.state.effective_profile("azure").output_targets == ("azure",)


def test_cli_inspect_and_compile_a_declarative_recipe(recipe_file: Path, tmp_path: Path) -> None:
    code, out = run_main("inspect", str(recipe_file), "--json")
    assert code == EXIT_OK
    payload = json.loads(out)["variants"]
    assert set(payload) == {"default", "azure"}
    assert payload["default"]["base"] == "debian/bookworm"

    code, out = run_main("inspect", str(recipe_file), "--variant", "azure")
    assert code == EXIT_OK and "azure" in out

    code, _ = run_main("compile", str(recipe_file), "--out", str(tmp_path / "tree"))
    assert code == EXIT_OK
    motd = tmp_path / "tree" / "default" / "mkosi.extra" / "etc" / "motd"
    assert motd.read_text() == "hello\n"


def test_cli_reports_resolution_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text(
        "from tundravm import Disk, Fragment, Key, Recipe\n"
        "recipe = Recipe('b', Fragment('c', items=(Disk('d', '/data', key=Key('k')),)))\n",
        encoding="utf-8",
    )
    code, _ = run_main("inspect", str(broken))
    assert code == EXIT_SDK_ERROR
    assert "disk-key-undefined" in capsys.readouterr().err

    code, out = run_main("lint", str(broken))
    assert code == EXIT_FAILURE
    assert "error disk-key-undefined [default]" in out
