"""Every stage of the example is an image file that loads, lints clean and compiles
deterministically."""

import ast
import importlib
import json
from pathlib import Path

import pytest

from tests.helpers import EXAMPLE_RECIPES, REPO_ROOT, run_main
from tundravm._reference import MODULES
from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import Recipe, compile, lock, write_lock
from tundravm.recipe import load_file

INTERNAL_MODULES = ("tundravm._modules", "tundravm._options", "tundravm._source")

EXAMPLES = pytest.mark.parametrize(
    "path", EXAMPLE_RECIPES, ids=lambda path: path.relative_to(REPO_ROOT).as_posix()
)


def _recipe(path: Path) -> Recipe:
    return load_file(path, extra_paths=[REPO_ROOT]).recipe


def test_examples_are_syntax_valid() -> None:
    examples = sorted(Path("examples").rglob("*.py"))
    assert examples

    for path in examples:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))


def test_examples_use_only_the_public_or_internal_api() -> None:
    for path in sorted(Path("examples").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                assert not module.startswith(INTERNAL_MODULES), f"{path} imports {module}"


def _public_homes() -> dict[int, str]:
    """``id(object) -> namespace`` for every public object: each has exactly one."""
    homes: dict[int, str] = {}
    for module in MODULES:
        imported = importlib.import_module(module)
        for name in imported.__all__:
            value = getattr(imported, name)
            if isinstance(value, type) or callable(value):
                homes[id(value)] = module
    return homes


def test_examples_import_each_public_name_from_its_home() -> None:
    homes = _public_homes()
    found = []
    for path in sorted(Path("examples").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith(
                "tundravm"
            ):
                continue
            module = importlib.import_module(node.module or "")
            for alias in node.names:
                value = getattr(module, alias.name)
                home = homes.get(id(value))
                if home is not None and home != node.module:
                    found.append(f"{path}: {alias.name} from {node.module}, not {home}")
    assert found == []


@EXAMPLES
def test_example_binds_an_image_or_the_internal_recipe(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert bound & {"image", "recipe"}, f"{path.name}: bind `image` (or `recipe` internally)"
    assert isinstance(_recipe(path), Recipe)


@EXAMPLES
def test_example_compiles_deterministically(path: Path) -> None:
    first = compile(_recipe(path))
    second = compile(_recipe(path))
    assert first.entries
    assert (first.digest, first.entries) == (second.digest, second.entries)


@EXAMPLES
def test_examples_name_no_backend(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader picks the backend: TUNDRAVM_BACKEND, else this host's."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "backend" not in bound, f"{path.name}: drop `backend = ...`"
    monkeypatch.setenv("TUNDRAVM_BACKEND", "lima")
    loaded = load_file(path, extra_paths=[REPO_ROOT])
    assert isinstance(loaded.backend, LimaMkosiBackend) and loaded.backend_origin == "env"


@pytest.mark.parametrize("path", EXAMPLE_RECIPES[:1], ids=lambda path: path.name)
def test_the_first_stage_builds_the_same_identity_twice(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    pins = lock(_recipe(path), resolver=_fixed_pins)  # the build refuses no lock offline here
    identities = []
    for root in ("a", "b"):
        monkeypatch.setenv("TUNDRAVM_BUILD", root)
        write_lock(pins, Path(root) / "tundravm.lock")
        code, _ = run_main("build", str(path), "--pythonpath", str(REPO_ROOT))
        assert code == 0
        (latest,) = Path(root).glob("*/latest-production")
        manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
        identities.append((manifest["id"], manifest["artifacts"]["qemu.raw"]["sha256"]))
    assert identities[0] == identities[1]


def _fixed_pins(source: object) -> str:
    """A pin for every source without the network: a commit for git, a sha256 for http."""
    return "1" * 40 if hasattr(source, "ref") else "0" * 64
