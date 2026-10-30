"""The surge image compiles byte for byte to its committed tree, whole and per profile.

The tree is the mkosi-native one: ``dev`` is the only profile overlay, ``azure`` and
``gcp`` the only wrappers, and the root is the verity layout (root and hash partitions
beside the ESP, in the QEMU disk and both wrappers). The dev profile compiled alone writes
the shared payload, the QEMU disk and its own overlay (the rest of the committed tree is
ignored); production alone differs in one known line.
"""

from __future__ import annotations

import pytest

from tests.helpers import REPO_ROOT, SURGE_EXAMPLE
from tundravm.declarative import Recipe, compile, load
from tundravm.testing import assert_tree, assert_tree_matches

GOLDEN = SURGE_EXAMPLE / "mkosi"
SURGE_PROFILES = ("production", "dev")


@pytest.fixture(scope="module")
def surge() -> Recipe:
    return load(SURGE_EXAMPLE / "image.py", extra_paths=[REPO_ROOT])


def test_surge_image_declares_the_golden_profiles(surge: Recipe) -> None:
    assert tuple(v.name for v in surge.variants) == SURGE_PROFILES
    assert sorted(p.name for p in GOLDEN.iterdir()) == [
        "mkosi.conf",
        "mkosi.images",
        "mkosi.repart",
        "payload",
    ]
    assert sorted(p.name for p in (GOLDEN / "mkosi.images").iterdir()) == [
        "azure",
        "gcp",
        "payload",
    ]
    assert sorted(p.name for p in (GOLDEN / "payload" / "mkosi.profiles").iterdir()) == ["dev"]
    for wrapper in ("", "mkosi.images/azure/", "mkosi.images/gcp/"):
        repart = sorted(p.name for p in (GOLDEN / f"{wrapper}mkosi.repart").iterdir())
        assert repart == ["00-esp.conf", "10-root.conf", "11-root-verity.conf"], wrapper


def test_surge_image_matches_the_committed_tree(surge: Recipe) -> None:
    assert_tree(compile(surge), GOLDEN, update=False)


def test_the_dev_profile_compiles_alone_to_its_golden_tree(surge: Recipe) -> None:
    diff = assert_tree_matches(surge, GOLDEN, variants=["dev"], update=False)
    assert diff.is_clean


def test_production_alone_differs_only_in_the_shared_debloat_whitelist(surge: Recipe) -> None:
    """The shared payload's debloat keeps every unit any compiled profile enables, so
    compiling production alone masks the dev profile's serial getty, which the whole tree
    leaves unmasked (still disabled) in production too."""
    alone = {e.path: e.content for e in compile(surge, variants=["production"]).entries}
    whole = {e.path: e.content for e in compile(surge).entries}
    changed = sorted(path for path in alone if alone[path] != whole.get(path))
    assert changed == ["payload/mkosi.postinst.d/50-postinst.sh"]
    removed = set((whole[changed[0]] or b"").decode().split()) - set(
        (alone[changed[0]] or b"").decode().split()
    )
    assert removed == {'"serial-getty@.service"'}
