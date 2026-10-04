"""MkosiOptions: the mkosi knobs grouped off Image's constructor."""

from __future__ import annotations

import inspect
import io
from pathlib import Path

import pytest

from tundravm import Image, Kernel, MkosiOptions
from tundravm.cli import EXIT_OK, main
from tundravm.errors import ValidationError
from tundravm.lockfile import recipe_digest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BACKPORTS_TREE = (
    "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
)
DEFAULT_DIGEST = "82a726b26acfb531e3645ebd47e006e5b7536fd75b5773c2c1bcc930d16a78ee"
QEMU_BASIC_DIGEST = "571668210b9086615b19fde56a4b86cf0aa65f2ec480b2497a42641739d15c8e"
STRIP_VERSION = "sed -i '/^IMAGE_VERSION=/d' \"$BUILDROOT/usr/lib/os-release\" "

# Image()._recipe_payload() captured before the options moved off Image.
DEFAULT_PAYLOAD: dict[str, object] = {
    "base": "debian/bookworm",
    "arch": "x86_64",
    "default_profile": "default",
    "init_scripts": [],
    "profiles": {
        "default": {
            "packages": [],
            "build_packages": [],
            "build_sources": [],
            "output_targets": ["qemu"],
            "phases": {"finalize": [{"argv": [STRIP_VERSION], "env": {}, "cwd": None}]},
            "repositories": [],
            "files": [],
            "skeleton_files": [],
            "templates": [],
            "users": [],
            "services": [],
            "partitions": [],
            "hooks": [{"phase": "finalize", "after_phase": None, "argv": [STRIP_VERSION]}],
            "secrets": [],
            "debloat": {
                "enabled": True,
                "paths_remove": [
                    "/etc/credstore",
                    "/etc/machine-id",
                    "/etc/ssh/ssh_host_*_key*",
                    "/etc/systemd/network",
                    "/usr/lib/modules",
                    "/usr/lib/pcrlock.d",
                    "/usr/lib/systemd/catalog",
                    "/usr/lib/systemd/network",
                    "/usr/lib/systemd/user",
                    "/usr/lib/systemd/user-generators",
                    "/usr/lib/tmpfiles.d",
                    "/usr/lib/udev/hwdb.bin",
                    "/usr/lib/udev/hwdb.d",
                    "/usr/share/bash-completion",
                    "/usr/share/bug",
                    "/usr/share/debconf",
                    "/usr/share/doc",
                    "/usr/share/gcc",
                    "/usr/share/gdb",
                    "/usr/share/info",
                    "/usr/share/initramfs-tools",
                    "/usr/share/lintian",
                    "/usr/share/locale",
                    "/usr/share/man",
                    "/usr/share/menu",
                    "/usr/share/mime",
                    "/usr/share/perl5/debconf",
                    "/usr/share/polkit-1",
                    "/usr/share/systemd",
                    "/usr/share/zsh",
                ],
                "systemd_minimize": True,
            },
        }
    },
}


def _payload(img: Image) -> dict[str, object]:
    return img._recipe_payload(profile_names=img._active_profiles)


