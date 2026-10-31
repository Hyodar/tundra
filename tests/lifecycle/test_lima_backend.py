import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import bake_request
from tundravm.backends.base import StreamResult
from tundravm.backends.lima import VM_MKOSI, LimaMkosiBackend, lima_yaml
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest
from tundravm.toolchain import MKOSI_PIN, MkosiTool, ToolsTree, running_toolchain


def test_lima_mount_plan_single_mount(tmp_path: Path) -> None:
    request = bake_request(tmp_path)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")

    mounts = backend.mount_plan(request)

    assert len(mounts) == 1
    assert mounts[0].source == request.build_dir
    assert mounts[0].target == "/home/debian/mnt"


@pytest.mark.parametrize(
    ("instance_name", "pattern", "per_path"),
    [(None, r"tdx-builder-[0-9a-f]{8}", True), ("my-vm", "my-vm", False)],
    ids=["hash-based", "override"],
)
def test_lima_instance_name(
    tmp_path: Path, instance_name: str | None, pattern: str, per_path: bool
) -> None:
    """``tdx-builder-`` and 8 hex digits of the emit path, stable for one request and
    different per build path; an ``instance_name`` override wins for every path."""
    request = bake_request(tmp_path)
    other = BakeRequest(
        profile="default", build_dir=tmp_path / "b", emit_dir=tmp_path / "b" / "emit"
    )
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB", instance_name=instance_name)

    name = backend._resolve_instance_name(request)

    assert re.fullmatch(pattern, name)
    assert backend._resolve_instance_name(request) == name
    assert (backend._resolve_instance_name(other) != name) == per_path


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


def _bake_tree_request(tmp_path: Path, mkosi_args: tuple[str, ...] = ()) -> BakeRequest:
    """A request laid out like ``bake``: the mkosi tree under the build directory."""
    build = tmp_path / "build"
    (build / "mkosi" / "payload").mkdir(parents=True)
    return BakeRequest(
        profile="default", build_dir=build, emit_dir=build / "mkosi", mkosi_args=mkosi_args
    )


MKOSI_PATHS = [
    "--directory=/home/debian/mnt/mkosi",
    "--force",
    "--cache-directory=/home/debian/mkosi-cache",
    "--output-directory=/home/debian/mkosi-output",
]


@pytest.mark.parametrize(
    ("selection", "extra", "expected"),
    [
        (
            ("--profile=dev", "--dependency=azure"),
            [],
            [*MKOSI_PATHS, "--profile=dev", "--dependency=azure"],
        ),
        ((), ["--debug"], [*MKOSI_PATHS, "--debug"]),
    ],
    ids=["selection", "backend-mkosi-args"],
)
def test_lima_mkosi_args_are_paths_selection_then_extra(
    tmp_path: Path, selection: tuple[str, ...], extra: list[str], expected: list[str]
) -> None:
    """Guest paths, cache and output first, then the request's selection in order, then the
    backend's own ``mkosi_args``; the command runs the pinned mkosi in the state directory."""
    request = _bake_tree_request(tmp_path, selection)
    backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB", mkosi_args=extra)

    args = backend._mkosi_args(request)

    assert args == expected
    command = backend._in_state([*args, "build"])
    assert command.startswith(f"cd /home/debian/mkosi-state && {VM_MKOSI} ")


def test_lima_maps_host_paths_into_the_mount(tmp_path: Path) -> None:
    request = _bake_tree_request(tmp_path)
    backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB")
    output = request.build_dir / "default" / "output"

    assert backend._vm_path(request, output) == "/home/debian/mnt/default/output"
    with pytest.raises(BackendExecutionError) as excinfo:
        backend._vm_path(request, tmp_path / "elsewhere")
    assert excinfo.value.hint is not None


