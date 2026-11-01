"""``Local``: what a project directory's digest covers, its copy, and the paths it refuses."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tundravm._source import LocalSource, copy_local, local_digest, local_files
from tundravm.errors import SourceError, ValidationError
from tundravm.sources import Local

Change = Callable[[Path], object]


def _retarget(link: Path, target: str) -> None:
    link.unlink()
    link.symlink_to(target)


UNCHANGED: list[tuple[str, Change]] = [
    ("a file's time", lambda d: os.utime(d / "main.go", (1, 1))),
    ("a mode bit but exec", lambda d: (d / "main.go").chmod(0o600)),
    ("an ignored file", lambda d: (d / "target" / "out").write_text("y")),
    ("a .git directory", lambda d: (d / ".git" / "HEAD").write_text("y")),
]
CHANGED: list[tuple[str, Change]] = [
    ("a file's bytes", lambda d: (d / "main.go").write_text("package app\n")),
    ("the exec bit", lambda d: (d / "main.go").chmod(0o755)),
    ("a symlink's target", lambda d: _retarget(d / "link", "go.mod")),
    ("a new file", lambda d: (d / "new.go").write_text("x")),
    ("a renamed file", lambda d: (d / "main.go").rename(d / "app.go")),
]


def _directory(root: Path, *, repository: bool) -> Path:
    """``app/`` with a file, an executable, a symlink, ``.git`` and an ignored ``target/``."""
    app = root / "app"
    (app / "target").mkdir(parents=True)
    (app / ".git").mkdir()
    (app / ".git" / "HEAD").write_text("x")
    (app / "main.go").write_text("package main\n")
    (app / "go.mod").write_text("module app\n")
    (app / "run.sh").write_text("#!/bin/sh\n")
    (app / "run.sh").chmod(0o755)
    (app / "link").symlink_to("main.go")
    (app / "target" / "out").write_text("x")
    (app / ".gitignore").write_text("target/\n")
    if repository:
        (app / ".git" / "HEAD").unlink()
        (app / ".git").rmdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "app/main.go"], check=True)
        (app / ".git").mkdir()
        (app / ".git" / "HEAD").write_text("x")
    return app


@pytest.mark.parametrize(
    ("repository", "listed"),
    [
        (True, [".gitignore", "go.mod", "link", "main.go", "run.sh"]),
        (False, [".gitignore", "go.mod", "link", "main.go", "run.sh", "target/out"]),
    ],
    ids=["git work tree", "plain directory"],
)
def test_the_digest_covers_what_a_checkout_of_the_directory_holds(
    tmp_path: Path, repository: bool, listed: list[str]
) -> None:
    app = _directory(tmp_path, repository=repository)
    assert local_files(app) == listed
    pin = local_digest(app)
    copy = tmp_path / "copy"
    copy.mkdir()
    copy_local(LocalSource("app", directory=app), pin, copy)
    assert local_digest(copy) == pin and os.readlink(copy / "link") == "main.go"
    with pytest.raises(SourceError, match="changed since the lock"):
        copy_local(LocalSource("app", directory=app), "0" * 64, tmp_path / "stale")
    for what, change in UNCHANGED:
        if what == "an ignored file" and not repository:
            continue
        change(app)
        assert local_digest(app) == pin, what
    for what, change in CHANGED:
        change(app)
        assert local_digest(app) != pin, what
        pin = local_digest(app)


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("/srv/app", "is absolute"),
        ("../../..", "outside the project directory"),
        ("missing", "is not a directory"),
    ],
)
def test_local_refuses_a_path_the_lock_cannot_name(path: str, refused: str) -> None:
    with pytest.raises(ValidationError, match=refused):
        Local(path)
    found = Local("../fixtures/rust-probe/")
    assert found.path == "../fixtures/rust-probe"
    assert found.directory == REPO_ROOT / "tests" / "fixtures" / "rust-probe"
