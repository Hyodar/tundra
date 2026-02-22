from pathlib import Path

import pytest

from tdx.backends.lima import LimaBackend
from tdx.errors import BackendExecutionError
from tdx.models import BakeRequest


def test_lima_mount_plan_is_deterministic(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = LimaBackend()

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second
    assert tuple(mount.target for mount in first) == ("/mnt/host/build", "/mnt/host/emit")


def test_lima_prepare_fails_with_actionable_hint_when_missing_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = LimaBackend()
    monkeypatch.setattr("tdx.backends.lima.shutil.which", lambda _: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "limactl" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert "Install Lima" in excinfo.value.hint


def test_lima_prepare_and_execute_when_binary_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = LimaBackend()
    monkeypatch.setattr("tdx.backends.lima.shutil.which", lambda _: "/usr/bin/limactl")

    backend.prepare(request)
    result = backend.execute(request)
    backend.cleanup(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()
    assert "default" in result.profiles


def _request(tmp_path: Path) -> BakeRequest:
    return BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "emit",
    )
