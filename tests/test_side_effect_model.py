from pathlib import Path

from tdx import Image


def test_declarative_methods_do_not_touch_filesystem(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    image = Image(build_dir=build_dir)

    image.install("curl", "jq")
    image.output_targets("qemu")
    image.run("echo", "hello", phase="prepare")

    assert image.state.profiles["default"].packages == {"curl", "jq"}
    assert not build_dir.exists()


def test_explicit_output_operations_create_files(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    emit_dir = tmp_path / "mkosi"
    image = Image(build_dir=build_dir, backend="inprocess")
    image.install("curl")
    image.output_targets("qemu", "azure")
    image.run("echo", "ready", phase="prepare")

    lock_path = image.lock()
    assert lock_path == build_dir / "tdx.lock"
    assert lock_path.exists()

    generated = image.compile(emit_dir)
    assert generated == emit_dir
    assert (emit_dir / "default" / "mkosi.conf").exists()

    result = image.bake()
    assert result.artifact_for(profile="default", target="qemu") is not None
    assert result.artifact_for(profile="default", target="azure") is not None
    assert (build_dir / "default" / "disk.qcow2").exists()
    assert (build_dir / "default" / "disk.vhd").exists()
