"""Tests for the fixtures in ``tundravm.testing.pytest_plugin`` (auto-loaded via pytest11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.testing import FakeModule, assert_clean, recipe_file
from tundravm.testing.pytest_plugin import CliRunner, CompileFactory


def test_plugin_is_registered_by_entry_point(request: pytest.FixtureRequest) -> None:
    plugin = request.config.pluginmanager.get_plugin("tundravm")
    assert plugin is not None
    assert plugin.__name__ == "tundravm.testing.pytest_plugin"


def test_image_fixture_is_fresh_and_reproducible(image: Image) -> None:
    assert isinstance(image, Image)
    assert image.reproducible
    assert image.backend is None
    assert image.applied_modules() == ()


def test_inprocess_image_bakes_into_tmp_path(inprocess_image: Image, tmp_path: Path) -> None:
    assert isinstance(inprocess_image.backend, InProcessBackend)
    assert inprocess_image.build_dir == tmp_path / "build"
    inprocess_image.install("curl")
    assert_clean(inprocess_image, strict=True)

    result = inprocess_image.bake()
    assert result.profiles["default"].artifacts["qemu"].path.is_file()
    assert (tmp_path / "build" / "mkosi" / "default" / "mkosi.conf").is_file()


def test_compiled_factory_uses_fresh_dirs(
    image: Image, compiled: CompileFactory, tmp_path: Path
) -> None:
    image.apply(FakeModule("demo", packages=("curl",), init_script="echo demo"))
    with image.profile("azure"):
        image.install("jq")

    first = compiled(image)
    both = compiled(image, profiles=["default", "azure"])
    assert first.root != both.root
    assert first.root.is_relative_to(tmp_path)
    assert first.profiles == ("default",)
    assert "curl" in first.conf()
    assert "echo demo" in first.runtime_init()
    assert "jq" in both.conf(profile="azure")


def test_run_cli_fixture(run_cli: CliRunner, tmp_path: Path) -> None:
    path = recipe_file(tmp_path, "from tundravm import Image\nimg = Image()\n")
    code, out, err = run_cli("inspect", path, "--json")
    assert code == 0
    assert len(json.loads(out)["digest"]) == 64
    assert err == ""
