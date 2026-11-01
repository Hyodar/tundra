"""``tundravm doctor``: one row per capability, each failure naming its fix."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import run_main
from tundravm.backends.local_linux import DEBIAN_KEYRING
from tundravm.capabilities import (
    GO_PURPOSE,
    RUSTUP_PURPOSE,
    Host,
    ToolchainNeeds,
    capabilities,
    default_backend,
)
from tundravm.cli import EXIT_FAILURE, EXIT_OK, doctor
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
    "go": "go version go1.24.4 linux/amd64",
    "rustup": "rustup 1.28.2 (e4f3ad6f8 2025-04-28)",
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


def _refusing(runner: Callable[[Sequence[str]], Any], argv: tuple[str, ...]) -> Any:
    """*runner*, except that *argv* exits 1 (``sudo -n true`` on a host that asks)."""

    def run(given: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if tuple(given) == argv:
            return subprocess.CompletedProcess(list(given), 1, "", "a password is required\n")
        result: subprocess.CompletedProcess[str] = runner(given)
        return result

    return run


def _rows(host: Host, **kwargs: object) -> dict[str, str]:
    return {row.name: row.render().split(None, 1)[1] for row in capabilities(host, **kwargs)}  # type: ignore[arg-type]


LINUX_FILES = (DEBIAN_KEYRING, "/usr/share/OVMF/OVMF_CODE_4M.fd")


def test_a_ready_linux_host_reports_every_capability_in_the_plan_format() -> None:
    host, _ = _host(files=LINUX_FILES)

    rows = [row.render() for row in capabilities(host)]

    assert rows == [
        f"build:x86-64     ready (mkosi {MKOSI_PIN.version}, user namespaces)",
        f"build:go         ready (go1.24.4, which {GO_PURPOSE})",
        f"build:rustup     ready (rustup 1.28.2, which {RUSTUP_PURPOSE})",
        "run:simulated    ready (qemu 9.2.0, OVMF /usr/share/OVMF/OVMF_CODE_4M.fd, KVM)",
        "run:tdx          unavailable: no Intel TDX host: kvm_intel tdx is not enabled",
        "deploy:gcp       ready (gcloud as ops@example.com)",
        "deploy:azure     ready (az as ops@example.com)",
        rows[7],
        rows[8],
        "verify:azure     unsupported: Azure evidence is vTPM-bound: no TDX register can be "
        "predicted",
    ]
    assert rows[7].startswith("verify:qemu      ready (built-in TDX replay ")
    assert rows[8].startswith("verify:gcp       ready (built-in replay, GCP firmware table ")


NO_GO = f"no go on PATH, which {GO_PURPOSE}: install Go 1.21+"
NO_RUSTUP = (
    f"no rustup on PATH, which {RUSTUP_PURPOSE}: install rustup "
    "(https://rustup.rs, or the Debian package rustup)"
)
NEEDS_BOTH = ToolchainNeeds(go=("tdxs",), rustup=("raiko",))


@pytest.mark.parametrize(
    ("tool", "version", "needs", "row", "code"),
    [
        ("go", None, NEEDS_BOTH, f"blocked: {NO_GO}", EXIT_FAILURE),
        (
            "go",
            "go version go1.19.8 linux/amd64",
            NEEDS_BOTH,
            f"blocked: go1.19.8 predates toolchain switching (Go 1.21), and go {GO_PURPOSE}: "
            "install Go 1.21+",
            EXIT_FAILURE,
        ),
        ("rustup", None, NEEDS_BOTH, f"blocked: {NO_RUSTUP}", EXIT_FAILURE),
        ("go", None, None, f"optional: {NO_GO}", EXIT_OK),
        ("rustup", None, None, f"optional: {NO_RUSTUP}", EXIT_OK),
        (
            "rustup",
            None,
            ToolchainNeeds(go=("tdxs",)),
            "optional: not needed by this image",
            EXIT_OK,
        ),
        (
            "go",
            "go version go1.24.4 linux/amd64",
            ToolchainNeeds(),
            "optional: not needed by this image",
            EXIT_OK,
        ),
    ],
    ids=[
        "no-go",
        "too-old-go",
        "no-rustup",
        "no-image-go",
        "no-image-rustup",
        "unneeded",
        "unneeded-present",
    ],
)
def test_the_host_toolchain_rows_block_only_for_an_image_that_needs_them(
    tool: str, version: str | None, needs: ToolchainNeeds | None, row: str, code: int
) -> None:
    """build:go and build:rustup say what each tool is for, and block only when the image's
    sources need it; without an image a missing tool is optional."""
    tools = {k: v for k, v in TOOLS.items() if k != tool}
    host, _ = _host(tools if version is None else {**tools, tool: version}, files=LINUX_FILES)
    assert _rows(host, needs=needs)[f"build:{tool}"] == row
    assert doctor(io.StringIO(), area="build", host=host, needs=needs) == code


PROBE = """
from dataclasses import replace

