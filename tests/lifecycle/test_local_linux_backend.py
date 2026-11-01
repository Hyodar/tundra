"""The Linux backend: every mkosi verb through tundravm._mkosi (faked at that boundary)."""

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import bake_request
from tundravm import _mkosi
from tundravm.backends.base import StreamResult
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.compiler.emit_mkosi import esp_bytes, uki_capacity
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, OutputTarget
from tundravm.testing import FakeMkosi
from tundravm.toolchain import ToolsTree, running_toolchain

pytestmark = pytest.mark.boundary

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the Linux backend and mkosi are Linux-only"
)


def _request(
    tmp_path: Path, *targets: OutputTarget, mkosi_args: tuple[str, ...] = ()
) -> BakeRequest:
    """A request laid out like ``bake``: the mkosi tree under the build directory."""
    build = tmp_path / "build"
    (build / "mkosi" / "payload").mkdir(parents=True)
    (build / "mkosi" / "mkosi.conf").write_text("[Output]\nFormat=disk\nOutput=qemu\n")
    return BakeRequest(
        profile="default",
        build_dir=build,
        emit_dir=build / "mkosi",
        output_targets=targets or ("qemu",),
        mkosi_args=mkosi_args,
    )


def test_local_backend_mount_plan_is_deterministic(tmp_path: Path) -> None:
    backend = LocalLinuxBackend()
    request = bake_request(tmp_path)

    first = backend.mount_plan(request)
    second = backend.mount_plan(request)

    assert first == second
    assert tuple(mount.target for mount in first) == (
        str(request.build_dir),
        str(request.emit_dir),
    )


@pytest.mark.parametrize(("platform", "machine"), [("darwin", "arm64"), ("linux", "aarch64")])
def test_local_backend_refuses_hosts_other_than_x86_64_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, machine: str
) -> None:
    monkeypatch.setattr("tundravm.backends.local_linux.sys.platform", platform)
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: machine)

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend().prepare(bake_request(tmp_path))

    assert "x86-64 Linux host" in str(excinfo.value)
    assert excinfo.value.hint is not None and "Lima" in excinfo.value.hint
    assert excinfo.value.code == "E_BACKEND_EXECUTION"


