import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import bake_request
from tundravm.backends.base import StreamResult
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, OutputTarget


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


def test_local_backend_fails_on_non_linux_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalLinuxBackend()
    request = bake_request(tmp_path)
    monkeypatch.setattr("tundravm.backends.local_linux.sys.platform", "darwin")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: "/usr/bin/mkosi")

    with pytest.raises(BackendExecutionError) as excinfo:
        backend.prepare(request)

    assert "Linux host" in str(excinfo.value)
    assert excinfo.value.code == "E_BACKEND_EXECUTION"


def test_local_backend_fails_when_mkosi_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalLinuxBackend()
    request = bake_request(tmp_path)
    monkeypatch.setattr("tundravm.backends.local_linux.sys.platform", "linux")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: None)

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
    request = bake_request(tmp_path)
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: "/usr/bin/mkosi")
    # Patch version check to avoid hitting real mkosi binary
    monkeypatch.setattr(
        "tundravm.backends.local_linux.subprocess.run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "mkosi 26.0", "stderr": ""})(),
    )

    backend.prepare(request)

    assert request.build_dir.exists()
    assert request.emit_dir.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Local backend is Linux-specific.")
def test_local_backend_mkosi_version_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that _check_mkosi_version rejects old mkosi versions."""
    backend = LocalLinuxBackend()
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: "/usr/bin/mkosi")

    # Mock subprocess.run to return an old version
    class FakeResult:
        returncode = 0
        stdout = "mkosi 20.2"
        stderr = ""

    monkeypatch.setattr(
        "tundravm.backends.local_linux.subprocess.run",
        lambda *a, **kw: FakeResult(),
    )

    with pytest.raises(BackendExecutionError) as excinfo:
        backend._check_mkosi_version()

    assert "below minimum" in str(excinfo.value)
    assert excinfo.value.context["version"] == "mkosi 20.2"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Local backend is Linux-specific.")
def test_local_backend_mkosi_version_check_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that _check_mkosi_version accepts valid mkosi versions."""
    backend = LocalLinuxBackend()
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: "/usr/bin/mkosi")

    class FakeResult:
        returncode = 0
        stdout = "mkosi 26.1"
        stderr = ""

    monkeypatch.setattr(
        "tundravm.backends.local_linux.subprocess.run",
        lambda *a, **kw: FakeResult(),
    )

    # Should not raise
    backend._check_mkosi_version()


# ── command line, tools tree, ownership ──────────────────────────────────


def _relative_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conf: str = "[Output]\nFormat=uki\n"
) -> BakeRequest:
    """A request with *relative* build/emit dirs, as the CLI's ``--out build`` gives."""
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "build" / "mkosi" / "default"
    project.mkdir(parents=True)
    (project / "mkosi.conf").write_text(f"[Distribution]\nDistribution=debian\n{conf}")
    monkeypatch.setattr(
        "tundravm.backends.local_linux.shutil.which", lambda tool: f"/usr/bin/{tool}"
    )
    return BakeRequest(profile="default", build_dir=Path("build"), emit_dir=Path("build/mkosi"))


def _flags(cmd: list[str]) -> dict[str, str]:
    return dict(arg[2:].split("=", 1) for arg in cmd if arg.startswith("--") and "=" in arg)


def test_local_command_passes_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relative_request(tmp_path, monkeypatch)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: True)
    monkeypatch.setattr("tundravm.backends.local_linux.os.getuid", lambda: 1000)

    cmd = LocalLinuxBackend().command(request)

    build = tmp_path / "build"
    assert cmd[:2] == ["sudo", "/usr/bin/mkosi"]
    assert cmd[-1] == "build"
    flags = _flags(cmd)
    assert flags["directory"] == str(build / "mkosi" / "default")
    assert flags["output-dir"] == str(build / "default" / "output")
    assert flags["workspace-directory"] == str(build / ".mkosi" / "workspace")
    assert flags["cache-directory"] == str(build / ".mkosi" / "cache")
    assert all(Path(flags[k]).is_absolute() for k in flags if k not in ("image-id",))
    assert "tools-tree" not in flags


def test_local_command_adds_a_tools_tree_without_ukify_and_keeps_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notices: list[tuple[str, str]] = []
    request = replace(
        _relative_request(tmp_path, monkeypatch),
        on_notice=lambda level, message: notices.append((level, message)),
    )
    conf = tmp_path / "build" / "mkosi" / "default" / "mkosi.conf"
    before = conf.read_bytes()
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)

    cmd = LocalLinuxBackend(privilege="none").command(request)

    assert "--tools-tree=default" in cmd
    assert [level for level, _ in notices] == ["info"]
    assert "ukify not found" in notices[0][1] and "--tools-tree=default" in notices[0][1]
    assert conf.read_bytes() == before
    assert sorted(p.name for p in conf.parent.iterdir()) == ["mkosi.conf"]