def test_image_constructor_is_small_and_keyword_only() -> None:
    params = inspect.signature(Image).parameters
    assert list(params) == [
        "base",
        "arch",
        "backend",
        "build_dir",
        "reproducible",
        "policy",
        "kernel",
        "mirror",
        "tools_tree_mirror",
        "default_profile",
        "mkosi",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_defaults() -> None:
    options = MkosiOptions()
    assert options.with_network is True
    assert options.clean_package_metadata is True
    assert options.manifest_format == "json"
    assert options.compress_output is None
    assert options.output_directory is None
    assert options.seed is None
    assert options.sandbox_trees == ()
    assert options.package_cache_directory is None
    assert options.init_script is None
    assert options.environment == {}
    assert options.environment_passthrough is None
    assert options.emit_mode == "per_directory"
    assert options.generate_version_script is False
    assert options.generate_cloud_postoutput is True
    assert options.non_defaults() == {}
    assert Image().mkosi == options


def test_options_are_frozen_and_normalized() -> None:
    options = MkosiOptions(sandbox_trees=["a:b"], environment_passthrough=["X"])  # type: ignore[arg-type]
    assert options.sandbox_trees == ("a:b",)
    assert options.environment_passthrough == ("X",)
    with pytest.raises(AttributeError):
        options.seed = "x"  # type: ignore[misc]


def test_mkosi_options_chains_and_merges() -> None:
    img = Image()
    result = img.mkosi_options(seed="abc").install("curl").mkosi_options(with_network=False)

    assert result is img
    assert img.mkosi == MkosiOptions(seed="abc", with_network=False)
    assert img.mkosi.non_defaults() == {"with_network": False, "seed": "abc"}
    config = img._emit_config()
    assert config.seed == "abc"
    assert config.with_network is False


def test_mkosi_options_is_profile_independent() -> None:
    img = Image()
    with img.profile("dev"):
        img.mkosi_options(package_cache_directory="mkosi.cache")
    assert img.mkosi.package_cache_directory == "mkosi.cache"


def test_mkosi_options_rejects_unknown_names() -> None:
    with pytest.raises(ValidationError, match="Unknown mkosi option"):
        Image().mkosi_options(with_networking=False)


def test_set_kernel_chains() -> None:
    kernel = Kernel.tdx_kernel("6.8")
    img = Image()
    assert img.set_kernel(kernel) is img
    assert img.kernel is kernel
    assert img._emit_config().kernel is kernel


def test_backports_updates_sandbox_trees_once() -> None:
    img = Image(mkosi=MkosiOptions(sandbox_trees=("a:b",), seed="s"))
    img.backports().backports()

    assert img.mkosi.sandbox_trees == ("a:b", BACKPORTS_TREE)
    assert img.mkosi.seed == "s"
    assert img._emit_config().sandbox_trees == ("a:b", BACKPORTS_TREE)


def test_default_payload_is_unchanged() -> None:
    img = Image()
    assert _payload(img) == DEFAULT_PAYLOAD
    assert recipe_digest(_payload(img)) == DEFAULT_DIGEST


def test_options_do_not_enter_the_payload() -> None:
    img = Image(
        mkosi=MkosiOptions(
            seed="s",
            compress_output="zstd",
            environment={"A": "1"},
            emit_mode="native_profiles",
        )
    )
    assert _payload(img) == DEFAULT_PAYLOAD


def test_changed_options_recompile(tmp_path: Path) -> None:
    img = Image()
    tree = img.compile(tmp_path / "mkosi").path
    assert "Seed=630b" not in (tree / "default" / "mkosi.conf").read_text(encoding="utf-8")

    img.mkosi_options(seed="630b")
    img.compile(tmp_path / "mkosi")
    assert "Seed=630b" in (tree / "default" / "mkosi.conf").read_text(encoding="utf-8")


def test_explain_lists_only_non_default_options() -> None:
    img = Image()
    assert img.explain()["build_options"] == {}
    assert "Build options" not in img.summary()

    img.mkosi_options(
        seed="s",
        with_network=False,
        environment={"B": "2", "A": "1"},
        init_script="#!/bin/sh\n",
    )
    options = img.explain()["build_options"]
    assert options == {
        "with_network": False,
        "seed": "s",
        "init_script": "sha256:a8076d3d28d2",
        "environment": {"A": "1", "B": "2"},
    }
    assert (
        "Build options: with_network=no seed=s init_script=sha256:a8076d3d28d2"
        " environment=A=1,B=2\n" in img.summary()
    )


def test_qemu_basic_digest_is_unchanged() -> None:
    out = io.StringIO()
    assert main(["digest", str(EXAMPLES / "qemu_basic.py")], stdout=out) == EXIT_OK
    assert out.getvalue().strip() == QEMU_BASIC_DIGEST


def test_surge_tree_is_up_to_date() -> None:
    surge = EXAMPLES / "surge-tdx-prover"
    out = io.StringIO()
    code = main(
        ["compile", str(surge / "image.py"), "--out", str(surge / "mkosi"), "--check"],
        stdout=out,
    )
    assert (code, out.getvalue()) == (EXIT_OK, "tree is up to date with the recipe\n")
