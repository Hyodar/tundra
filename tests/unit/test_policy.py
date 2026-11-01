from pathlib import Path

import pytest

from tundravm.declarative import (
    Backend,
    Build,
    Git,
    Install,
    Package,
    Policy,
    Recipe,
    bake,
    lock,
)
from tundravm.declarative.lifecycle import bake_image
from tundravm.declarative.lower import lower
from tundravm.errors import LockfileError, PolicyError


def test_policy_requires_frozen_lock_for_bake(tmp_path: Path) -> None:
    recipe = Recipe(
        "frozen",
        items=(Package("curl"), Package("linux-image-amd64")),
        policy=Policy(require_frozen_lock=True),
    )
    with pytest.raises(PolicyError):
        bake_image(lower(recipe), None, locked=None, backend=None, build=tmp_path / "x")
    artifacts = bake(recipe, lock=lock(recipe), backend=Backend("local"), build=tmp_path / "api")
    assert [(a.profile, a.target) for a in artifacts] == [("default", "qemu")]


def test_policy_network_offline_locks_without_the_network() -> None:
    build = Build(
        "tool",
        Git("https://example.invalid/tool.git", "main"),
        script="make",
        install=(Install("tool", "/usr/bin/tool"),),
    )
    recipe = Recipe(
        "offline",
        items=(build,),
        policy=Policy(network_mode="offline"),
    )

    with pytest.raises(LockfileError, match="Cannot lock offline"):
        lock(recipe)
