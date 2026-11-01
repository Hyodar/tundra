"""Tests for the fixtures in ``tundravm.testing.pytest_plugin`` (auto-loaded via pytest11)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tundravm.backends import LocalLinuxBackend
from tundravm.declarative import Fragment, Package, Recipe, Variant, lint, lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.testing import FakeMkosi, assert_clean, fake_fragment, recipe_file
from tundravm.testing.pytest_plugin import CliRunner, CompileFactory


def test_plugin_is_registered_by_entry_point(request: pytest.FixtureRequest) -> None:
    plugin = request.config.pluginmanager.get_plugin("tundravm")
    assert plugin is not None
    assert plugin.__name__ == "tundravm.testing.pytest_plugin"


def test_recipe_fixture_is_minimal_and_clean(recipe: Recipe) -> None:
    assert isinstance(recipe, Recipe)
    assert recipe.name == "test"
    assert recipe.items == (Package("linux-image-amd64"),)
    assert [(v.name, v.outputs) for v in recipe.variants] == [("default", ("qemu",))]
    assert lint(recipe) == ()


def test_recipe_fixture_builds_against_the_fake_mkosi(
    recipe: Recipe, tmp_path: Path, fake_mkosi: FakeMkosi
) -> None:
    curl = dataclasses.replace(recipe, items=(Package("curl"), Package("linux-image-amd64")))
    assert_clean(curl)

    _, (artifact,) = bake_image(
        lower(curl), None, locked=None, backend=LocalLinuxBackend(), build=tmp_path / "build"
    )
    assert (artifact.profile, artifact.target) == ("default", "qemu")
    assert artifact.path.is_file()
    assert (tmp_path / "build" / "mkosi" / "payload" / "mkosi.conf").is_file()
    assert fake_mkosi.verbs() == ["summary", "build"]


def test_compiled_factory_uses_fresh_dirs(
    recipe: Recipe, compiled: CompileFactory, tmp_path: Path
) -> None:
    demo = fake_fragment("demo", packages=("curl",), init="echo demo")
    azure = Variant("azure", outputs=("azure",), add=Fragment("azure", (Package("jq"),)))
    extended = dataclasses.replace(
        recipe,
        items=(demo,),
        variants=(*recipe.variants, azure),
    )

    first = compiled(extended, "default")
    both = compiled(extended)
    assert first.root != both.root
    assert first.root.is_relative_to(tmp_path)
    assert first.profiles == ("default",)
    assert both.profiles == ("default", "azure")
    assert "curl" in first.conf()
    assert "echo demo" in first.runtime_init()
    assert "jq" in both.conf(profile="azure")
    assert compiled(extended, profiles=["azure"]).profiles == ("azure",)


def test_run_cli_fixture(run_cli: CliRunner, tmp_path: Path) -> None:
    source = 'from tundravm.declarative import Fragment, Recipe\nrecipe = Recipe(name="t")\n'
    path = recipe_file(tmp_path, source)
    code, out, err = run_cli("show", path, "--json")
    assert code == 0
    assert len(json.loads(out)["digest"]) == 64
    assert err == ""