@linux_only
def test_local_backend_prepare_creates_directories(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    request = bake_request(tmp_path)

    LocalLinuxBackend().prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()
    assert fake_mkosi.calls == []


def test_local_arguments_are_absolute_paths_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree carries every mkosi setting: the backend passes paths and the selection."""
    monkeypatch.chdir(tmp_path)
    request = BakeRequest(
        profile="default", build_dir=Path("build"), emit_dir=Path("build") / "mkosi"
    )
    (tmp_path / "build" / "mkosi").mkdir(parents=True)

    argv = LocalLinuxBackend().arguments(request)

    build = tmp_path / "build"
    assert argv == [
        f"--directory={build / 'mkosi'}",
        "--force",
        f"--output-directory={build / 'default' / 'output'}",
        f"--workspace-directory={build / '.mkosi' / 'workspace'}",
        f"--cache-directory={build / '.mkosi' / 'cache'}",
    ]


@pytest.mark.parametrize(
    ("cache_key", "incremental"),
    [(None, False), ("dev-0123456789abcdef", True)],
    ids=["production", "development"],
)
def test_only_a_keyed_request_builds_incrementally_and_from_its_own_cache_directory(
    tmp_path: Path, cache_key: str | None, incremental: bool
) -> None:
    """A production request names no key: mkosi's cache directory is ``.mkosi/cache``
    (repository metadata) and ``Incremental=`` stays off; a development request builds
    with ``--incremental=yes`` under ``.mkosi/incremental`` and mkosi's cache key suffixed
    with tundravm's."""
    request = replace(_request(tmp_path), cache_key=cache_key)

    argv = LocalLinuxBackend().arguments(request)

    state = tmp_path / "build" / ".mkosi"
    assert f"--cache-directory={state / ('incremental' if incremental else 'cache')}" in argv
    assert ("--incremental=yes" in argv) is incremental
    assert (f"--cache-key=&d~&r~&a~&I~{cache_key}" in argv) is incremental
    assert not any(arg.startswith("--cache-key") for arg in argv) or incremental


@linux_only
def test_a_keyed_build_records_whether_the_cache_was_stored_or_reused(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first build under a key stores every image's base tree (mkosi writes the cache
    manifests), the next reuses them (the manifests stay as they were); a request without
    a key records nothing."""
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    keyed = replace(_request(tmp_path), cache_key="dev-0123456789abcdef")
    backend = LocalLinuxBackend()

    first = backend.execute(keyed).profiles["default"].cache
    second = backend.execute(keyed).profiles["default"].cache
    plain = backend.execute(replace(keyed, cache_key=None)).profiles["default"].cache

    directory = tmp_path / "build" / ".mkosi" / "incremental"
    assert first is not None and second is not None and plain is None
    assert (first.directory, first.key) == (directory, "dev-0123456789abcdef")
    assert first.images == {"payload": "saved", "main": "saved"}
    assert second.images == {"payload": "hit", "main": "hit"}
    assert sorted(path.name for path in directory.iterdir()) == [
        "debian~trixie~x86-64~main~dev-0123456789abcdef.cache",
        "debian~trixie~x86-64~main~dev-0123456789abcdef.manifest",
        "debian~trixie~x86-64~payload~dev-0123456789abcdef.cache",
        "debian~trixie~x86-64~payload~dev-0123456789abcdef.manifest",
    ]


def test_local_arguments_pass_the_selection_then_the_backends_args(tmp_path: Path) -> None:
    request = _request(
        tmp_path, "azure", "gcp", mkosi_args=("--dependency=azure", "--dependency=gcp")
    )
    backend = LocalLinuxBackend(mkosi_args=["--debug"])

    argv = backend.arguments(request)

    assert argv[0] == f"--directory={tmp_path / 'build' / 'mkosi'}"
    assert argv[-3:] == ["--dependency=azure", "--dependency=gcp", "--debug"]
    for setting in ("--image-id", "--tools-tree", "--with-network", "--profile"):
        assert not any(arg.startswith(setting) for arg in argv), setting


@linux_only
def test_local_execute_summarizes_then_builds_through_the_adapter(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    request = _request(tmp_path)
    lines: list[str] = []
    request = BakeRequest(
        profile="default",
        build_dir=request.build_dir,
        emit_dir=request.emit_dir,
        on_output=lines.append,
    )
    backend = LocalLinuxBackend()

    result = backend.execute(request)

    build = tmp_path / "build"
    summary, build_call = fake_mkosi.calls
    assert fake_mkosi.verbs() == ["summary", "build"]
    assert summary.argv[-3:] == ("--no-pager", "summary", "--json") and summary.stdout
    assert build_call.argv == (*backend.arguments(request), "build")
    assert {summary.cwd, build_call.cwd} == {build / ".mkosi"}
    assert build_call.log == build / "default" / "mkosi.log"
    assert "fake mkosi build" in lines
    artifact = result.profiles["default"].artifacts["qemu"]
    assert artifact.path == build / "default" / "output" / "qemu.raw"
    assert artifact.boot_payload == build / "default" / "output" / "payload.efi"
    resolved = json.loads((build / "default" / "mkosi-summary.json").read_text())
    assert resolved["Tools"]["Release"] == "trixie"
    assert backend.tools_tree == ToolsTree("debian", "trixie", "20251020T000000Z")
    toolchain = running_toolchain(backend)
    assert toolchain.tools_tree == backend.tools_tree
    assert toolchain.mkosi is not None and toolchain.mkosi.version == _mkosi.PINNED_VERSION


@linux_only
def test_local_execute_reports_mkosis_failure_with_the_log_tail(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    fake_mkosi.fail = 1
    fake_mkosi.log = "‣ Installing packages\napt: E: Unable to locate package nope"

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend().execute(_request(tmp_path))

    message = str(excinfo.value)
    assert "mkosi build failed (exit 1)" in message
    assert "| apt: E: Unable to locate package nope" in message
    assert excinfo.value.context["returncode"] == "1"
    assert excinfo.value.hint is not None and "mkosi.log" in excinfo.value.hint


@linux_only
@pytest.mark.parametrize(
    ("extra", "said"),
    [(0, "mkosi build failed (exit 1)."), (1 << 20, "The UKI is 268.8 MiB: the 272.0 MiB ESP")],
    ids=["fits", "past-the-esp"],
)
def test_a_failed_build_with_a_uki_past_the_esp_names_its_size_and_the_fix(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch, extra: int, said: str
) -> None:
    """mkosi built the payload, then failed the disk: a UKI past what the ESP holds is the
    failure, told with both sizes and the ceiling to raise; one that fits leaves mkosi's
    failure as it is."""
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    fake_mkosi.fail = 1
    fake_mkosi.uki_size = uki_capacity(esp_bytes("initramfs")) + extra

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend().execute(_request(tmp_path))

    assert str(excinfo.value).startswith(said)
    assert str(excinfo.value.hint).startswith(
        "Raise the UKI's ceiling with RootFS.initramfs(uki_max_size="
    ) == bool(extra)


@linux_only
def test_local_execute_uses_sudo_without_user_namespaces_and_reclaims_the_output(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build re-executes this interpreter under sudo; the summary stays unprivileged."""
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: "/usr/bin/sudo")
    monkeypatch.setattr("tundravm.backends.local_linux.os.getuid", lambda: 1000)
    monkeypatch.setattr("tundravm.backends.local_linux.os.getgid", lambda: 1001)
    fake_mkosi.sudo = True
    request = _request(tmp_path)
    streamed: list[list[str]] = []
    chowned: list[list[str]] = []

    def run_streaming(argv: Sequence[str], **_: object) -> StreamResult:
        """Stand in for sudo: run the module entry in-process, as root would."""
        streamed.append(list(argv))
        assert list(argv[:5]) == ["sudo", "-E", sys.executable, "-m", "tundravm._mkosi"]
        code = _mkosi.main(list(argv[5:]))
        return StreamResult(code, ())

    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        chowned.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr("tundravm.backends.local_linux.run_streaming", run_streaming)
    monkeypatch.setattr("tundravm.backends.local_linux.subprocess.run", run)
    notices: list[tuple[str, str]] = []
    request = BakeRequest(
        profile="default",
        build_dir=request.build_dir,
        emit_dir=request.emit_dir,
        on_notice=lambda level, text: notices.append((level, text)),
    )

    result = LocalLinuxBackend().execute(request)

    build = tmp_path / "build"
    (command,) = streamed
    separator = command.index("--")
    assert command[5:separator] == [
        "--cwd",
        str(build / ".mkosi"),
        "--log",
        str(build / "default" / "mkosi.log"),
        "--result",
        command[10],
    ]
    assert tuple(command[separator + 1 :]) == fake_mkosi.calls[-1].argv
    assert fake_mkosi.verbs() == ["summary", "build"]
    assert result.profiles["default"].artifacts["qemu"].path.name == "qemu.raw"
    owned = [
        build / "default" / "output",
        build / ".mkosi" / "workspace",
        build / ".mkosi" / "cache",
        build / "default" / "mkosi.log",
    ]
    assert chowned == [["sudo", "chown", "-R", "1000:1001", *map(str, owned)]]
    assert any("sudo" in text for _, text in notices)


