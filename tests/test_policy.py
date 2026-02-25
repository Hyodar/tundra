import hashlib
import subprocess
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends import InProcessBackend
from tundravm.errors import PolicyError, ValidationError
from tundravm.fetch import fetch, fetch_git
from tundravm.policy import Policy


def test_policy_requires_frozen_lock_for_bake(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend=InProcessBackend()).set_policy(
        Policy(require_frozen_lock=True)
    )

    with pytest.raises(PolicyError):
        image.bake()

    image.lock()
    result = image.bake(frozen=True)
    assert result.artifact_for(profile="default", target="qemu") is not None


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
