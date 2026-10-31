"""Tests for Cache / Build / CacheDecl shell fragment generation."""

import os
import subprocess
from pathlib import Path

import pytest

from tundravm.build_cache import (
    Build,
    Cache,
    CacheDecl,
)

# ── Directory artifacts ─────────────────────────────────────────────


def _tree_decl() -> CacheDecl:
    return Cache.declare(
        "app",
        (Cache.dir(src=Build.build_path("app/out"), dest=Build.dest_path("opt/app"), name="app"),),
    )


def _listing(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found.append((rel, f"-> {os.readlink(path)}"))
        elif path.is_dir():
            found.append((rel, "dir"))
        else:
            found.append((rel, f"{oct(path.stat().st_mode & 0o777)} {path.read_text()}"))
    return found


@pytest.mark.parametrize("dotfiles_only", [False, True])
def test_exact_trees_store_and_restore_dotfiles_links_modes_and_empty_dirs(
    tmp_path: Path, dotfiles_only: bool
) -> None:
    out = tmp_path / "buildroot" / "build" / "app" / "out"
    out.mkdir(parents=True)
    (out / ".config").write_text("hidden\n")
    (out / "empty").mkdir()
    if not dotfiles_only:
        (out / "bin").mkdir()
        (out / "bin" / "app").write_text("#!/bin/sh\n")
        (out / "bin" / "app").chmod(0o750)
        (out / "current").symlink_to("bin/app")
    env = {
        **os.environ,
        "BUILDROOT": str(tmp_path / "buildroot"),
        "DESTDIR": str(tmp_path / "dest"),
        "CACHE": str(tmp_path / "cache"),
    }
    script = _tree_decl().wrap("true", root="$CACHE")
    subprocess.run(["bash", "-c", f"set -euo pipefail; {script}"], env=env, check=True)
    assert _listing(tmp_path / "dest" / "opt" / "app") == _listing(out)
    assert _listing(tmp_path / "cache" / "app" / "app") == _listing(out)

    # a cache hit restores the stored tree without building
    restored = tmp_path / "dest"
    subprocess.run(["rm", "-rf", str(restored), str(out)], check=True)
    subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; {_tree_decl().wrap('false', root='$CACHE')}",
        ],
        env=env,
        check=True,
    )
    assert _listing(restored / "opt" / "app") == _listing(tmp_path / "cache" / "app" / "app")
    assert (restored / "opt" / "app" / ".config").read_text() == "hidden\n"
    assert (restored / "opt" / "app" / "empty").is_dir()