@linux_only
def test_local_execute_fails_when_sudo_runs_nothing(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: "/usr/bin/sudo")
    fake_mkosi.sudo = True

    def refused(argv: Sequence[str], **_: object) -> StreamResult:
        return StreamResult(1, ("sudo: a password is required",))

    monkeypatch.setattr("tundravm.backends.local_linux.run_streaming", refused)
    monkeypatch.setattr(
        "tundravm.backends.local_linux.subprocess.run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend().execute(_request(tmp_path))

    assert "sudo: a password is required" in str(excinfo.value)


@linux_only
def test_local_backend_without_sudo_or_user_namespaces_names_the_fix(
    tmp_path: Path, fake_mkosi: FakeMkosi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tundravm.backends.local_linux.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: None)
    fake_mkosi.sudo = True

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend().prepare(bake_request(tmp_path))

    assert excinfo.value.hint is not None
    assert "apparmor_restrict_unprivileged_userns=0" in excinfo.value.hint


def test_privilege_none_never_uses_sudo(fake_mkosi: FakeMkosi) -> None:
    fake_mkosi.sudo = True

    assert LocalLinuxBackend(privilege="none").uses_sudo() is False
    assert LocalLinuxBackend().uses_sudo() is True


def test_local_requirements_are_what_the_tools_tree_does_not_provide() -> None:
    tools = [requirement.tool for requirement in LocalLinuxBackend().requirements()]

    assert tools == ["apt", "dpkg", "debian-archive-keyring"]


def test_local_fingerprint_names_the_mkosi_it_runs() -> None:
    found = LocalLinuxBackend(mkosi_args=["--debug"]).fingerprint()

    assert found["name"] == "local_linux"
    assert found["privilege"] == "auto"
    assert found["mkosi_args"] == ["--debug"]
    assert str(found["mkosi"]).startswith(_mkosi.PINNED_VERSION)
