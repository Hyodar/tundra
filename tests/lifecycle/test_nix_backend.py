import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import bake_request
from tundravm.backends.base import write_flake_nix
from tundravm.backends.lima import LimaMkosiBackend
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.backends.nix import NixMkosiBackend
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest
from tundravm.toolchain import MkosiTool, NixpkgsPin, Toolchain


def test_nix_backend_mount_plan_is_deterministic(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
    backend = NixMkosiBackend()

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second


def test_nix_backend_fails_on_non_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = bake_request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tundravm.backends.nix.sys.platform", "darwin")

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "Linux" in str(excinfo.value)


def test_nix_backend_fails_when_nix_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = bake_request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tundravm.backends.nix.shutil.which", lambda _: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "nix" in str(excinfo.value).lower()


def test_nix_backend_prepare_creates_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = bake_request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tundravm.backends.nix.shutil.which", lambda _: "/usr/bin/nix")

    backend.prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()


def test_nix_backend_prepare_writes_flake_nix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = bake_request(tmp_path)
    backend = NixMkosiBackend()
    monkeypatch.setattr("tundravm.backends.nix.shutil.which", lambda _: "/usr/bin/nix")

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
    request = bake_request(tmp_path)
    backend = NixMkosiBackend()
    output_dir = tmp_path / "output"

    args = backend._build_mkosi_args(request, output_dir)

    assert args[0] == "mkosi"
    assert "--force" in args
    assert f"--image-id={request.profile}" in args
    assert f"--output-dir={output_dir}" in args
    assert args[-1] == "build"


def test_nix_backend_build_mkosi_args_extra(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
    backend = NixMkosiBackend(mkosi_args=["--debug"])
    output_dir = tmp_path / "output"

    args = backend._build_mkosi_args(request, output_dir)

    assert "--debug" in args


def test_nix_backend_passes_absolute_paths_for_relative_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "build" / "mkosi" / "default").mkdir(parents=True)
    request = BakeRequest(profile="default", build_dir=Path("build"), emit_dir=Path("build/mkosi"))
    backend = NixMkosiBackend()

    args = backend._build_mkosi_args(request, Path("build/default/output"))

    assert f"--directory={tmp_path / 'build' / 'mkosi' / 'default'}" in args
    assert f"--output-dir={tmp_path / 'build' / 'default' / 'output'}" in args
    assert backend._resolve_mkosi_dir(request) == tmp_path / "build" / "mkosi" / "default"


_PIN = NixpkgsPin(
    rev="56c02bc00adcf003215cc4bd996d6efaf4cff188",
    nar_hash="sha256-9i/VTdusq/+NM/tz+J1Re+ojkMB8MBf0QshnYfzHz30=",
    last_modified=1787498568,
)
_PINNED = Toolchain(
    tundravm="0.1.0", mkosi=MkosiTool("25.3", "nix", _PIN.rev), systemd="257", nixpkgs=_PIN
)


def test_flake_pins_nixpkgs_to_the_locked_rev_with_a_flake_lock(tmp_path: Path) -> None:
    write_flake_nix(tmp_path, _PINNED)

    flake = (tmp_path / "flake.nix").read_text()
    assert f'inputs.nixpkgs.url = "github:NixOS/nixpkgs/{_PIN.rev}";' in flake
    assert "nixos-unstable" not in flake
    locked = json.loads((tmp_path / "flake.lock").read_text())
    node = locked["nodes"]["nixpkgs"]
    assert locked["version"] == 7
    assert node["locked"]["rev"] == _PIN.rev
    assert node["locked"]["narHash"] == _PIN.nar_hash
    assert node["locked"]["lastModified"] == _PIN.last_modified
    assert node["original"] == {
        "owner": "NixOS",
        "repo": "nixpkgs",
        "rev": _PIN.rev,
        "type": "github",
    }


def test_flake_guards_the_locked_mkosi_version(tmp_path: Path) -> None:
    write_flake_nix(tmp_path, _PINNED)

    flake = (tmp_path / "flake.nix").read_text()
    assert 'if pkgs.mkosi.version != "25.3"' in flake
    assert "tundravm.lock pins mkosi 25.3, but nixpkgs 56c02bc00adc provides mkosi" in flake


def test_flake_runs_mkosi_with_a_clean_path(tmp_path: Path) -> None:
    write_flake_nix(tmp_path, _PINNED)

    flake = (tmp_path / "flake.nix").read_text()
    assert 'env PATH="${toolsEnv}/bin"' in flake
    assert 'PATH="$PATH"' not in flake
    assert 'name = "mkosi-tools";' in flake
    assert "exec ${pkgs.util-linux}/bin/unshare" in flake


def test_unpinned_flake_follows_unstable_and_drops_a_stale_lock(tmp_path: Path) -> None:
    write_flake_nix(tmp_path, _PINNED)
    write_flake_nix(tmp_path, Toolchain(tundravm="0.1.0"))

    flake = (tmp_path / "flake.nix").read_text()
    assert 'inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";' in flake
    assert "pkgs.mkosi.version !=" not in flake
    assert not (tmp_path / "flake.lock").exists()


def test_nix_backend_prepare_pins_the_request_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = replace(bake_request(tmp_path), toolchain=_PINNED)
    monkeypatch.setattr("tundravm.backends.nix.shutil.which", lambda _: "/usr/bin/nix")

    NixMkosiBackend().prepare(request)

    assert _PIN.rev in (request.emit_dir / "flake.nix").read_text()
    assert (request.emit_dir / "flake.lock").is_file()


def test_backends_declare_their_toolchain_source() -> None:
    assert NixMkosiBackend.toolchain_source == "nix"
    assert LimaMkosiBackend.toolchain_source == "nix"
    assert LocalLinuxBackend.toolchain_source == "host"
