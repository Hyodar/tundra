from pathlib import Path

import pytest

from tundravm.declarative import (
    Hook,
    Package,
    Recipe,
    Target,
    Variant,
    compile,
    lint,
    lower,
    resolve,
)


def _recipe(*packages: str, targets: tuple[Target, ...] = ("qemu", "azure")) -> Recipe:
    return Recipe(
        "effects",
        items=(*(Package(p) for p in packages), Hook("ready", "prepare", "echo ready")),
        variants=(Variant("default", outputs=targets),),
    )


def test_declarative_values_do_not_touch_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    recipe = _recipe("curl", "jq", targets=("qemu",))
    resolve(recipe, variant="default")
    lint(recipe)
    tree = compile(recipe)
    image = lower(recipe)

    assert image.state.profiles["default"].packages == {"acpid", "curl", "jq", "openssl"}
    assert tree.entries
    assert list(tmp_path.iterdir()) == []
