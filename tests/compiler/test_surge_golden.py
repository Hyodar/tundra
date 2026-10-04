"""The surge recipe compiles byte for byte to its committed golden tree, whole and per variant."""

from __future__ import annotations

import pytest

from tests.helpers import REPO_ROOT, SURGE_EXAMPLE
from tundravm.declarative import Recipe, compile, load
from tundravm.testing import assert_tree, assert_tree_matches

GOLDEN = SURGE_EXAMPLE / "mkosi"
SURGE_VARIANTS = ("default", "azure", "gcp", "devtools")


@pytest.fixture(scope="module")
def surge() -> Recipe:
    return load(SURGE_EXAMPLE / "image.py", extra_paths=[REPO_ROOT])


def test_surge_recipe_declares_the_golden_variants(surge: Recipe) -> None:
    assert tuple(v.name for v in surge.variants) == SURGE_VARIANTS
    assert sorted(p.name for p in GOLDEN.iterdir()) == sorted(SURGE_VARIANTS)


def test_surge_recipe_matches_the_committed_tree(surge: Recipe) -> None:
    assert_tree(compile(surge), GOLDEN, update=False)


@pytest.mark.parametrize("variant", SURGE_VARIANTS)
def test_surge_variant_compiles_alone_to_its_golden_tree(surge: Recipe, variant: str) -> None:
    diff = assert_tree_matches(surge, GOLDEN, variants=[variant], update=False)
    assert diff.is_clean
