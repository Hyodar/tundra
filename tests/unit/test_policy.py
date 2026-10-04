import io
from pathlib import Path

import pytest

from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import (
    Backend,
    Build,
    Fragment,
    Git,
    Install,
    Policy,
    Recipe,
    bake,
    lock,
)
from tundravm.errors import LockfileError
from tundravm.recipe import load_recipe

FROZEN_RECIPE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import Fragment, Package, Policy, Recipe

recipe = Recipe(
    "frozen",
    Fragment("frozen", items=(Package("curl"),)),
    policy=Policy(require_frozen_lock=True),
)
backend = InProcessBackend()
"""


def test_policy_requires_frozen_lock_for_bake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # the CLI's default lockfile is ./build/tundravm.lock
    path = tmp_path / "recipe.py"
    path.write_text(FROZEN_RECIPE, encoding="utf-8")
    out = tmp_path / "out"
    lock_path = tmp_path / "frozen.lock"

    # Without a lockfile the CLI bakes unfrozen, which the policy refuses.
    assert main(["bake", str(path), "--out", str(out)], stdout=io.StringIO()) == EXIT_SDK_ERROR
    assert "error [E_POLICY]" in capsys.readouterr().err

    assert main(["lock", str(path), "--path", str(lock_path)], stdout=io.StringIO()) == EXIT_OK
    argv = ["bake", str(path), "--lockfile", str(lock_path), "--out", str(out)]
    assert main(argv, stdout=io.StringIO()) == EXIT_OK

    recipe = load_recipe(path)
    assert isinstance(recipe.policy, Policy) and recipe.policy.require_frozen_lock
    artifacts = bake(
        recipe, locked=lock(recipe), backend=Backend("inprocess"), out=tmp_path / "api"
    )
    assert [(a.variant, a.target) for a in artifacts] == [("default", "qemu")]


def test_policy_network_offline_locks_without_the_network() -> None:
    build = Build(
        "tool",
        Git("https://example.invalid/tool.git", "main"),
        script="make",
        install=(Install("tool", "/usr/bin/tool"),),
    )
    recipe = Recipe(
        "offline",
        Fragment("tool", items=(build,)),
        policy=Policy(network_mode="offline"),
    )

    with pytest.raises(LockfileError, match="Cannot lock offline"):
        lock(recipe)


def test_policy_doc_includes_ci_guidance() -> None:
    doc = Path("docs/policy.md").read_text(encoding="utf-8")
    assert "require_frozen_lock" in doc
    assert "mutable_ref_policy" in doc
