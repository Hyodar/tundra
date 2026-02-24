"""Tests for BuildCaches / CacheEntry shell fragment generation."""

from tdx.build_cache import BuildCaches, CacheEntry


def test_has_returns_shell_check() -> None:
    caches = BuildCaches()
    result = caches.has("nethermind-1.32.3")
    assert '[ -d "$BUILDDIR/nethermind-1.32.3" ]' in result
    assert '[ "$(ls -A "$BUILDDIR/nethermind-1.32.3" 2>/dev/null)" ]' in result


def test_create_returns_cache_entry() -> None:
    caches = BuildCaches()
    entry = caches.create("tdxs-master")
    assert isinstance(entry, CacheEntry)
    assert entry.name == "tdxs-master"


def test_get_returns_cache_entry() -> None:
    caches = BuildCaches()
    entry = caches.get("tdxs-master")
    assert isinstance(entry, CacheEntry)
    assert entry.name == "tdxs-master"


def test_add_file_returns_mkdir_and_install() -> None:
    entry = CacheEntry("pkg-v1")
    result = entry.add_file("binary", "$BUILDDIR/out/binary")
    assert 'mkdir -p "$BUILDDIR/pkg-v1"' in result
    assert 'install -m 0755 $BUILDDIR/out/binary "$BUILDDIR/pkg-v1/binary"' in result


def test_add_file_custom_mode() -> None:
    entry = CacheEntry("pkg-v1")
    result = entry.add_file("config", "$BUILDDIR/out/config", mode="0644")
    assert "install -m 0644" in result


def test_add_dir_returns_mkdir_and_cp() -> None:
    entry = CacheEntry("pkg-v1")
    result = entry.add_dir("plugins", "$BUILDDIR/out/plugins")
    assert 'mkdir -p "$BUILDDIR/pkg-v1/plugins"' in result
    assert 'cp -r $BUILDDIR/out/plugins/* "$BUILDDIR/pkg-v1/plugins/"' in result


def test_copy_file_returns_install() -> None:
    entry = CacheEntry("pkg-v1")
    result = entry.copy_file("binary", "$DESTDIR/usr/bin/pkg")
    assert 'install -m 0755 "$BUILDDIR/pkg-v1/binary" $DESTDIR/usr/bin/pkg' in result


def test_copy_file_custom_mode() -> None:
    entry = CacheEntry("pkg-v1")
    result = entry.copy_file("config", "$DESTDIR/etc/pkg.conf", mode="0644")
    assert "install -m 0644" in result


def test_copy_dir_returns_mkdir_and_cp() -> None:
    entry = CacheEntry("pkg-v1")
    result = entry.copy_dir("plugins", "$DESTDIR/etc/app/plugins")
    assert "mkdir -p $DESTDIR/etc/app/plugins" in result
    assert 'cp -r "$BUILDDIR/pkg-v1/plugins"/* $DESTDIR/etc/app/plugins/' in result


def test_image_caches_property() -> None:
    from tdx import Image

    img = Image(reproducible=False)
    assert isinstance(img.caches, BuildCaches)