@pytest.mark.parametrize(
    ("conf", "mkosi_args"),
    [
        ("[Output]\nFormat=disk\n[Content]\nBootable=no\n", []),
        ("[Output]\nFormat=disk\n", []),
        ("[Output]\nFormat=uki\n", ["--format=directory", "--bootable=no"]),
        ("[Output]\nFormat=uki\n", ["--format", "disk", "--bootable=no"]),
    ],
)
def test_local_command_keeps_host_tools_for_builds_without_a_uki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conf: str, mkosi_args: list[str]
) -> None:
    request = _relative_request(tmp_path, monkeypatch, conf)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)

    cmd = LocalLinuxBackend(privilege="none", mkosi_args=mkosi_args).command(request)

    assert not any(arg.startswith("--tools-tree") for arg in cmd)


@pytest.mark.parametrize(
    "conf", ["[Output]\nFormat=uki\n", "[Output]\nFormat=disk\n[Content]\nBootable=yes\n"]
)
def test_local_command_adds_a_tools_tree_for_uki_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conf: str
) -> None:
    request = _relative_request(tmp_path, monkeypatch, conf)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)

    cmd = LocalLinuxBackend(privilege="none").command(request)

    assert "--tools-tree=default" in cmd


def test_local_command_respects_the_recipes_mkosi_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = "[Build]\nToolsTree=/opt/tools\nCacheDirectory=/var/cache/mine\n"
    request = _relative_request(tmp_path, monkeypatch, conf)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)

    flags = _flags(LocalLinuxBackend(privilege="none").command(request))

    assert "tools-tree" not in flags and "cache-directory" not in flags
    assert "workspace-directory" in flags


def test_local_command_reuses_a_stashed_default_tools_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relative_request(tmp_path, monkeypatch, "[Build]\nToolsTree=default\n")
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: True)
    backend = LocalLinuxBackend(privilege="none")
    assert _flags(backend.command(request))["tools-tree"] == "default"

    cached = tmp_path / "build" / ".mkosi" / "mkosi.tools"
    cached.mkdir(parents=True)
    assert _flags(backend.command(request))["tools-tree"] == str(cached)


def test_local_execute_finds_the_artifact_and_moves_the_tools_tree_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relative_request(tmp_path, monkeypatch)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    monkeypatch.setattr(LocalLinuxBackend, "_check_mkosi_version", lambda self: None)
    seen: list[tuple[list[str], Path | None]] = []

    def fake_mkosi(argv: Sequence[str], *, cwd: Path | None = None, **_: object) -> StreamResult:
        seen.append((list(argv), cwd))
        flags = _flags(list(argv))
        (Path(flags["directory"]) / "mkosi.tools" / "usr").mkdir(parents=True)
        (Path(flags["directory"]) / "mkosi.tools.manifest").write_text("{}")
        (Path(flags["output-dir"]) / "default.efi").write_bytes(b"MZ")
        return StreamResult(returncode=0, tail=())

    monkeypatch.setattr("tundravm.backends.local_linux.run_streaming", fake_mkosi)
    backend = LocalLinuxBackend(privilege="none")

    result = backend.execute(request)

    build = tmp_path / "build"
    artifact = result.profiles["default"].artifacts["qemu"]
    assert artifact.path == build / "default" / "output" / "default.efi"
    assert seen[0][1] == build / "mkosi" / "default"
    project = build / "mkosi" / "default"
    assert sorted(p.name for p in project.iterdir()) == ["mkosi.conf"]
    assert (build / ".mkosi" / "mkosi.tools" / "usr").is_dir()
    assert (build / ".mkosi" / "mkosi.tools.manifest").is_file()
    assert _flags(backend.command(request))["tools-tree"] == str(build / ".mkosi" / "mkosi.tools")


