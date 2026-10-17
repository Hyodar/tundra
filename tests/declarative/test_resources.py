"""``Resources.from_module``: files anchored at the declaring module, not the working dir."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from tundravm.declarative import File, Recipe, Resources
from tundravm.declarative.inputs import recipe_inputs
from tundravm.declarative.resources import resource_reads
from tundravm.errors import ValidationError

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def installed(tmp_path: Path) -> Iterator[Path]:
    """A package ``assetpkg`` importable from a site directory, with one asset."""
    package = tmp_path / "site" / "assetpkg"
    (package / "conf").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "mod.py").write_text("")
    (package / "conf" / "app.conf").write_text("port = 1\n")
    sys.path.insert(0, str(package.parent))
    try:
        yield package
    finally:
        sys.path.remove(str(package.parent))
        for name in ("assetpkg", "assetpkg.mod"):
            sys.modules.pop(name, None)


def test_resolves_next_to_the_module_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assets = Resources.from_module("examples.nethermind_base")
    assert assets.root == REPO / "examples"
    path = assets.file("nethermind-base/kernel/kernel-yocto.config")
    assert path == REPO / "examples" / "nethermind-base" / "kernel" / "kernel-yocto.config"


def test_an_installed_package_and_its_submodules(installed: Path) -> None:
    for name in ("assetpkg", "assetpkg.mod"):
        assets = Resources.from_module(name)
        assert assets.file("conf/app.conf") == installed / "conf" / "app.conf"
        assert assets.directory("conf") == installed / "conf"
        assert assets.text("conf/app.conf") == "port = 1\n"
    assert installed / "conf" / "app.conf" in resource_reads()


def test_a_script_without_a_spec_falls_back_to_its_file(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("x")
    module = types.ModuleType("_resources_script")
    module.__file__ = str(tmp_path / "script.py")
    sys.modules[module.__name__] = module
    try:
        assert Resources.from_module(module.__name__).file("data.txt") == tmp_path / "data.txt"
    finally:
        del sys.modules[module.__name__]


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("missing.conf", "Module 'assetpkg' has no resource file 'missing.conf'"),
        ("../outside", "must name a path inside the module's directory"),
        ("/etc/passwd", "must name a path inside the module's directory"),
    ],
)
def test_errors_name_the_module_and_the_path(installed: Path, relative: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message.replace("(", r"\(")):
        Resources.from_module("assetpkg").file(relative)


def test_unknown_module() -> None:
    with pytest.raises(ValidationError, match="no such module"):
        Resources.from_module("no_such_module_for_resources")


def test_watch_sees_the_absolute_paths(installed: Path) -> None:
    source = Resources.from_module("assetpkg").file("conf/app.conf")
    recipe = Recipe(name="r", items=(File.copy("/etc/app.conf", source),))
    assert source in recipe_inputs(recipe)