@pytest.mark.parametrize(
    ("cpus", "memory", "disk"),
    [(1, "1GiB", "10GiB"), (4, "8GiB", "50GiB")],
    ids=["small", "large"],
)
def test_lima_guest_is_x86_64_with_the_pinned_mkosi(cpus: int, memory: str, disk: str) -> None:
    """One supported pair: an x86-64 Debian guest (TCG on Apple Silicon), mkosi at the pin;
    the configured resources are the backend's and the guest's."""
    backend = LimaMkosiBackend(cpus=cpus, memory=memory, disk=disk)

    config = lima_yaml(backend, "/build")

    assert (backend.cpus, backend.memory, backend.disk) == (cpus, memory, disk)
    assert f"cpus: {cpus}\n" in config
    assert f'memory: "{memory}"' in config and f'disk: "{disk}"' in config
    assert "vmType: qemu\narch: x86_64\n" in config
    assert "amd64.qcow2" in config and "arm64" not in config
    assert "rosetta" not in config and "nix" not in config
    assert MKOSI_PIN.requirement in config
    assert MKOSI_PIN.commit[:12] in VM_MKOSI
    assert backend.mkosi_tool == MkosiTool(MKOSI_PIN.version, MKOSI_PIN.commit)
    assert backend.fingerprint()["mkosi"] == backend.mkosi_tool.label()


_TOOLS = {"Distribution": "debian", "Release": "trixie", "Snapshot": "20251020T000000Z"}


def _fake_vm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str = f"mkosi {MKOSI_PIN.version}",
    summary: str | None = None,
) -> tuple[LimaMkosiBackend, BakeRequest, list[str]]:
    """A Lima backend whose VM commands are recorded and answered."""
    ran: list[str] = []
    backend = LimaMkosiBackend(cpus=2, memory="4GiB", disk="20GiB", instance_name="t")
    request = _bake_tree_request(tmp_path)
    resolved = json.dumps({"Tools": _TOOLS, "Images": [{"Image": "main"}]})

    def lima_exec(
        self: LimaMkosiBackend, instance: str, cmd: str
    ) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        if cmd.endswith("--version"):
            return subprocess.CompletedProcess(["ssh"], 0, version + "\n", "")
        if cmd.endswith("summary --json"):
            out = resolved if summary is None else summary
            return subprocess.CompletedProcess(["ssh"], 0, out, "mkosi: bad tree")
        return subprocess.CompletedProcess(["ssh"], 0, "", "")

    def streaming(argv: Sequence[str], **_: object) -> StreamResult:
        ran.append(argv[-1])
        return StreamResult(0, ())

    monkeypatch.setattr("tundravm.backends.lima.shutil.which", lambda _: "/usr/bin/limactl")
    monkeypatch.setattr("tundravm.backends.lima.run_streaming", streaming)
    monkeypatch.setattr(LimaMkosiBackend, "_lima_exec", lima_exec)
    return backend, request, ran


def test_lima_installs_checks_summarizes_then_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, request, ran = _fake_vm(tmp_path, monkeypatch)

    backend.execute(request)

    install, version, summary, build, move = ran
    assert f"test -x {VM_MKOSI}" in install
    assert version == f"{VM_MKOSI} --version"
    assert summary.endswith("--no-pager summary --json")
    assert build.startswith(f"cd /home/debian/mkosi-state && {VM_MKOSI} ")
    assert build.endswith(" build")
    assert "mv /home/debian/mkosi-output/*" in move
    assert backend.tools_tree == ToolsTree("debian", "trixie", "20251020T000000Z")
    saved = json.loads((request.build_dir / "default" / "mkosi-summary.json").read_text())
    assert saved["Tools"] == _TOOLS
    found = running_toolchain(backend)
    assert found.mkosi == MkosiTool(MKOSI_PIN.version, MKOSI_PIN.commit)
    assert found.tools_tree == backend.tools_tree


def test_lima_refuses_a_guest_running_another_mkosi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, request, ran = _fake_vm(tmp_path, monkeypatch, version="mkosi 26")

    with pytest.raises(BackendExecutionError, match=r"is not mkosi 27\.1: mkosi 26") as excinfo:
        backend.execute(request)
    assert excinfo.value.hint is not None and "reinstall" in excinfo.value.hint
    assert not any(cmd.endswith(" build") for cmd in ran)


def test_lima_reports_a_summary_that_is_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, request, _ = _fake_vm(tmp_path, monkeypatch, summary="")

    with pytest.raises(BackendExecutionError, match="summary --json failed.*bad tree"):
        backend.execute(request)
