import hashlib
import subprocess
from pathlib import Path

import pytest

from tundravm._source import GitSource, HttpSource, default_resolver
from tundravm.errors import ValidationError


def test_http_source_resolves_to_the_sha256_of_its_content(tmp_path: Path) -> None:
    source = tmp_path / "source.tar.gz"
    source.write_bytes(b"hello tdx")

    assert default_resolver(HttpSource(source.as_uri())) == hashlib.sha256(b"hello tdx").hexdigest()


def test_inline_pins_skip_resolution() -> None:
    commit = "a" * 40
    digest = "b" * 64

    assert default_resolver(GitSource("/nonexistent", commit)) == commit
    assert default_resolver(HttpSource("file:///nonexistent", sha256=digest)) == digest


def test_git_source_resolves_a_branch_to_its_commit(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path / "repo")

    assert default_resolver(GitSource(str(repo), "main")) == commit


def test_git_source_with_an_unknown_ref_is_a_validation_error(tmp_path: Path) -> None:
    repo, _ = _create_repo(tmp_path / "repo")

    with pytest.raises(ValidationError, match="Unable to resolve git ref"):
        default_resolver(GitSource(str(repo), "no-such-branch"))
    with pytest.raises(ValidationError, match="Git command failed"):
        default_resolver(GitSource(str(tmp_path / "missing"), "main"))


def _create_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], cwd=path)
    _run_git(["checkout", "-b", "main"], cwd=path)
    _run_git(["config", "user.email", "tdx@example.com"], cwd=path)
    _run_git(["config", "user.name", "TDX Test"], cwd=path)
    (path / "README.md").write_text("hello repo\n", encoding="utf-8")
    _run_git(["add", "README.md"], cwd=path)
    _run_git(["commit", "-m", "initial"], cwd=path)
    return path, _run_git(["rev-parse", "HEAD"], cwd=path)


def _run_git(argv: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(["git", *argv], cwd=cwd, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()
