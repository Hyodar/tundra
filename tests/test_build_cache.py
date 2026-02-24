"""Tests for Cache / Build / CacheDecl shell fragment generation."""

from tdx.build_cache import Build, Cache, CacheDecl, CacheDir, CacheFile, DestPath, OutPath, SrcPath

# ── Path helpers ────────────────────────────────────────────────────


def test_src_path_str() -> None:
    assert str(Build.build_path("foo/bar")) == "$BUILDROOT/build/foo/bar"


def test_dest_path_str() -> None:
    assert str(Build.dest_path("usr/bin/app")) == "$DESTDIR/usr/bin/app"


def test_output_path_str() -> None:
    assert str(Build.output_path("go-cache")) == "$BUILDDIR/go-cache"


def test_path_types() -> None:
    assert isinstance(Build.build_path("x"), SrcPath)
    assert isinstance(Build.dest_path("x"), DestPath)
    assert isinstance(Build.output_path("x"), OutPath)


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


def test_wrap_with_output_path_src() -> None:
    """Cache.file src can be an OutPath too."""
    decl = Cache.declare(
        "pkg-v1",
        (
            Cache.file(
                src=Build.output_path("pkg-out/binary"),
                dest=Build.dest_path("usr/bin/app"),
                name="app",
            ),
        ),
    )
    result = decl.wrap("go build")
    assert "$BUILDDIR/pkg-out/binary" in result
