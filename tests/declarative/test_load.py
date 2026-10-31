"""Loading declarative recipe files, and the CLI running on them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm.cli import EXIT_OK

RECIPE = """
from tundravm.declarative import Debian, File, Fragment, Package, Recipe, Unit, Variant
from tundravm.backends.local_linux import LocalLinuxBackend

backend = LocalLinuxBackend()

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
