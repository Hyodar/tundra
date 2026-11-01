"""An incremental build's cache key: what mkosi's own cache manifest leaves out, and nothing
a change after the cache point touches."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tundravm.declarative import (
    Debian,
    Declaration,
    File,
    Package,
    Recipe,
    Repository,
    Setting,
    Variant,
    lower,
)
from tundravm.declarative._bake import incremental_key
from tundravm.toolchain import Toolchain, ToolsTree

SNAPSHOT = "20261018T082309Z"
BASE = Recipe(
    "node",
    platform=Debian("trixie", snapshot=SNAPSHOT),
    items=(Package("curl"),),
    variants=(Variant("default"), Variant("dev", release=False)),
)
TOOLS = Toolchain("0", tools_tree=ToolsTree("debian", "trixie", SNAPSHOT))


def _with(*items: Declaration) -> Recipe:
    return replace(BASE, items=(*BASE.items, *items))


@pytest.mark.parametrize(
    ("recipe", "profile", "toolchain", "same"),
    [
        (BASE, "default", TOOLS, True),
        (_with(File("/etc/motd", "node\n")), "default", TOOLS, True),
        (_with(Package("jq")), "default", TOOLS, True),
        (
            _with(File("/etc/apt/apt.conf.d/90proxy", "x\n", stage="skeleton")),
            "default",
            TOOLS,
            False,
        ),
        (_with(Setting("Content", "WithRecommends", ("yes",))), "default", TOOLS, False),
        (
            replace(BASE, platform=Debian("trixie", snapshot="20261101T000000Z")),
            "default",
            TOOLS,
            False,
        ),
        (
            _with(Repository("extra", "https://deb.example/debian", "trixie", keyring="k.gpg")),
            "default",
            TOOLS,
            False,
        ),
        (BASE, "dev", TOOLS, False),
        (BASE, "default", Toolchain("0", tools_tree=ToolsTree("debian", "trixie", None)), False),
    ],
    ids=[
        "unchanged",
        "a-file",
        "a-package",
        "a-skeleton-file",
        "a-verbatim-setting",
        "the-snapshot",
        "a-repository",
        "another-profile",
        "another-tools-tree",
    ],
)
def test_the_key_covers_what_mkosis_cache_manifest_leaves_out(
    recipe: Recipe, profile: str, toolchain: Toolchain, same: bool
) -> None:
    """A file, a unit or a package leaves the key alone (mkosi's manifest sees the package
    set, and the rest is applied after the cache point); the skeleton, a verbatim setting,
    the snapshot, a repository, the profile and the tools tree each make a new entry."""
    base = incremental_key(lower(BASE), "default", TOOLS)
    found = incremental_key(lower(recipe), profile, toolchain)
    assert base.startswith("default-") and len(base) == len("default-") + 16
    assert (found == base) is same
