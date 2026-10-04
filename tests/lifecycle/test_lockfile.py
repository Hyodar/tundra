import io
from pathlib import Path

import pytest

from tundravm.cli import EXIT_SDK_ERROR, main
from tundravm.declarative import (
    Backend,
    Fragment,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
)
from tundravm.errors import LockfileError
from tundravm.lockfile import (
    LockedFetch,
    build_lockfile,
    parse_lockfile,
    serialize_lockfile,
)

RECIPE_FILE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import Fragment, Package, Recipe

recipe = Recipe(
    "lockfile", Fragment("lockfile", items=(Package("curl"), Package("linux-image-amd64")))
)
backend = InProcessBackend()
"""


def _recipe(*packages: str) -> Recipe:
    return Recipe(
        "lockfile",
        Fragment(
            "lockfile", items=(Package("linux-image-amd64"), *(Package(name) for name in packages))
        ),
        base="debian/bookworm",
    )


def test_lockfile_roundtrip_parser_serializer() -> None:
    lock = build_lockfile(
        recipe={
            "base": "debian/bookworm",
            "profiles": {"default": {"packages": ["curl"]}},
        },
        fetches=[LockedFetch(source="https://example.invalid/a", kind="http", digest="abc")],
    )
    encoded = serialize_lockfile(lock)
    decoded = parse_lockfile(encoded)

    assert decoded == lock


def test_lock_records_dependency_and_recipe_metadata() -> None:
    recipe = Recipe(
        "lockfile",
        Fragment("lockfile", items=(Package("curl"),)),
        base="debian/bookworm",
        variants=(
            Variant("default", target="qemu"),
            Variant("dev", add=Fragment("dev", items=(Package("jq"),))),
        ),
    )

    lockfile = lock(recipe).lockfile

    assert lockfile.version == 3
    assert lockfile.recipe["base"] == "debian/bookworm"
    assert lockfile.dependencies["default"] == ["curl"]
    assert lockfile.dependencies["dev"] == ["curl", "jq"]
    assert lockfile.recipe_digest


def test_bake_frozen_fails_when_lock_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = tmp_path / "recipe.py"
    recipe.write_text(RECIPE_FILE, encoding="utf-8")
    missing = tmp_path / "missing.lock"

    code = main(
        ["bake", str(recipe), "--lockfile", str(missing), "--out", str(tmp_path / "out")],
        stdout=io.StringIO(),
    )

    assert code == EXIT_SDK_ERROR
    assert "error [E_LOCKFILE]" in capsys.readouterr().err
    assert not (tmp_path / "out" / "bake-result.json").exists()


def test_bake_frozen_fails_when_lock_is_stale(tmp_path: Path) -> None:
    locked = lock(_recipe("curl"))

    with pytest.raises(LockfileError) as excinfo:
        bake(_recipe("curl", "jq"), locked=locked, backend=Backend("inprocess"), out=tmp_path)

    assert "stale" in str(excinfo.value).lower()


def test_bake_frozen_succeeds_with_current_lock(tmp_path: Path) -> None:
    recipe = _recipe("curl")

    artifacts = bake(recipe, locked=lock(recipe), backend=Backend("inprocess"), out=tmp_path)

    assert [(a.variant, a.target) for a in artifacts] == [("default", "qemu")]
    assert artifacts[0].path.is_file()
