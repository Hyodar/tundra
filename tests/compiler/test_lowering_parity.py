"""Lowering writes ``RecipeState`` directly, byte for byte what the fluent ``Image`` calls built.

Every example recipe, the surge recipe (each variant) and every ``init`` template
compile to the tree digests, and lock the recipe digests, recorded in
``fixtures/lowering_parity.json`` from the lowering that drove ``Image``'s
declaration methods. The surge recipe also matches its committed golden tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import EXAMPLE_RECIPES, REPO_ROOT, SURGE_EXAMPLE
from tundravm.declarative import compile, load, lower
from tundravm.lockfile import recipe_digest
from tundravm.templates import TEMPLATES, render_recipe_template
from tundravm.testing import assert_tree

FIXTURE = Path(__file__).parent / "fixtures" / "lowering_parity.json"
EXPECTED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))


def recipe_paths(scratch: Path) -> dict[str, Path]:
    """Every recipe the parity covers, by name; ``init`` templates are rendered into *scratch*."""
    paths = {path.stem: path for path in EXAMPLE_RECIPES}
    paths["surge"] = SURGE_EXAMPLE / "image.py"
    for template in TEMPLATES:
        path = scratch / template / f"{template}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_recipe_template(
                title=template,
                filename=path.name,
                base="debian/trixie",
                backend="inprocess",
                template=template,
            ),
            encoding="utf-8",
        )
        paths[f"init-{template}"] = path
    return paths


def measure(path: Path) -> dict[str, Any]:
    """The tree and recipe digests of the recipe at *path*, whole and per variant."""
    recipe = load(path, extra_paths=[REPO_ROOT])
    names = tuple(variant.name for variant in recipe.variants)

    def recipe_of(selected: tuple[str, ...]) -> str:
        payload = lower(recipe, variants=selected)._recipe_payload(profile_names=selected)
        return recipe_digest(payload)

    return {
        "tree": compile(recipe).digest,
        "recipe": recipe_of(names),
        "variants": {
            name: {"tree": compile(recipe, variants=[name]).digest, "recipe": recipe_of((name,))}
            for name in names
        },
    }


@pytest.fixture(scope="module")
def paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return recipe_paths(tmp_path_factory.mktemp("templates"))


def test_the_fixture_covers_every_recipe(paths: dict[str, Path]) -> None:
    assert sorted(EXPECTED) == sorted(paths)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_recipe_lowers_to_the_recorded_digests(name: str, paths: dict[str, Path]) -> None:
    assert measure(paths[name]) == EXPECTED[name]


def test_surge_recipe_matches_the_committed_tree() -> None:
    surge = load(SURGE_EXAMPLE / "image.py", extra_paths=[REPO_ROOT])
    assert_tree(compile(surge), SURGE_EXAMPLE / "mkosi", update=False)
