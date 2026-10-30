"""``tundravm doctor``: one row per capability, each failure naming its fix."""

from __future__ import annotations

import io
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend
from tundravm.backends.local_linux import DEBIAN_KEYRING
from tundravm.capabilities import Capability, Host, capabilities, default_backend
from tundravm.cli import EXIT_FAILURE, EXIT_OK, doctor
from tundravm.recipe import load_file
from tundravm.toolchain import MKOSI_PIN

TOOLS = {
    "apt-get": "apt 2.9.8 (amd64)",
    "dpkg": "Debian 'dpkg' package management program version 1.22.11 (amd64).",
    "test": "",
    "sudo": "Sudo version 1.9.16",
    "qemu-system-x86_64": "QEMU emulator version 9.2.0 (Debian 1:9.2.0+ds-1)",
    "limactl": "limactl version 1.0.0",
    "gcloud": "Google Cloud SDK 500.0.0",
    "az": "azure-cli 2.66.0",
}


def _host(
    tools: Mapping[str, str] = TOOLS,
    *,
    system: str = "Linux",
    machine: str = "x86_64",
    userns: bool = True,
    uid: int = 1000,
    files: Sequence[str] = ("/usr/share/OVMF/OVMF_CODE_4M.fd",),
    proc: Mapping[str, str] | None = None,
    kvm: bool = True,
    accounts: Mapping[str, str] | None = None,
) -> tuple[Host, list[tuple[str, ...]]]:
    """A described host: *tools* answer their commands, *files* exist, *proc* is readable."""
    ran: list[tuple[str, ...]] = []
    every = {"gcloud": "ops@example.com", "az": "ops@example.com"}
    logins = every if accounts is None else accounts

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        ran.append(tuple(argv))
        tool = argv[0]
        if tool not in tools:
            raise FileNotFoundError(tool)
        if tool == "test":
            return subprocess.CompletedProcess(list(argv), 0 if argv[-1] in files else 1, "", "")
        if tuple(argv[1:2]) in (("auth",), ("account",)):
            login = logins.get(tool, "")
            return subprocess.CompletedProcess(list(argv), 0 if login else 1, login + "\n", "")
        return subprocess.CompletedProcess(list(argv), 0, tools[tool] + "\n", "")

    readable = proc or {}
    host = Host(
        runner=runner,
        system=system,
        machine=machine,
        uid=uid,
        env={},
        exists=lambda path: path in files,
        read=lambda path: readable.get(path),
        userns=lambda: userns,
        kvm=lambda: kvm,
        mkosi_version=lambda: MKOSI_PIN.version,
    )
    return host, ran


def _rows(host: Host, **kwargs: object) -> dict[str, str]:
    return {row.name: row.render().split(None, 1)[1] for row in capabilities(host, **kwargs)}  # type: ignore[arg-type]


LINUX_FILES = (DEBIAN_KEYRING, "/usr/share/OVMF/OVMF_CODE_4M.fd")


def test_a_ready_linux_host_reports_every_capability_in_the_plan_format() -> None:
    host, _ = _host(files=LINUX_FILES)

    rows = [row.render() for row in capabilities(host)]

    assert rows == [
        f"build:x86-64     ready (mkosi {MKOSI_PIN.version}, user namespaces)",
        "run:simulated    ready (qemu 9.2.0, OVMF /usr/share/OVMF/OVMF_CODE_4M.fd, KVM)",
        "run:tdx          unavailable: no Intel TDX host (kvm_intel tdx is not enabled)",
        "deploy:gcp       ready (gcloud as ops@example.com)",
        "deploy:azure     ready (az as ops@example.com)",
        rows[5],
        rows[6],
        "verify:azure     unsupported: Azure evidence is vTPM-bound: no TDX register can be "
        "predicted",
    ]
    assert rows[5].startswith("verify:qemu      ready (built-in TDX replay ")
    assert rows[6].startswith("verify:gcp       ready (built-in replay, GCP firmware table ")


