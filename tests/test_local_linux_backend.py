import sys
from pathlib import Path

import pytest

from tdx.backends.local_linux import LocalLinuxBackend
from tdx.errors import BackendExecutionError
from tdx.models import BakeRequest


def test_local_backend_mount_plan_is_deterministic(tmp_path: Path) -> None:
    backend = LocalLinuxBackend()
    request = _request(tmp_path)

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second
    assert tuple(mount.target for mount in first) == (
        str(request.build_dir),
        str(request.emit_dir),
    )


def test_local_backend_fails_on_non_linux_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalLinuxBackend()
    request = _request(tmp_path)
    monkeypatch.setattr("tdx.backends.local_linux.sys.platform", "darwin")
    monkeypatch.setattr("tdx.backends.local_linux.shutil.which", lambda _: "/usr/bin/mkosi")

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "Linux host" in str(excinfo.value)
    assert excinfo.value.code == "E_BACKEND_EXECUTION"


def test_local_backend_fails_when_mkosi_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalLinuxBackend()
    request = _request(tmp_path)
    monkeypatch.setattr("tdx.backends.local_linux.sys.platform", "linux")
    monkeypatch.setattr("tdx.backends.local_linux.shutil.which", lambda _: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "mkosi" in str(excinfo.value)
    assert excinfo.value.hint is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Local backend is Linux-specific.")
def test_local_backend_prepare_creates_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalLinuxBackend()
    request = _request(tmp_path)
    monkeypatch.setattr("tdx.backends.local_linux.shutil.which", lambda _: "/usr/bin/mkosi")

    backend.prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()


def _request(tmp_path: Path) -> BakeRequest:
    return BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "emit",
    )
