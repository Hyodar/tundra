"""``tundravm doctor``: what this host can do, one independent capability per row.

Each row is ``<area>:<what>`` and a verdict:

- ``ready`` (with what it will use),
- ``blocked: <why>: <fix>`` when something installable or configurable is missing,
- ``unavailable: <why>`` when this host cannot do it (no TDX hardware),
- ``unsupported: <why>`` when tundravm does not do it anywhere.

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
from .deploy.qemu import OVMF_CODE_PATHS, TDVF_PATHS
from .measure.gcp_firmware import TABLE_VERSION
from .measure.model import MEASURE_VERSION
from .toolchain import MKOSI_PIN, installed_version

Area = Literal["build", "run", "deploy", "verify"]
AREAS: tuple[Area, ...] = ("build", "run", "deploy", "verify")
Verdict = Literal["ready", "blocked", "unavailable", "unsupported"]
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
BuildBackendKind = Literal["local", "lima", "inprocess"]


@dataclass(frozen=True, slots=True)
class Capability:
    """One row: *name* (``build:x86-64``), its *verdict* and the *detail* after it."""

    name: str
    verdict: Verdict
    detail: str = ""

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
    if backend == "inprocess":
        return Capability(name, "ready", "simulated: the inprocess backend builds nothing")
    if backend == "lima":
        return _lima_build(name, host)
    return _linux_build(name, host)


def _lima_build(name: str, host: Host) -> Capability:
    if not host.has("limactl"):
        return Capability(name, "blocked", "limactl not found: install Lima (lima-vm.io)")
    if host.machine in ("x86_64", "AMD64"):
        return Capability(name, "ready", f"Lima x86-64 guest, mkosi {MKOSI_PIN.version}")
    return Capability(
        name,
        "ready",
        f"Lima x86-64 guest under QEMU TCG emulation on this {host.machine} host: "
        f"same bytes, several times slower; mkosi {MKOSI_PIN.version}",
    )


def _linux_build(name: str, host: Host) -> Capability:
    if host.system != "Linux":
        return Capability(
            name,
            "blocked",
            f"this is {host.system}: build with the Lima backend (TUNDRAVM_BACKEND=lima)",
        )
    if not host.linux_x86_64:
        return Capability(
            name,
            "blocked",
            f"tundravm builds x86-64 images on x86-64 Linux; this is {host.machine}: "
            "build with the Lima backend (TUNDRAVM_BACKEND=lima)",
        )
    version = host.mkosi_version()
    if version != MKOSI_PIN.version:
        found = "no mkosi" if version is None else f"mkosi {version}"
        return Capability(
            name,
            "blocked",
            f"{found} in tundravm's environment, it needs {MKOSI_PIN.version}: "
            "reinstall tundravm (`uv sync` or `pip install`)",
        )
    for tool, fix in (("apt-get", "install apt"), ("dpkg", "install dpkg")):
        if host.output(tool, "--version") is None:
            return Capability(
                name, "blocked", f"no {tool} (mkosi bootstraps its tools tree with it): {fix}"
            )
    if host.output("test", "-e", DEBIAN_KEYRING) is None:
        return Capability(
            name, "blocked", "no Debian archive keyring: install debian-archive-keyring"
        )
    privilege = _privilege(host)
    if privilege is None:
        return Capability(name, "blocked", _userns_fix(host))
    return Capability(name, "ready", f"mkosi {MKOSI_PIN.version}, {privilege}")


def _privilege(host: Host) -> str | None:
    """How mkosi gets privileges here, or ``None`` when it cannot."""
    if host.uid == 0:
        return "root"
    if host.userns():
        return "user namespaces"
    if host.has("sudo"):
        return "sudo (no unprivileged user namespaces here)"
    return None


def _userns_fix(host: Host) -> str:
    """Why this host gives no user namespace, and the fix, from the kernel's knobs."""
    reason = "no unprivileged user namespaces and no sudo"
    if host.read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") == "1":
        return f"{reason}: run `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`"
    if host.read("/proc/sys/kernel/unprivileged_userns_clone") == "0":
        return f"{reason}: run `sysctl -w kernel.unprivileged_userns_clone=1`"
    if host.read("/proc/sys/user/max_user_namespaces") == "0":
        return f"{reason}: run `sysctl -w user.max_user_namespaces=65536`"
    return f"{reason}: allow user namespaces (container or LSM policy), or install sudo"


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


def _run_simulated(host: Host) -> Capability:
    name = "run:simulated"
    qemu = _qemu(host)
    if qemu is None:
        return Capability(name, "blocked", "qemu-system-x86_64 not found: install qemu-system-x86")
    firmware = next((path for path in OVMF_CODE_PATHS if host.exists(path)), None)
    if firmware is None:
        return Capability(name, "blocked", "no OVMF firmware: install ovmf")
    accel = "KVM" if host.system == "Linux" and host.kvm() else "TCG emulation, slow"
    return Capability(name, "ready", f"qemu {qemu}, OVMF {firmware}, {accel}")


def _run_tdx(host: Host) -> Capability:
    name = "run:tdx"
    enabled = host.read("/sys/module/kvm_intel/parameters/tdx")
    if host.system != "Linux" or enabled not in ("Y", "1"):
        return Capability(name, "unavailable", "no Intel TDX host (kvm_intel tdx is not enabled)")
    if _qemu(host) is None:
        return Capability(name, "blocked", "qemu-system-x86_64 not found: install qemu-system-x86")
    firmware = next((path for path in TDVF_PATHS if host.exists(path)), None)
    if firmware is None:
        return Capability(name, "blocked", "no TDVF firmware: install ovmf with TDX support")
    return Capability(name, "ready", f"TDVF {firmware}")


# ── deploy ───────────────────────────────────────────────────────────────


def deploy_capabilities(host: Host) -> tuple[Capability, ...]:
    """``deploy:gcp`` (the gcloud CLI and an account) and ``deploy:azure`` (az, logged in)."""
    return (_deploy_gcp(host), _deploy_azure(host))


def _deploy_gcp(host: Host) -> Capability:
    name = "deploy:gcp"
    if not host.has("gcloud"):
        return Capability(name, "blocked", "gcloud not found: install the Google Cloud CLI")
    account = host.output(
        "gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"
    )
    if not account:
        return Capability(name, "blocked", "gcloud has no active account: run `gcloud auth login`")
    return Capability(name, "ready", f"gcloud as {account}")


def _deploy_azure(host: Host) -> Capability:
    name = "deploy:azure"
    if not host.has("az"):
        return Capability(name, "blocked", "az not found: install the Azure CLI")
    account = host.output("az", "account", "show", "--query", "user.name", "--output", "tsv")
    if not account:
        return Capability(name, "blocked", "az is not logged in: run `az login`")
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
    host: Host, *, backend: BuildBackendKind | None = None, areas: Sequence[Area] = AREAS
) -> tuple[Capability, ...]:
    """Every row of *areas*, in :data:`AREAS` order; *backend* defaults to this host's."""
    rows: list[Capability] = []
    for area in AREAS:
        if area not in areas:
            continue
        if area == "build":
            rows.append(build_capability(host, backend or default_backend(host)))
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
    "build_capability",
    "capabilities",
    "default_backend",
    "deploy_capabilities",
    "failed",
    "run_capabilities",
    "verify_capabilities",
]
