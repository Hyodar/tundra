"""Tests for Cache / Build / CacheDecl shell fragment generation."""

import os
import subprocess
from pathlib import Path

import pytest

from tundravm.build_cache import (
    Build,
    Cache,
    CacheDecl,
    CacheDir,
    CacheFile,
    ChrootPath,
    DestPath,
    SrcPath,
)

# ── Path helpers ────────────────────────────────────────────────────


def test_src_path_str() -> None:
    assert str(Build.build_path("foo/bar")) == "$BUILDROOT/build/foo/bar"


def test_dest_path_str() -> None:
    assert str(Build.dest_path("usr/bin/app")) == "$DESTDIR/usr/bin/app"


def test_chroot_path_str() -> None:
    assert str(Build.chroot_path("raiko")) == "/build/raiko"


def test_path_types() -> None:
    assert isinstance(Build.build_path("x"), SrcPath)
    assert isinstance(Build.dest_path("x"), DestPath)
    assert isinstance(Build.chroot_path("x"), ChrootPath)


# ── Cache.file / Cache.dir ──────────────────────────────────────────


def test_cache_file_returns_cache_file() -> None:
    f = Cache.file(
        src=Build.build_path("pkg/out/bin"),
        dest=Build.dest_path("usr/bin/app"),
        name="app",
    )
    assert isinstance(f, CacheFile)
    assert f.name == "app"
    assert f.mode == "0755"


def test_cache_file_custom_mode() -> None:
    f = Cache.file(
        src=Build.build_path("pkg/out/cfg"),
        dest=Build.dest_path("etc/app.conf"),
        name="cfg",
        mode="0644",
    )
    assert f.mode == "0644"


def test_cache_dir_returns_cache_dir() -> None:
    d = Cache.dir(
        src=Build.build_path("pkg/out/plugins"),
        dest=Build.dest_path("etc/app/plugins"),
        name="plugins",
    )
    assert isinstance(d, CacheDir)
    assert d.name == "plugins"


# ── Cache.declare ───────────────────────────────────────────────────


def test_declare_returns_cache_decl() -> None:
    decl = Cache.declare(
        "pkg-v1",
        (
            Cache.file(
                src=Build.build_path("pkg/out/bin"),
                dest=Build.dest_path("usr/bin/app"),
                name="app",
            ),
        ),
    )
    assert isinstance(decl, CacheDecl)
    assert decl.key == "pkg-v1"
    assert len(decl.artifacts) == 1


def test_declare_sanitizes_slashes_in_key() -> None:
    decl = Cache.declare(
        "raiko-feat/tdx",
        (
            Cache.file(
                src=Build.build_path("raiko/target/release/raiko-host"),
                dest=Build.dest_path("usr/bin/raiko"),
                name="raiko",
            ),
        ),
    )
    assert decl.key == "raiko-feat_tdx"


# ── CacheDecl.wrap ──────────────────────────────────────────────────


def test_wrap_single_file() -> None:
    decl = Cache.declare(
        "tdxs-master",
        (
            Cache.file(
                src=Build.build_path("tdxs-master/build/tdxs"),
                dest=Build.dest_path("usr/bin/tdxs"),
                name="tdxs",
            ),
        ),
    )
    result = decl.wrap("echo building")

    # Cache check
    assert '[ -d "$BUILDDIR/tdxs-master" ]' in result
    # Build command on miss
    assert "echo building" in result
    # Store: src → cache
    assert "$BUILDROOT/build/tdxs-master/build/tdxs" in result
    assert '"$BUILDDIR/tdxs-master"/tdxs' in result
    # Restore: cache → dest
    assert "$DESTDIR/usr/bin/tdxs" in result


def test_wrap_multiple_artifacts() -> None:
    decl = Cache.declare(
        "nethermind-1.32.3-linux-x64",
        (
            Cache.file(
                src=Build.build_path("nethermind-1.32.3/out/nethermind"),
                dest=Build.dest_path("usr/bin/nethermind"),
                name="nethermind",
            ),
            Cache.file(
                src=Build.build_path("nethermind-1.32.3/out/NLog.config"),
                dest=Build.dest_path("etc/nethermind-surge/NLog.config"),
                name="NLog.config",
                mode="0644",
            ),
            Cache.dir(
                src=Build.build_path("nethermind-1.32.3/out/plugins"),
                dest=Build.dest_path("etc/nethermind-surge/plugins"),
                name="plugins",
            ),
        ),
    )
    result = decl.wrap("dotnet publish ...")

    # Store all three
    assert '"$BUILDDIR/nethermind-1.32.3-linux-x64"/nethermind' in result
    assert '"$BUILDDIR/nethermind-1.32.3-linux-x64"/NLog.config' in result
    assert '"$BUILDDIR/nethermind-1.32.3-linux-x64"/plugins' in result

    # Restore all three
    assert "$DESTDIR/usr/bin/nethermind" in result
    assert "$DESTDIR/etc/nethermind-surge/NLog.config" in result
    assert "$DESTDIR/etc/nethermind-surge/plugins" in result


def test_wrap_structure_if_not_then_fi() -> None:
    """Verify the if-not/fi structure: build+store on miss, restore always."""
    decl = Cache.declare(
        "pkg-v1",
        (
            Cache.file(
                src=Build.build_path("pkg/out/bin"),
                dest=Build.dest_path("usr/bin/app"),
                name="app",
            ),
        ),
    )
    result = decl.wrap("make build")

    # Structure: if !(cache_exists); then build && store; fi && restore
    assert result.startswith("if ! (")
    assert "make build" in result
    assert "fi && " in result


# ── Directory artifacts ─────────────────────────────────────────────


def _tree_decl() -> CacheDecl:
    return Cache.declare(
        "app",
        (Cache.dir(src=Build.build_path("app/out"), dest=Build.dest_path("opt/app"), name="app"),),
    )


def test_exact_trees_copy_the_whole_directory() -> None:
    script = _tree_decl().wrap("true", root="$CACHE", exact_trees=True)
    assert 'cp -a "$BUILDROOT/build/app/out/." "$CACHE/app"/app/' in script
    assert 'mkdir -p "$DESTDIR/opt/app" && cp -a "$CACHE/app"/app/. "$DESTDIR/opt/app/"' in script
    assert "cp -r" not in script and "/*" not in script


def test_historical_trees_keep_the_glob_copy() -> None:
    script = _tree_decl().wrap("true", root="$CACHE")
    assert 'cp -r "$BUILDROOT/build/app/out"/* "$CACHE/app"/app/' in script
    assert 'cp -r "$CACHE/app"/app/* "$DESTDIR/opt/app"/' in script


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
    script = _tree_decl().wrap("true", root="$CACHE", exact_trees=True)
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
            f"set -euo pipefail; {_tree_decl().wrap('false', root='$CACHE', exact_trees=True)}",
        ],
        env=env,
        check=True,
    )
    assert _listing(restored / "opt" / "app") == _listing(tmp_path / "cache" / "app" / "app")
    assert (restored / "opt" / "app" / ".config").read_text() == "hidden\n"
    assert (restored / "opt" / "app" / "empty").is_dir()