def test_build_falls_back_to_sudo_and_names_the_userns_fix_without_it() -> None:
    sudo, _ = _host(files=LINUX_FILES, userns=False)
    assert _rows(sudo)["build:x86-64"] == (
        f"ready (mkosi {MKOSI_PIN.version}, sudo (no unprivileged user namespaces here))"
    )
    root, _ = _host(files=LINUX_FILES, userns=False, uid=0)
    assert _rows(root)["build:x86-64"].endswith(", root)")

    no_sudo = {tool: text for tool, text in TOOLS.items() if tool != "sudo"}
    restricted = {"/proc/sys/kernel/apparmor_restrict_unprivileged_userns": "1"}
    host, _ = _host(no_sudo, files=LINUX_FILES, userns=False, proc=restricted)
    assert _rows(host)["build:x86-64"] == (
        "blocked: no unprivileged user namespaces and no sudo: "
        "run `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`"
    )
    clone_off = {"/proc/sys/kernel/unprivileged_userns_clone": "0"}
    host, _ = _host(no_sudo, files=LINUX_FILES, userns=False, proc=clone_off)
    assert "kernel.unprivileged_userns_clone=1" in _rows(host)["build:x86-64"]


@pytest.mark.parametrize(
    ("missing", "fix"),
    [
        ("apt-get", "blocked: no apt-get (mkosi bootstraps its tools tree with it): install apt"),
        ("dpkg", "blocked: no dpkg (mkosi bootstraps its tools tree with it): install dpkg"),
    ],
)
def test_build_names_the_missing_host_tool(missing: str, fix: str) -> None:
    tools = {tool: text for tool, text in TOOLS.items() if tool != missing}
    host, _ = _host(tools, files=LINUX_FILES)

    assert _rows(host)["build:x86-64"] == fix


def test_build_needs_the_debian_keyring_and_the_pinned_mkosi() -> None:
    host, _ = _host(files=())
    assert _rows(host)["build:x86-64"] == (
        "blocked: no Debian archive keyring: install debian-archive-keyring"
    )
    host, _ = _host(files=LINUX_FILES)
    host.mkosi_version = lambda: "26"
    assert _rows(host)["build:x86-64"] == (
        f"blocked: mkosi 26 in tundravm's environment, it needs {MKOSI_PIN.version}: "
        "reinstall tundravm (`uv sync` or `pip install`)"
    )


def test_macos_builds_in_lima_and_says_what_tcg_costs() -> None:
    host, _ = _host(system="Darwin", machine="arm64")

    assert default_backend(host) == "lima"
    assert _rows(host, areas=("build",))["build:x86-64"] == (
        "ready (Lima x86-64 guest under QEMU TCG emulation on this arm64 host: same bytes, "
        f"several times slower; mkosi {MKOSI_PIN.version})"
    )
    no_lima = {tool: text for tool, text in TOOLS.items() if tool != "limactl"}
    host, _ = _host(no_lima, system="Darwin", machine="arm64")
    assert _rows(host, areas=("build",))["build:x86-64"] == (
        "blocked: limactl not found: install Lima (lima-vm.io)"
    )
    linux, _ = _host(files=LINUX_FILES)
    assert default_backend(linux) == "local"
    assert _rows(linux, backend="lima")["build:x86-64"] == (
        f"ready (Lima x86-64 guest, mkosi {MKOSI_PIN.version})"
    )
    host, _ = _host(system="Darwin", machine="arm64")
    assert _rows(host, backend="local")["build:x86-64"] == (
        "blocked: this is Darwin: build with the Lima backend (--backend lima)"
    )


def test_run_rows_name_qemu_ovmf_kvm_and_tdx() -> None:
    host, _ = _host(files=LINUX_FILES, kvm=False)
    assert _rows(host)["run:simulated"].endswith(", TCG emulation, slow)")
    no_qemu = {tool: text for tool, text in TOOLS.items() if tool != "qemu-system-x86_64"}
    host, _ = _host(no_qemu, files=LINUX_FILES)
    assert _rows(host)["run:simulated"] == (
        "blocked: qemu-system-x86_64 not found: install qemu-system-x86"
    )
    host, _ = _host(files=(DEBIAN_KEYRING,))
    assert _rows(host)["run:simulated"] == "blocked: no OVMF firmware: install ovmf"

    tdx = {"/sys/module/kvm_intel/parameters/tdx": "Y"}
    host, _ = _host(files=(*LINUX_FILES, "/usr/share/ovmf/OVMF.fd"), proc=tdx)
    assert _rows(host)["run:tdx"] == "ready (TDVF /usr/share/ovmf/OVMF.fd)"
    host, _ = _host(files=LINUX_FILES, proc=tdx)
    assert _rows(host)["run:tdx"] == "blocked: no TDVF firmware: install ovmf with TDX support"


