"""The flashbox example compiles byte for byte to its committed tree, whole and per profile.

Its components are the attested node's, through the ``components`` link beside its image
file; its tree adds what the attested node's lacks: the searcher's container subimage, a
built kernel and a firewall with modes.
"""

from __future__ import annotations

import pytest

from tests.helpers import FLASHBOX, REPO_ROOT
from tundravm.declarative import Recipe, compile
from tundravm.recipe import load_file
from tundravm.testing import assert_tree
from tundravm.testing._model import assert_tree_matches

GOLDEN = FLASHBOX / "mkosi"


@pytest.fixture(scope="module")
def flashbox() -> Recipe:
    return load_file(FLASHBOX / "image.py", extra_paths=[REPO_ROOT]).recipe


def test_flashbox_matches_the_committed_tree(flashbox: Recipe) -> None:
    assert_tree(compile(flashbox), GOLDEN, update=False)


def test_flashbox_dev_profile_compiles_alone_to_its_golden_tree(flashbox: Recipe) -> None:
    assert tuple(v.name for v in flashbox.variants) == ("production", "dev")
    assert sorted(p.name for p in (GOLDEN / "mkosi.images").iterdir()) == [
        "azure",
        "bob",
        "gcp",
        "payload",
    ]
    diff = assert_tree_matches(flashbox, GOLDEN, profiles=["dev"], update=False)
    assert diff.is_clean


@pytest.mark.parametrize("name", ["main.go", "dev.go"])
def test_the_searchers_program_is_the_runtimes_reference_program(name: str) -> None:
    written = GOLDEN / "payload" / "tundravm-cli" / "searcher" / name
    assert written.read_bytes() == (REPO_ROOT / "tools" / "logincli" / name).read_bytes()
