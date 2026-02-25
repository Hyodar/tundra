from pathlib import Path

import pytest

from tdx.backends.nix import NixMkosiBackend
from tdx.errors import BackendExecutionError
from tdx.models import BakeRequest


def test_nix_backend_mount_plan_is_deterministic(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend()

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second


def test_nix_backend_fails_on_non_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tdx.backends.nix.sys.platform", "darwin")

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "Linux" in str(excinfo.value)


def test_nix_backend_fails_when_nix_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tdx.backends.nix.shutil.which", lambda _: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "nix" in str(excinfo.value).lower()


def test_nix_backend_prepare_creates_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tdx.backends.nix.shutil.which", lambda _: "/usr/bin/nix")

    backend.prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()


def test_nix_backend_prepare_writes_flake_nix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tdx.backends.nix.shutil.which", lambda _: "/usr/bin/nix")

    backend.prepare(request)

    flake = request.emit_dir / "flake.nix"
    assert flake.exists()
    assert "mkosi" in flake.read_text()


def test_nix_backend_detects_nix_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IN_NIX_SHELL", "1")
    assert NixMkosiBackend._in_nix_shell() is True


def test_nix_backend_detects_nix_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IN_NIX_SHELL", raising=False)
    monkeypatch.setenv("NIX_STORE", "/nix/store")
    assert NixMkosiBackend._in_nix_shell() is True


def test_nix_backend_not_in_nix_shell_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IN_NIX_SHELL", raising=False)
    monkeypatch.delenv("NIX_STORE", raising=False)
    assert NixMkosiBackend._in_nix_shell() is False


def test_nix_backend_build_mkosi_args_per_directory(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend()
    output_dir = tmp_path / "output"

    args = backend._build_mkosi_args(request, output_dir)

    assert args[0] == "mkosi"
    assert "--force" in args
    assert f"--image-id={request.profile}" in args
    assert f"--output-dir={output_dir}" in args
    assert args[-1] == "build"


def test_nix_backend_build_mkosi_args_extra(tmp_path: Path) -> None:
    request = _request(tmp_path)
    backend = NixMkosiBackend(mkosi_args=["--debug"])
    output_dir = tmp_path / "output"

    args = backend._build_mkosi_args(request, output_dir)

    assert "--debug" in args


def _request(tmp_path: Path) -> BakeRequest:
    return BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "emit",
    )