def test_local_execute_hands_sudo_output_back_to_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relative_request(tmp_path, monkeypatch)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    monkeypatch.setattr(LocalLinuxBackend, "_check_mkosi_version", lambda self: None)
    monkeypatch.setattr("tundravm.backends.local_linux.os.getuid", lambda: 1000)
    monkeypatch.setattr("tundravm.backends.local_linux.os.getgid", lambda: 1001)
    project = tmp_path / "build" / "mkosi" / "default"
    events: list[str] = []

    def failing_mkosi(argv: Sequence[str], **_: object) -> StreamResult:
        (project / "mkosi.tools").mkdir()
        return StreamResult(returncode=1, tail=("boom",))

    def fake_run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert (project / "mkosi.tools").is_dir()  # chown runs before the tools tree moves
        events.append(" ".join(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr("tundravm.backends.local_linux.run_streaming", failing_mkosi)
    monkeypatch.setattr("tundravm.backends.local_linux.subprocess.run", fake_run)

    with pytest.raises(BackendExecutionError):
        LocalLinuxBackend().execute(request)

    build = tmp_path / "build"
    owned = [build / "default" / "output", build / ".mkosi", project / "mkosi.tools"]
    assert events == ["sudo chown -R 1000:1001 " + " ".join(map(str, owned))]
    assert (build / ".mkosi" / "mkosi.tools").is_dir()
    assert not (project / "mkosi.tools").exists()


def test_local_requirements_probe_the_tools_a_tools_tree_replaces() -> None:
    by_tool = {r.tool: r for r in LocalLinuxBackend().requirements()}
    for tool in ("ukify", "systemd-repart", "apt"):
        assert by_tool[tool].optional
        assert 'Setting("Build", "ToolsTree", ("default",))' in by_tool[tool].hint
    assert by_tool["ukify"].hint.endswith("or install systemd-ukify")
    assert not by_tool["mkosi"].optional


# ── cloud postoutput tools ───────────────────────────────────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Local backend is Linux-specific.")
@pytest.mark.parametrize(("target", "tool"), [("azure", "qemu-img"), ("gcp", "sgdisk")])
def test_local_prepare_fails_fast_when_a_cloud_tool_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: OutputTarget, tool: str
) -> None:
    """The azure/gcp postoutput scripts would exit 127 inside mkosi; say so before it runs."""
    request = replace(bake_request(tmp_path), profile=target, output_targets=(target,))
    monkeypatch.setattr(
        "tundravm.backends.local_linux.shutil.which",
        lambda name: None if name == tool else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(LocalLinuxBackend, "_check_mkosi_version", lambda self: None)

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend().prepare(request)

    assert excinfo.value.code == "E_BACKEND_EXECUTION"
    assert f"`{tool}`" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert excinfo.value.hint.endswith("or bake --variant default")
    assert excinfo.value.context["tool"] == tool
    assert not request.build_dir.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Local backend is Linux-specific.")
def test_local_prepare_needs_no_cloud_tool_for_qemu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tundravm.backends.local_linux.shutil.which",
        lambda name: "/usr/bin/mkosi" if name == "mkosi" else None,
    )
    monkeypatch.setattr(LocalLinuxBackend, "_check_mkosi_version", lambda self: None)

    LocalLinuxBackend().prepare(bake_request(tmp_path))

    assert (tmp_path / "build").is_dir()


@pytest.mark.parametrize("target", ["azure", "gcp"])
def test_local_command_gives_a_default_tools_tree_the_cloud_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: OutputTarget
) -> None:
    """The cloud postoutput scripts run in the tools tree, which lacks qemu-img, sgdisk, parted."""
    request = replace(_relative_request(tmp_path, monkeypatch), output_targets=(target,))
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)

    flags = _flags(LocalLinuxBackend(privilege="none").command(request))

    assert flags["tools-tree"] == "default"
    assert flags["tools-tree-package"] == "qemu-utils,gdisk,parted"


def test_local_command_adds_no_cloud_packages_for_qemu_or_host_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relative_request(tmp_path, monkeypatch)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    assert "tools-tree-package" not in _flags(LocalLinuxBackend(privilege="none").command(request))
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: True)
    cloud = replace(request, output_targets=("azure",))
    assert not any(
        arg.startswith("--tools-tree") for arg in LocalLinuxBackend(privilege="none").command(cloud)
    )


