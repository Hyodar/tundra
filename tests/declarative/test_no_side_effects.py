from pathlib import Path

import pytest

from tundravm.declarative import (
    Backend,
    Fragment,
    Hook,
    Package,
    Recipe,
    Target,
    Variant,
    bake,
    compile,
    lint,
    lock,
    lower,
    resolve,
    write_lock,
)


def _recipe(*packages: str, targets: tuple[Target, ...] = ("qemu", "azure")) -> Recipe:
    return Recipe(
        "effects",
        Fragment(
            "common",
            items=(*(Package(p) for p in packages), Hook("ready", "prepare", "echo ready")),
        ),
        variants=(Variant("default", targets=targets),),
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

    assert image.state.profiles["default"].packages == {"curl", "jq"}
    assert tree.entries
    assert list(tmp_path.iterdir()) == []


def test_explicit_output_operations_create_files(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    emit_dir = tmp_path / "mkosi"
    recipe = _recipe("curl")

    locked = lock(recipe)
    lock_path = build_dir / "tundravm.lock"
    assert not lock_path.exists()
    write_lock(locked, lock_path)
    assert lock_path.exists()

    compile(recipe).write(emit_dir)
    assert (emit_dir / "default" / "mkosi.conf").exists()

    artifacts = bake(recipe, locked=locked, backend=Backend("inprocess"), out=build_dir)
    assert {(a.variant, a.target) for a in artifacts} == {
        ("default", "qemu"),
        ("default", "azure"),
    }
    assert (build_dir / "default" / "disk.qcow2").exists()
    assert (build_dir / "default" / "disk.vhd").exists()
