import io
from pathlib import Path

import pytest

from tundravm.cli import EXIT_OK, main
from tundravm.declarative import (
    Backend,
    Build,
    Git,
    Install,
    Policy,
    Recipe,
    bake,
    lock,
)
from tundravm.declarative.lifecycle import bake_image
from tundravm.declarative.lower import lower
from tundravm.errors import LockfileError, PolicyError
from tundravm.recipe import load_recipe

FROZEN_RECIPE = """
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import Fragment, Package, Policy, Recipe

recipe = Recipe(
    "frozen",
    items=(Package("curl"), Package("linux-image-amd64")),
    policy=Policy(require_frozen_lock=True),
)
backend = LocalLinuxBackend()
"""


def test_policy_requires_frozen_lock_for_bake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # the CLI's lockfile is ./build/tundravm.lock
    path = tmp_path / "recipe.py"
    path.write_text(FROZEN_RECIPE, encoding="utf-8")

    # The API refuses an unfrozen bake; the CLI never bakes one: a build needs the lock.
    with pytest.raises(PolicyError):
        bake_image(lower(load_recipe(path)), None, locked=None, backend=None, build=tmp_path / "x")
    assert main(["lock", str(path)], stdout=io.StringIO()) == EXIT_OK
    assert main(["build", str(path)], stdout=io.StringIO()) == EXIT_OK

    recipe = load_recipe(path)
    assert isinstance(recipe.policy, Policy) and recipe.policy.require_frozen_lock
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
