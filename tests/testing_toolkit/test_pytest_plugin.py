"""Tests for the fixtures in ``tundravm.testing.pytest_plugin`` (auto-loaded via pytest11)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tundravm import Fragment, Package, Recipe, Variant, lint
from tundravm.testing import assert_clean, bake_in_process, fake_fragment, recipe_file
from tundravm.testing.pytest_plugin import CliRunner, CompileFactory


def test_plugin_is_registered_by_entry_point(request: pytest.FixtureRequest) -> None:
    plugin = request.config.pluginmanager.get_plugin("tundravm")
    assert plugin is not None
    assert plugin.__name__ == "tundravm.testing.pytest_plugin"


def test_recipe_fixture_is_minimal_and_clean(recipe: Recipe) -> None:
    assert isinstance(recipe, Recipe)
    assert recipe.name == "test"
    assert recipe.common == Fragment("test", (Package("linux-image-amd64"),))
    assert [(v.name, v.outputs) for v in recipe.variants] == [("default", ("qemu",))]
    assert lint(recipe) == ()


def test_recipe_fixture_bakes_in_process(recipe: Recipe, tmp_path: Path) -> None:
    curl = dataclasses.replace(
        recipe, common=Fragment("test", (Package("curl"), Package("linux-image-amd64")))
    )
    assert_clean(curl, strict=True)

    (artifact,) = bake_in_process(curl, out=tmp_path / "build")
    assert (artifact.variant, artifact.target) == ("default", "qemu")
    assert artifact.simulated
    assert artifact.path.is_file()
    assert (tmp_path / "build" / "mkosi" / "default" / "mkosi.conf").is_file()


def test_compiled_factory_uses_fresh_dirs(
    recipe: Recipe, compiled: CompileFactory, tmp_path: Path
) -> None:
    demo = fake_fragment("demo", packages=("curl",), init="echo demo")
    azure = Variant("azure", target="azure", add=Fragment("azure", (Package("jq"),)))
    extended = dataclasses.replace(
        recipe,
        common=Fragment("test", (demo,)),
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
    assert compiled(extended, variants=["azure"]).profiles == ("azure",)


def test_run_cli_fixture(run_cli: CliRunner, tmp_path: Path) -> None:
    source = (
        'from tundravm import Fragment, Recipe\nrecipe = Recipe(name="t", common=Fragment("t"))\n'
    )
    path = recipe_file(tmp_path, source)
    code, out, err = run_cli("inspect", path, "--json")
    assert code == 0
    assert len(json.loads(out)["digest"]) == 64
    assert err == ""