def test_deploy_rows_name_the_cli_and_the_login() -> None:
    host, _ = _host(files=LINUX_FILES, accounts={})
    rows = _rows(host, areas=("deploy",))
    assert rows == {
        "deploy:gcp": "blocked: gcloud has no active account: run `gcloud auth login`",
        "deploy:azure": "blocked: az is not logged in: run `az login`",
    }
    bare = {tool: text for tool, text in TOOLS.items() if tool not in ("gcloud", "az")}
    host, _ = _host(bare, files=LINUX_FILES)
    assert _rows(host, areas=("deploy",)) == {
        "deploy:gcp": "blocked: gcloud not found: install the Google Cloud CLI",
        "deploy:azure": "blocked: az not found: install the Azure CLI",
    }


def test_inprocess_builds_nothing() -> None:
    host, ran = _host(tools={})
    assert _rows(host, backend="inprocess", areas=("build",)) == {
        "build:x86-64": "ready (simulated: the inprocess backend builds nothing)"
    }
    assert ran == []


def test_capability_render_pads_the_name() -> None:
    assert Capability("run:tdx", "unavailable", "no TDX").render() == (
        "run:tdx          unavailable: no TDX"
    )
    assert Capability("build:x86-64", "ready").render() == "build:x86-64     ready"


# ── the command ──────────────────────────────────────────────────────────


def test_doctor_for_prints_only_that_area_and_fails_on_a_blocked_row() -> None:
    host, _ = _host(files=LINUX_FILES, accounts={})
    out = io.StringIO()

    code = doctor(None, out, host=host, areas=["deploy"])

    assert code == EXIT_FAILURE
    assert [line.split()[0] for line in out.getvalue().splitlines()] == [
        "deploy:gcp",
        "deploy:azure",
    ]


def test_doctor_without_for_prints_every_row_and_fails_only_on_build() -> None:
    host, _ = _host(files=LINUX_FILES, accounts={})
    out = io.StringIO()

    assert doctor(None, out, host=host) == EXIT_OK
    names = [line.split()[0] for line in out.getvalue().splitlines()]
    assert names == [
        "build:x86-64",
        "run:simulated",
        "run:tdx",
        "deploy:gcp",
        "deploy:azure",
        "verify:qemu",
        "verify:gcp",
        "verify:azure",
    ]
    blocked, _ = _host({}, files=())
    assert doctor(None, io.StringIO(), host=blocked) == EXIT_FAILURE


def test_doctor_checks_the_given_or_recipe_backend(tmp_path: Path) -> None:
    host, _ = _host(system="Darwin", machine="arm64")
    out = io.StringIO()
    doctor(None, out, host=host, backend=LocalLinuxBackend(), areas=["build"])
    assert "blocked: this is Darwin" in out.getvalue()

    path = write_recipe_file(
        tmp_path,
        """
from tundravm.declarative import Package, Recipe
from tundravm.backends import LimaMkosiBackend

backend = LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB")
recipe = Recipe("node", items=(Package("linux-image-amd64"),))
""",
    )
    host, _ = _host(files=LINUX_FILES)
    out = io.StringIO()
    assert doctor(load_file(path), out, host=host, areas=["build"]) == EXIT_OK
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("build:x86-64     ready (Lima x86-64 guest")
    assert lines[-1].startswith("lint: ")
    assert isinstance(load_file(path).backend, LimaMkosiBackend)


def test_doctor_cli_takes_for_and_the_injected_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    host, ran = _host(files=LINUX_FILES)
    monkeypatch.setattr("tundravm.cli.Host", lambda: host)

    code, out = run_main("doctor", "--for", "run")

    assert code == EXIT_OK
    assert out.splitlines()[0].startswith("run:simulated    ready (qemu 9.2.0")
    assert ("qemu-system-x86_64", "--version") in ran
    assert "build:" not in out and "lint:" not in out