def test_local_command_reuses_a_stashed_tools_tree_only_with_the_cloud_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = replace(_relative_request(tmp_path, monkeypatch), output_targets=("azure",))
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    backend = LocalLinuxBackend(privilege="none")
    cached = tmp_path / "build" / ".mkosi" / "mkosi.tools"
    (cached / "usr" / "bin").mkdir(parents=True)

    assert _flags(backend.command(request))["tools-tree"] == "default"

    (cached / "usr" / "bin" / "qemu-img").touch()
    (cached / "usr" / "sbin").mkdir()
    (cached / "usr" / "sbin" / "sgdisk").touch()
    (cached / "usr" / "sbin" / "parted").touch()
    flags = _flags(backend.command(request))
    assert flags["tools-tree"] == str(cached)
    assert "tools-tree-package" not in flags


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Local backend is Linux-specific.")
@pytest.mark.parametrize("conf", ["[Build]\nToolsTree=default\n", "[Output]\nFormat=uki\n"])
def test_local_prepare_skips_the_host_cloud_check_in_a_tools_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conf: str
) -> None:
    request = replace(_relative_request(tmp_path, monkeypatch, conf), output_targets=("azure",))
    monkeypatch.setattr(
        "tundravm.backends.local_linux.shutil.which",
        lambda name: None if name in ("qemu-img", "ukify") else f"/usr/bin/{name}",
    )
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    monkeypatch.setattr(LocalLinuxBackend, "_check_mkosi_version", lambda self: None)

    LocalLinuxBackend().prepare(request)

    assert (tmp_path / "build").is_dir()


# ── pefile ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "conf",
    [
        "[Output]\nFormat=directory\n",
        "[Output]\nFormat=disk\n",
        "[Output]\nFormat=disk\n[Content]\nBootable=yes\n",
    ],
)
def test_local_command_adds_a_tools_tree_without_pefile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conf: str
) -> None:
    """mkosi reads an installed kernel with pefile, even for a directory build."""
    notices: list[tuple[str, str]] = []
    request = replace(
        _relative_request(tmp_path, monkeypatch, conf),
        on_notice=lambda level, message: notices.append((level, message)),
    )
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: True)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_pefile", lambda: False)

    cmd = LocalLinuxBackend(privilege="none").command(request)

    assert "--tools-tree=default" in cmd
    assert notices == [
        (
            "info",
            "pefile not found on the host; building with mkosi's default tools tree "
            "(--tools-tree=default). Install python3-pefile to use the host tools.",
        )
    ]


@pytest.mark.parametrize(
    ("conf", "mkosi_args"),
    [
        ("[Output]\nFormat=disk\n[Content]\nBootable=no\n", []),
        ("[Output]\nFormat=directory\n", ["--bootable=no"]),
    ],
)
def test_local_command_keeps_host_tools_without_pefile_for_unbootable_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conf: str, mkosi_args: list[str]
) -> None:
    request = _relative_request(tmp_path, monkeypatch, conf)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    probed: list[bool] = []

    def no_pefile() -> bool:
        probed.append(True)
        return False

    monkeypatch.setattr("tundravm.backends.local_linux.host_has_pefile", no_pefile)

    cmd = LocalLinuxBackend(privilege="none", mkosi_args=mkosi_args).command(request)

    assert not any(arg.startswith("--tools-tree") for arg in cmd)
    assert probed == []


def test_local_notice_names_every_missing_host_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notices: list[str] = []
    request = replace(
        _relative_request(tmp_path, monkeypatch),
        on_notice=lambda _level, message: notices.append(message),
    )
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_ukify", lambda: False)
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_pefile", lambda: False)

    LocalLinuxBackend(privilege="none").command(request)

    assert notices[0].startswith("ukify and pefile not found on the host;")
    assert notices[0].endswith("Install systemd-ukify and python3-pefile to use the host tools.")


def test_local_requirements_probe_pefile_with_mkosis_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "mkosi"
    script.write_text("#!/opt/py/bin/python3.12 -s\nimport mkosi\n")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: str(script))

    by_tool = {r.tool: r for r in LocalLinuxBackend().requirements()}

    pefile = by_tool["pefile"]
    assert pefile.optional
    assert pefile.probe[0] == "/opt/py/bin/python3.12"
    assert pefile.probe[1:] == ("-c", "import pefile; print(pefile.__version__)")
    assert pefile.hint == (
        'install python3-pefile, or let mkosi use its tools tree (Setting("Build", "ToolsTree", '
        '("default",)))'
    )


@pytest.mark.parametrize(
    ("shebang", "python"),
    [("#!/usr/bin/env python3\n", "python3"), ("#!/bin/sh\n", "python3"), ("", "python3")],
)
def test_mkosi_python_falls_back_to_python3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shebang: str, python: str
) -> None:
    from tundravm.backends.local_linux import mkosi_python

    script = tmp_path / "mkosi"
    script.write_text(f"{shebang}exec mkosi\n")
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: str(script))
    assert mkosi_python() == python
    monkeypatch.setattr("tundravm.backends.local_linux.shutil.which", lambda _: None)
    assert mkosi_python() == "python3"
