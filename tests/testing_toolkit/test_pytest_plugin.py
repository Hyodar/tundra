"""Tests for the fixtures in ``tundravm.testing.pytest_plugin`` (auto-loaded via pytest11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tundravm.backends import LocalLinuxBackend
from tundravm.declarative import Package, Recipe, lint, lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.testing import FakeMkosi, image_file
from tundravm.testing.pytest_plugin import CliRunner


def test_plugin_is_registered_by_entry_point(request: pytest.FixtureRequest) -> None:
    plugin = request.config.pluginmanager.get_plugin("tundravm")
    assert plugin is not None
    assert plugin.__name__ == "tundravm.testing.pytest_plugin"


def test_fake_mkosi_fixture_builds_with_nothing_forked(
    tmp_path: Path, fake_mkosi: FakeMkosi
) -> None:
    curl = Recipe(name="test", items=(Package("curl"), Package("linux-image-amd64")))
    assert lint(curl) == ()

    _, (artifact,) = bake_image(
        lower(curl), None, locked=None, backend=LocalLinuxBackend(), build=tmp_path / "build"
    )
    assert (artifact.profile, artifact.target) == ("default", "qemu")
    assert artifact.path.is_file()
    assert (tmp_path / "build" / "mkosi" / "payload" / "mkosi.conf").is_file()
    assert fake_mkosi.verbs() == ["summary", "build"]


def test_run_cli_fixture(run_cli: CliRunner, tmp_path: Path) -> None:
    source = 'from tundravm.declarative import Fragment, Recipe\nrecipe = Recipe(name="t")\n'
    path = image_file(tmp_path, source)
    code, out, err = run_cli("show", path, "--json")
    assert code == 0
    assert len(json.loads(out)["digest"]) == 64
    assert err == ""
