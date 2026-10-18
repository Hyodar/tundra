import io
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import bake_request
from tundravm.backends.base import StreamResult
from tundravm.backends.lima import LimaMkosiBackend, lima_yaml
from tundravm.cli import EXIT_OK, doctor
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest
from tundravm.toolchain import MkosiTool, NixpkgsPin, Toolchain, running_toolchain


def test_lima_mount_plan_single_mount(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    mounts = backend.mount_plan(request)

    assert len(mounts) == 1
    assert mounts[0].source == request.build_dir
    assert mounts[0].target == "/home/debian/mnt"


def test_lima_mount_plan_is_deterministic(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second


def test_lima_instance_name_is_hash_based(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    name = backend._resolve_instance_name(request)

    assert name.startswith("tdx-builder-")
    assert len(name) == len("tdx-builder-") + 8


def test_lima_instance_name_deterministic(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
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
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB", instance_name="my-vm")

    assert backend._resolve_instance_name(request) == "my-vm"


def test_lima_prepare_fails_with_actionable_hint_when_missing_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
    monkeypatch.setattr("tundravm.backends.lima.shutil.which", lambda _: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "limactl" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert "Lima" in excinfo.value.hint


def test_lima_prepare_creates_directories_when_binary_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
    monkeypatch.setattr("tundravm.backends.lima.shutil.which", lambda _: "/usr/bin/limactl")
    monkeypatch.setattr(LimaMkosiBackend, "_instance_running", lambda self, inst: True)

    backend.prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()


def _bake_tree_request(tmp_path: Path) -> BakeRequest:
    """A request laid out like ``bake``: the mkosi tree under the build directory."""
    build = tmp_path / "build"
    (build / "mkosi" / "default").mkdir(parents=True)
    return BakeRequest(profile="default", build_dir=build, emit_dir=build / "mkosi")


def test_lima_build_mkosi_command_per_directory(tmp_path: Path) -> None:
    request = _bake_tree_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    cmd = backend._build_mkosi_command(request)

    assert "--directory=/home/debian/mnt/mkosi/default" in cmd
    assert "--force" in cmd
    assert "--image-id=default" in cmd
    assert "--cache-directory=/home/debian/mkosi-cache" in cmd
    assert "--output-dir=/home/debian/mkosi-output" in cmd
    assert cmd.endswith(" build")


def test_lima_maps_host_paths_into_the_mount(tmp_path: Path) -> None:
    request = _bake_tree_request(tmp_path)
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB")
    output = request.build_dir / "default" / "output"

    assert backend._vm_path(request, output) == "/home/debian/mnt/default/output"
    with pytest.raises(BackendExecutionError) as excinfo:
        backend._vm_path(request, tmp_path / "elsewhere")
    assert excinfo.value.hint is not None


def test_lima_build_mkosi_command_extra_args(tmp_path: Path) -> None:
    request = _bake_tree_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB", mkosi_args=["--debug"])

    cmd = backend._build_mkosi_command(request)

    assert "--debug" in cmd


def test_lima_resources_are_configurable() -> None:
    backend = LimaMkosiBackend(cpus=4, memory="8GiB", disk="50GiB")

    assert backend.cpus == 4
    assert backend.memory == "8GiB"
    assert backend.disk == "50GiB"


def test_lima_disables_rosetta_by_default() -> None:
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB")

    config = lima_yaml(backend, "/build")

    assert backend.rosetta is False
    assert "rosetta:\n  enabled: false\n  binfmt: false\n" in config
    assert "extra-platforms" not in config.replace("no extra-platforms", "")
    assert backend.fingerprint()["rosetta"] is False


def test_lima_rosetta_is_an_explicit_opt_in(tmp_path: Path) -> None:
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB", rosetta=True)

    config = lima_yaml(backend, "/build")

    assert "rosetta:\n  enabled: true\n  binfmt: true\n" in config
    assert "echo 'extra-platforms = x86_64-linux' >> ~/.config/nix/nix.conf" in config
    assert backend.fingerprint()["rosetta"] is True
    plain = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB")
    request = bake_request(tmp_path)
    rosetta_vm = backend._resolve_instance_name(request)
    assert rosetta_vm == plain._resolve_instance_name(request) + "-rosetta"


def test_doctor_warns_when_lima_rosetta_is_on() -> None:
    out = io.StringIO()
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB", rosetta=True)

    code = doctor(None, out, runner=_limactl, backend=backend)

    assert code == EXIT_OK
    assert (
        "  warning: rosetta is on: builds are reproducible: not guaranteed (rosetta); "
        "use LimaMkosiBackend(rosetta=False)"
    ) in out.getvalue().splitlines()


def test_doctor_is_quiet_about_rosetta_by_default() -> None:
    out = io.StringIO()
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB")

    doctor(None, out, runner=_limactl, backend=backend)

    assert "rosetta" not in out.getvalue()


def _limactl(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if argv[0] == "limactl":
        return subprocess.CompletedProcess(list(argv), 0, stdout="limactl version 1.0.0\n")
    raise FileNotFoundError(argv[0])


_PIN = NixpkgsPin(rev="56c02bc00adcf003215cc4bd996d6efaf4cff188", nar_hash="sha256-x")
_PINNED = Toolchain(
    tundravm="0.1.0", mkosi=MkosiTool("25.3", "nix", _PIN.rev), systemd="257", nixpkgs=_PIN
)
_STORE_MKOSI = "/nix/store/0abc-mkosi/bin/mkosi"


def _fake_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str, returncode: int = 0
) -> tuple[LimaMkosiBackend, BakeRequest, list[str]]:
    """A Lima backend whose VM commands are recorded; the identity probe prints *identity*."""
    ran: list[str] = []
    backend = LimaMkosiBackend(cpus=2, memory="4GiB", disk="20GiB", instance_name="t")
    build = tmp_path / "build"
    request = replace(
        BakeRequest(profile="default", build_dir=build, emit_dir=build / "mkosi"),
        toolchain=_PINNED,
    )

    def lima_exec(
        self: LimaMkosiBackend, instance: str, cmd: str
    ) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        out = identity if "command -v mkosi" in cmd else ""
        code = returncode if "command -v mkosi" in cmd else 0
        return subprocess.CompletedProcess(["ssh"], code, out, "nix: boom")

    def streaming(argv: Sequence[str], **_: object) -> StreamResult:
        ran.append(argv[-1])
        return StreamResult(0, ())

    monkeypatch.setattr("tundravm.backends.lima.shutil.which", lambda _: "/usr/bin/limactl")
    monkeypatch.setattr("tundravm.backends.lima.run_streaming", streaming)
    monkeypatch.setattr(LimaMkosiBackend, "_lima_exec", lima_exec)
    return backend, request, ran


def test_lima_records_the_mkosi_the_pinned_vm_shell_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, request, ran = _fake_vm(tmp_path, monkeypatch, f"{_STORE_MKOSI}\nmkosi 25.3\n")

    backend.execute(request)

    probe = next(cmd for cmd in ran if "command -v mkosi" in cmd)
    assert "/home/debian/.nix-profile/bin/nix develop path:/home/debian/mnt/mkosi -c" in probe
    assert backend.executed_mkosi == MkosiTool("25.3", "nix", _PIN.rev, path=_STORE_MKOSI)
    found = running_toolchain(backend, locked=_PINNED)
    assert found.mkosi == backend.executed_mkosi
    assert found.payload()["mkosi"]["path"] == _STORE_MKOSI


def test_lima_refuses_a_vm_shell_running_another_mkosi_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = "/nix/store/9zzz-mkosi-99/bin/mkosi\nmkosi 99.0\n"
    backend, request, _ = _fake_vm(tmp_path, monkeypatch, identity)

    with pytest.raises(BackendExecutionError, match=r"ran mkosi 99\.0.*pins mkosi 25\.3"):
        backend.execute(request)
    assert backend.executed_mkosi is None


def test_lima_refuses_an_mkosi_outside_the_nix_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, request, _ = _fake_vm(tmp_path, monkeypatch, "/usr/bin/mkosi\nmkosi 25.3\n")

    with pytest.raises(BackendExecutionError, match="outside the Nix store"):
        backend.execute(request)


def test_lima_fails_when_the_vm_shell_cannot_identify_mkosi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, request, _ = _fake_vm(tmp_path, monkeypatch, "", returncode=1)

    with pytest.raises(BackendExecutionError, match="Could not identify.*boom"):
        backend.execute(request)