from tundravm import Service
from tundravm.sources import CargoBinary, Git

probe = CargoBinary("probe", Git("https://example.com/probe.git", "b" * 40))
image = replace(image, services=[*image.services, Service("probe", [probe])])
"""


@pytest.mark.parametrize(
    ("added", "row", "code"),
    [
        ("", "optional: not needed by this image", EXIT_OK),
        (PROBE, f"blocked: {NO_RUSTUP}", EXIT_FAILURE),
    ],
    ids=["no-cargo-source", "cargo-source-not-checked-out"],
)
def test_doctor_reads_what_the_projects_image_needs(
    project: Path, monkeypatch: pytest.MonkeyPatch, added: str, row: str, code: int
) -> None:
    """The project's image says which toolchains the build needs: every image's Tdxs is a Go
    source, and a Cargo source not checked out yet may pin a Rust toolchain."""
    with (project / "demo.py").open("a", encoding="utf-8") as demo:
        demo.write(added)
    host, _ = _host({k: v for k, v in TOOLS.items() if k != "rustup"}, files=LINUX_FILES)
    monkeypatch.setattr("tundravm.cli.Host", lambda: host)
    got, out = run_main("doctor", "--for", "build")
    rows = {line.split(None, 1)[0]: line.split(None, 1)[1] for line in out.splitlines()}
    assert rows["build:rustup"] == row and rows["build:go"].startswith("ready (go1.24.4")
    assert got == code


def test_build_falls_back_to_sudo_and_names_the_userns_fix_without_it() -> None:
    sudo, _ = _host(files=LINUX_FILES, userns=False)
    assert _rows(sudo)["build:x86-64"] == (
        f"ready (mkosi {MKOSI_PIN.version}, sudo, as this host has no unprivileged user namespaces)"
    )
    ask = replace(sudo, runner=_refusing(sudo.runner, ("sudo", "-n", "true")))
    assert _rows(ask)["build:x86-64"] == (
        f"ready (mkosi {MKOSI_PIN.version}, sudo, which may ask for a password, as this host "
        "has no unprivileged user namespaces)"
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
        "blocked: this is Darwin: build with the Lima backend (TUNDRAVM_BACKEND=lima)"
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


# ── the command ──────────────────────────────────────────────────────────


def _names(out: io.StringIO) -> list[str]:
    return [line.split()[0] for line in out.getvalue().splitlines()]


@pytest.mark.parametrize(
    ("area", "names"),
    [
        ("build", ["build:x86-64", "build:go", "build:rustup"]),
        ("run", ["run:simulated"]),
        ("tdx", ["run:tdx"]),
        ("azure", ["deploy:azure", "verify:azure"]),
        ("gcp", ["deploy:gcp", "verify:gcp"]),
    ],
)
def test_doctor_for_one_capability(area: str, names: list[str]) -> None:
    host, _ = _host(files=LINUX_FILES)
    out = io.StringIO()
    doctor(out, area=area, host=host)
    assert _names(out) == names


def test_doctor_for_fails_on_a_blocked_row_of_that_area() -> None:
    host, _ = _host(files=LINUX_FILES, accounts={})
    assert doctor(io.StringIO(), area="gcp", host=host) == EXIT_FAILURE
    assert doctor(io.StringIO(), area="build", host=host) == EXIT_OK


def test_doctor_without_for_prints_every_row_and_fails_only_on_build() -> None:
    host, _ = _host(files=LINUX_FILES, accounts={})
    out = io.StringIO()
    assert doctor(out, host=host) == EXIT_OK
    assert _names(out) == [
        "build:x86-64",
        "build:go",
        "build:rustup",
        "run:simulated",
        "run:tdx",
        "deploy:gcp",
        "deploy:azure",
        "verify:qemu",
        "verify:gcp",
        "verify:azure",
    ]
    blocked, _ = _host({}, files=())
    assert doctor(io.StringIO(), host=blocked) == EXIT_FAILURE


def test_doctor_json() -> None:
    host, _ = _host(files=LINUX_FILES)
    out = io.StringIO()
    doctor(out, area="tdx", host=host, as_json=True)
    payload = json.loads(out.getvalue())
    assert payload["schema"] == "tundravm.doctor/1"
    assert payload["rows"] == [
        {
            "name": "run:tdx",
            "verdict": "unavailable",
            "detail": "no Intel TDX host: kvm_intel tdx is not enabled",
        }
    ]


def test_doctor_cli_takes_for_and_the_injected_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    host, ran = _host(files=LINUX_FILES)
    monkeypatch.setattr("tundravm.cli.Host", lambda: host)
    code, out = run_main("doctor", "--for", "run")
    assert code == EXIT_OK
    assert out.splitlines()[0].startswith("run:simulated    ready (qemu 9.2.0")
    assert ("qemu-system-x86_64", "--version") in ran
