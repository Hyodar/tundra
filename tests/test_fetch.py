import hashlib
import subprocess
import warnings
from pathlib import Path

import pytest

from tdx.errors import ReproducibilityError, ValidationError
from tdx.fetch import MutableRefWarning, fetch, fetch_git


def test_fetch_requires_sha256(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")

    with pytest.raises(ValidationError):
        fetch(source.as_uri(), sha256="", cache_dir=tmp_path / "cache")


def test_fetch_caches_by_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    payload = b"hello tdx"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    first = fetch(source.as_uri(), sha256=digest, cache_dir=tmp_path / "cache")
    source.write_bytes(b"mutated source content")
    second = fetch(source.as_uri(), sha256=digest, cache_dir=tmp_path / "cache")

    assert first == second
    assert second.read_bytes() == payload


def test_fetch_raises_on_hash_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"mismatch")

    with pytest.raises(ReproducibilityError):
        fetch(source.as_uri(), sha256="0" * 64, cache_dir=tmp_path / "cache")


def test_fetch_git_resolves_commit_verifies_tree_and_caches(tmp_path: Path) -> None:
    repo, commit, tree_hash = _create_repo(tmp_path / "repo")
    cache_dir = tmp_path / "git-cache"

    first = fetch_git(str(repo), ref=commit, tree_hash=tree_hash, cache_dir=cache_dir)
    second = fetch_git(str(repo), ref=commit, tree_hash=tree_hash, cache_dir=cache_dir)

    assert first.path == second.path
    assert first.commit == commit
    assert first.tree_hash == tree_hash
    assert first.mutable_ref is False


def test_fetch_git_warns_on_mutable_ref_by_default(tmp_path: Path) -> None:
    repo, _, tree_hash = _create_repo(tmp_path / "repo")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fetch_git(str(repo), ref="main", tree_hash=tree_hash, cache_dir=tmp_path / "cache")

    assert result.mutable_ref is True
    assert any(isinstance(item.message, MutableRefWarning) for item in caught)


def test_fetch_git_can_escalate_mutable_ref_to_error(tmp_path: Path) -> None:
    repo, _, tree_hash = _create_repo(tmp_path / "repo")

    with pytest.raises(ReproducibilityError) as excinfo:
        fetch_git(
            str(repo),
            ref="main",
            tree_hash=tree_hash,
            cache_dir=tmp_path / "cache",
            mutable_ref_policy="error",
        )

    assert "not allowed" in str(excinfo.value)


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
