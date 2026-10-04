import hashlib
import io
import subprocess
from pathlib import Path

import pytest

from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import Backend, Policy, bake, lock
from tundravm.errors import PolicyError, ValidationError
from tundravm.fetch import fetch, fetch_git
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


def test_policy_mutable_ref_error_is_enforced(tmp_path: Path) -> None:
    repo, _, tree_hash = _create_repo(tmp_path / "repo")
    policy = Policy(mutable_ref_policy="error")

    with pytest.raises(PolicyError):
        fetch_git(
            str(repo),
            ref="main",
            tree_hash=tree_hash,
            cache_dir=tmp_path / "cache",
            policy=policy,
        )


def test_policy_network_offline_blocks_fetch_operations(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    policy = Policy(network_mode="offline")

    with pytest.raises(PolicyError):
        fetch(source.as_uri(), sha256=digest, cache_dir=tmp_path / "cache", policy=policy)

    repo, commit, tree_hash = _create_repo(tmp_path / "repo")
    with pytest.raises(PolicyError):
        fetch_git(
            str(repo),
            ref=commit,
            tree_hash=tree_hash,
            cache_dir=tmp_path / "git-cache",
            policy=policy,
        )


def test_policy_integrity_mode_controls_hash_requirement(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"payload")

    with pytest.raises(ValidationError):
        fetch(source.as_uri(), sha256="", cache_dir=tmp_path / "cache")

    relaxed = fetch(
        source.as_uri(),
        sha256="",
        cache_dir=tmp_path / "cache-relaxed",
        policy=Policy(require_integrity=False),
    )
    assert relaxed.exists()


def test_policy_doc_includes_ci_guidance() -> None:
    doc = Path("docs/policy.md").read_text(encoding="utf-8")
    assert "require_frozen_lock" in doc
    assert "mutable_ref_policy" in doc


def _create_repo(path: Path) -> tuple[Path, str, str]:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], cwd=path)
    _run_git(["checkout", "-b", "main"], cwd=path)
    _run_git(["config", "user.email", "tdx@example.com"], cwd=path)
    _run_git(["config", "user.name", "TDX Test"], cwd=path)

    (path / "README.md").write_text("hello repo\n", encoding="utf-8")
    _run_git(["add", "README.md"], cwd=path)
    _run_git(["commit", "-m", "initial"], cwd=path)

    commit = _run_git(["rev-parse", "HEAD"], cwd=path)
    tree_hash = _run_git(["rev-parse", "HEAD^{tree}"], cwd=path)
    return path, commit, tree_hash


def _run_git(argv: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()
