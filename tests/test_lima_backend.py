from pathlib import Path

import pytest

from tdx.backends.lima import LimaMkosiBackend
from tdx.errors import BackendExecutionError
from tdx.models import BakeRequest


def test_lima_mount_plan_single_mount(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    mounts = backend.mount_plan(request)

    assert len(mounts) == 1
    assert mounts[0].source == request.build_dir
    assert mounts[0].target == "/home/debian/mnt"


def test_lima_mount_plan_is_deterministic(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second


def test_lima_instance_name_is_hash_based(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    name = backend._resolve_instance_name(request)

    assert name.startswith("tdx-builder-")
    assert len(name) == len("tdx-builder-") + 8


def test_lima_instance_name_deterministic(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    first = backend._resolve_instance_name(request)
    second = backend._resolve_instance_name(request)

    assert first == second


def test_lima_instance_name_differs_per_path(tmp_path: Path) -> None:
    r1 = BakeRequest(profile="default", build_dir=tmp_path / "a", emit_dir=tmp_path / "a" / "emit")
    r2 = BakeRequest(profile="default", build_dir=tmp_path / "b", emit_dir=tmp_path / "b" / "emit")
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    assert backend._resolve_instance_name(r1) != backend._resolve_instance_name(r2)


def test_lima_instance_name_override(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB", instance_name="my-vm")

    assert backend._resolve_instance_name(request) == "my-vm"


def test_lima_prepare_fails_with_actionable_hint_when_missing_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
    monkeypatch.setattr("tdx.backends.lima.shutil.which", lambda _: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "limactl" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert "Lima" in excinfo.value.hint


def test_lima_prepare_creates_directories_when_binary_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
    monkeypatch.setattr("tdx.backends.lima.shutil.which", lambda _: "/usr/bin/limactl")
    monkeypatch.setattr(LimaMkosiBackend, "_instance_running", lambda self, inst: True)

    backend.prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()


def test_lima_build_mkosi_command_per_directory(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    cmd = backend._build_mkosi_command(request)

    assert "--force" in cmd
    assert "--image-id=default" in cmd
    assert "--cache-directory=/home/debian/mkosi-cache" in cmd
    assert "--output-dir=/home/debian/mkosi-output" in cmd
    assert "build" in cmd


def test_lima_build_mkosi_command_extra_args(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB", mkosi_args=["--debug"])

    cmd = backend._build_mkosi_command(request)

    assert "--debug" in cmd


def test_lima_resources_are_configurable() -> None:
    backend = LimaMkosiBackend(cpus=4, memory="8GiB", disk="50GiB")

    assert backend.cpus == 4
    assert backend.memory == "8GiB"
    assert backend.disk == "50GiB"


def _request(tmp_path: Path) -> BakeRequest:
    return BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "emit",
    )
