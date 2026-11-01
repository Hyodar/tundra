"""``tundravm doctor``: what this host can do, one independent capability per row.

Each row is ``<area>:<what>`` and a verdict:

- ``ready`` (with what it will use),
- ``blocked: <why>`` when something installable or configurable is missing,
- ``unavailable: <why>`` when this host cannot do it (no TDX hardware),
- ``optional: <why>`` when the image needs no such tool (``not needed by this image``), or
  no image says whether it does and the tool (``rustup``) is missing (never a failure),
- ``unsupported: <why>`` when tundravm does not do it anywhere.

A row's *fix* is the one command that clears it, an apt package on Debian or Ubuntu; a
ready row has one when a command makes it better (KVM instead of TCG, no sudo).

Every fact comes through :class:`Host`, so tests describe a host instead of probing this one.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .backends.local_linux import DEBIAN_KEYRING
from .deploy.qemu import TDVF_PATHS, ovmf_firmware
from .measure.gcp_firmware import TABLE_VERSION
from .measure.model import MEASURE_VERSION
from .toolchain import MKOSI_PIN, installed_version

Area = Literal["build", "run", "deploy", "verify"]
AREAS: tuple[Area, ...] = ("build", "run", "deploy", "verify")
Verdict = Literal["ready", "blocked", "optional", "unavailable", "unsupported"]
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
BuildBackendKind = Literal["local", "lima"]


@dataclass(frozen=True, slots=True)
class Capability:
    """One row: *name* (``build:x86-64``), its *verdict*, the *detail* after it and its
    *fix*, one command (``sudo apt install golang``), ``""`` for none."""

    name: str
    verdict: Verdict
    detail: str = ""
    fix: str = ""

    @property
    def area(self) -> str:
        return self.name.split(":", 1)[0]

    def render(self, width: int = 16) -> str:
        if self.verdict == "ready":
            text = "ready" + (f" ({self.detail})" if self.detail else "")
        else:
            text = f"{self.verdict}: {self.detail}"
        return f"{self.name.ljust(width)} {text}"


def run_probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* with a 10s timeout, capturing output."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)


def _userns_works() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    from . import _mkosi

    return not _mkosi.needs_sudo()


def _kvm_usable() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


@dataclass(slots=True)
class Host:
    """What doctor knows about the host; every field is replaceable (tests)."""

    runner: ProbeRunner = run_probe
    system: str = field(default_factory=platform.system)
    machine: str = field(default_factory=platform.machine)
    uid: int = field(default_factory=os.getuid)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    exists: Callable[[str], bool] = os.path.exists
    read: Callable[[str], str | None] = _read
    userns: Callable[[], bool] = _userns_works
    kvm: Callable[[], bool] = _kvm_usable
    mkosi_version: Callable[[], str | None] = lambda: installed_version("mkosi")

    def output(self, *argv: str) -> str | None:
        """The first line *argv* prints, or ``None`` when it is missing or fails."""
        try:
            result = self.runner(argv)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        text = (result.stdout or result.stderr or "").strip()
        return text.splitlines()[0] if text else ""

    def has(self, tool: str) -> bool:
        """Whether ``tool --version`` runs (through :attr:`runner`, so tests decide)."""
        return self.output(tool, "--version") is not None

    @property
    def linux_x86_64(self) -> bool:
        return self.system == "Linux" and self.machine in ("x86_64", "AMD64")

    @property
    def apple_silicon(self) -> bool:
        return self.system == "Darwin" and self.machine == "arm64"


def default_backend(host: Host) -> BuildBackendKind:
    """The backend this host builds with: Linux x86-64 builds natively, the rest in Lima."""
    return "local" if host.linux_x86_64 else "lima"


# ── build ────────────────────────────────────────────────────────────────


def build_capability(host: Host, backend: BuildBackendKind) -> Capability:
    """``build:x86-64``: whether *backend* can build here, and how."""
    name = "build:x86-64"
    if backend == "lima":
        return _lima_build(name, host)
    return _linux_build(name, host)


def _lima_build(name: str, host: Host) -> Capability:
    if not host.has("limactl"):
        fix = "brew install lima" if host.system == "Darwin" else "install Lima: https://lima-vm.io"
        return Capability(name, "blocked", "no limactl, which runs the Lima build guest", fix)
    if host.machine in ("x86_64", "AMD64"):
        return Capability(name, "ready", f"Lima x86-64 guest, mkosi {MKOSI_PIN.version}")
    return Capability(
        name,
        "ready",
        f"Lima x86-64 guest under QEMU TCG emulation on this {host.machine} host: "
        f"same bytes, several times slower; mkosi {MKOSI_PIN.version}",
    )


def _linux_build(name: str, host: Host) -> Capability:
    lima = "export TUNDRAVM_BACKEND=lima"
    if host.system != "Linux":
        return Capability(name, "blocked", f"this is {host.system}, not x86-64 Linux", lima)
    if not host.linux_x86_64:
        return Capability(name, "blocked", f"this is {host.machine}, not x86-64 Linux", lima)
    version = host.mkosi_version()
    if version != MKOSI_PIN.version:
        found = "no mkosi" if version is None else f"mkosi {version}"
        return Capability(
            name,
            "blocked",
            f"{found} in tundravm's environment, which installs mkosi {MKOSI_PIN.version}",
            "uv tool upgrade --reinstall tundravm (in a project: uv sync --reinstall)",
        )
    for tool in ("apt-get", "dpkg"):
        if host.output(tool, "--version") is None:
            return Capability(
                name,
                "blocked",
                f"no {tool}, which mkosi bootstraps its Debian tools tree with",
                f"build on Debian or Ubuntu, or {lima}",
            )
    if host.output("test", "-e", DEBIAN_KEYRING) is None:
        return Capability(
            name,
            "blocked",
            "no Debian archive keyring, which checks the tools tree's packages",
            apt_install(["debian-archive-keyring"]),
        )
    privilege = _privilege(host)
    if privilege is None:
        fix = (
            _userns_fix(host) or "allow user namespaces (container or LSM policy), or install sudo"
        )
        return Capability(name, "blocked", "no unprivileged user namespaces and no sudo", fix)
    fix = "" if privilege in ("root", "user namespaces") else _userns_fix(host)
    return Capability(name, "ready", f"mkosi {MKOSI_PIN.version}, {privilege}", fix)


GO_PURPOSE = (
    "fetches each Go source's modules and the toolchain its go.mod names for the offline "
    "build (every image's Tdxs is one)"
)
"""What the host ``go`` is for, in every ``build:go`` row."""

GO_FIX = "sudo apt install golang (Debian 13, Ubuntu 24.04; older: https://go.dev/dl)"
"""The fix of a host without ``go``: Debian 13's and Ubuntu 24.04's golang are 1.21+."""


@dataclass(frozen=True, slots=True)
class ToolchainNeeds:
    """The sources of the image doctor checks that need a host toolchain to build.

    *go*: its Go source builds. *rustup*: its Cargo source builds that pin a Rust toolchain,
    or whose checkout is not in the workspace yet to say.
    """

    go: tuple[str, ...] = ()
    rustup: tuple[str, ...] = ()


def _as_needed(row: Capability, wanted: Sequence[str] | None) -> Capability:
    """*row* for an image whose sources *wanted* need the tool (``None``: no image).

    Unneeded, it is ``optional: not needed by this image``; without an image, a missing
    tool is ``optional`` rather than ``blocked``. Either way it never fails doctor.
    """
    if wanted is not None and not wanted:
        return Capability(row.name, "optional", "not needed by this image")
    if wanted is None and row.verdict == "blocked":
        return Capability(row.name, "optional", row.detail, row.fix)
    return row


def go_capability(host: Host, needs: ToolchainNeeds | None = None) -> Capability:
    """``build:go``: the host ``go`` that prefetches Go sources (1.21+ switches toolchains),
    blocked when the image *needs* it (:func:`_as_needed`) and, without an image, when it
    is missing: every image builds its evidence service (``Tdxs``) from Go."""
    row = _go_row(host)
    return row if needs is None else _as_needed(row, needs.go)


def _go_row(host: Host) -> Capability:
    name = "build:go"
    words = (host.output("go", "version") or "").split()
    found = words[2] if len(words) > 2 and words[2].startswith("go1.") else None
    if found is None:
        return Capability(name, "blocked", f"no go on PATH, which {GO_PURPOSE}", GO_FIX)
    minor = found.removeprefix("go1.").split(".", 1)[0]
    if not minor.isdigit() or int(minor) < 21:
        return Capability(
            name,
            "blocked",
            f"{found} predates toolchain switching (Go 1.21), and go {GO_PURPOSE}",
            "install Go 1.21 or newer: https://go.dev/dl",
        )
    return Capability(name, "ready", f"{found}, which {GO_PURPOSE}")


RUSTUP_PURPOSE = (
    "installs the toolchain each Cargo source's rust-toolchain.toml pins for the offline build"
)
"""What the host ``rustup`` is for, in every ``build:rustup`` row."""


def rustup_capability(host: Host, needs: ToolchainNeeds | None = None) -> Capability:
    """``build:rustup``: the host ``rustup`` that installs a Cargo source's pinned toolchain,
    blocked only when the image *needs* it (:func:`_as_needed`)."""
    return _as_needed(_rustup_row(host), None if needs is None else needs.rustup)


def _rustup_row(host: Host) -> Capability:
    name = "build:rustup"
    words = (host.output("rustup", "--version") or "").split()
    if len(words) < 2 or words[0] != "rustup":
        return Capability(
            name,
            "blocked",
            f"no rustup on PATH, which {RUSTUP_PURPOSE}",
            "sudo apt install rustup (or https://rustup.rs)",
        )
    return Capability(name, "ready", f"rustup {words[1]}, which {RUSTUP_PURPOSE}")


def _privilege(host: Host) -> str | None:
    """How mkosi gets privileges here, or ``None`` when it cannot."""
    if host.uid == 0:
        return "root"
    if host.userns():
        return "user namespaces"
    if host.has("sudo"):
        ask = (
            ""
            if host.output("sudo", "-n", "true") is not None
            else ", which may ask for a password"
        )
        return f"sudo{ask}, as this host has no unprivileged user namespaces"
    return None


USERNS_KNOBS = (
    ("kernel.apparmor_restrict_unprivileged_userns", "1", "0"),
    ("kernel.unprivileged_userns_clone", "0", "1"),
    ("user.max_user_namespaces", "0", "65536"),
)
"""``(sysctl, the value that withholds user namespaces, the value that grants them)``."""


def _userns_fix(host: Host) -> str:
    """The sysctl that grants this host unprivileged user namespaces, ``""`` when no kernel
    knob withholds them."""
    for key, off, on in USERNS_KNOBS:
        if host.read(f"/proc/sys/{key.replace('.', '/')}") == off:
            return f"sudo sysctl -w {key}={on}"
    return ""


# ── run ──────────────────────────────────────────────────────────────────


def run_capabilities(host: Host) -> tuple[Capability, ...]:
    """``run:simulated`` (QEMU + OVMF) and ``run:tdx`` (a TDX host)."""
    return (_run_simulated(host), _run_tdx(host))


def _qemu(host: Host) -> str | None:
    line = host.output("qemu-system-x86_64", "--version")
    if line is None:
        return None
    words = line.split()
    index = words.index("version") + 1 if "version" in words else len(words)
    return words[index] if index < len(words) else "unknown"


def apt_install(packages: Sequence[str]) -> str:
    """The command that installs *packages* on Debian or Ubuntu."""
    return f"sudo apt install {' '.join(packages)}"


def _run_simulated(host: Host) -> Capability:
    name = "run:simulated"
    qemu, firmware = _qemu(host), ovmf_firmware(host.exists)
    missing = [
        (what, package)
        for what, package, found in (
            ("no qemu-system-x86_64", "qemu-system-x86", qemu),
            ("no OVMF firmware", "ovmf", firmware),
        )
        if found is None
    ]
    if qemu is None or firmware is None:
        why = ", ".join(what for what, _ in missing)
        return Capability(name, "blocked", why, apt_install([p for _, p in missing]))
    if host.system == "Linux" and host.kvm():
        return Capability(name, "ready", f"qemu {qemu}, OVMF {firmware[0]}, KVM")
    fix = "sudo usermod -aG kvm $USER, then log in again" if host.exists("/dev/kvm") else ""
    return Capability(name, "ready", f"qemu {qemu}, OVMF {firmware[0]}, TCG emulation, slow", fix)


def _run_tdx(host: Host) -> Capability:
    name = "run:tdx"
    enabled = host.read("/sys/module/kvm_intel/parameters/tdx")
    if host.system != "Linux" or enabled not in ("Y", "1"):
        return Capability(name, "unavailable", "no Intel TDX host: kvm_intel tdx is not enabled")
    if _qemu(host) is None:
        return Capability(
            name, "blocked", "no qemu-system-x86_64", apt_install(["qemu-system-x86"])
        )
    firmware = next((path for path in TDVF_PATHS if host.exists(path)), None)
    if firmware is None:
        return Capability(
            name,
            "blocked",
            "no TDVF firmware (OVMF built with TDX support)",
            "set up the TDX host: https://github.com/canonical/tdx",
        )
    return Capability(name, "ready", f"TDVF {firmware}")


# ── deploy ───────────────────────────────────────────────────────────────


def deploy_capabilities(host: Host) -> tuple[Capability, ...]:
    """``deploy:gcp`` (the gcloud CLI and an account) and ``deploy:azure`` (az, logged in)."""
    return (_deploy_gcp(host), _deploy_azure(host))


def _deploy_gcp(host: Host) -> Capability:
    name = "deploy:gcp"
    if not host.has("gcloud"):
        return Capability(
            name,
            "blocked",
            "no gcloud",
            "install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install",
        )
    account = host.output(
        "gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"
    )
    if not account:
        return Capability(name, "blocked", "gcloud has no active account", "gcloud auth login")
    return Capability(name, "ready", f"gcloud as {account}")


def _deploy_azure(host: Host) -> Capability:
    name = "deploy:azure"
    if not host.has("az"):
        return Capability(
            name,
            "blocked",
            "no az",
            "install the Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli",
        )
    account = host.output("az", "account", "show", "--query", "user.name", "--output", "tsv")
    if not account:
        return Capability(name, "blocked", "az is not logged in", "az login")
    return Capability(name, "ready", f"az as {account}")


# ── verify ───────────────────────────────────────────────────────────────


def verify_capabilities() -> tuple[Capability, ...]:
    """What tundravm replays measurements for: built in, so nothing depends on the host."""
    return (
        Capability("verify:qemu", "ready", f"built-in TDX replay {MEASURE_VERSION}"),
        Capability("verify:gcp", "ready", f"built-in replay, GCP firmware table {TABLE_VERSION}"),
        Capability(
            "verify:azure",
            "unsupported",
            "Azure evidence is vTPM-bound: no TDX register can be predicted",
        ),
    )


# ── report ───────────────────────────────────────────────────────────────


def capabilities(
    host: Host,
    *,
    backend: BuildBackendKind | None = None,
    areas: Sequence[Area] = AREAS,
    needs: ToolchainNeeds | None = None,
) -> tuple[Capability, ...]:
    """Every row of *areas*, in :data:`AREAS` order; *backend* defaults to this host's.

    *needs* is what the image doctor checks builds with; ``None`` without an image.
    """
    rows: list[Capability] = []
    for area in AREAS:
        if area not in areas:
            continue
        if area == "build":
            rows.append(build_capability(host, backend or default_backend(host)))
            rows.append(go_capability(host, needs))
            rows.append(rustup_capability(host, needs))
        elif area == "run":
            rows.extend(run_capabilities(host))
        elif area == "deploy":
            rows.extend(deploy_capabilities(host))
        else:
            rows.extend(verify_capabilities())
    return tuple(rows)


def failed(rows: Sequence[Capability], *, areas: Sequence[Area] | None) -> bool:
    """Whether doctor exits 1: a selected area (``build`` when none was asked for) is blocked."""
    wanted = ("build",) if areas is None else tuple(areas)
    return any(row.verdict == "blocked" and row.area in wanted for row in rows)


__all__ = [
    "AREAS",
    "Area",
    "Capability",
    "Host",
    "ToolchainNeeds",
    "apt_install",
    "build_capability",
    "capabilities",
    "default_backend",
    "deploy_capabilities",
    "failed",
    "run_capabilities",
    "verify_capabilities",
]
