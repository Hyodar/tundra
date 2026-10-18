"""The build toolchain: what ran mkosi, recorded in the lockfile and compared everywhere.

Two builders with the same lockfile should run identical tools. ``tundravm lock``
records the toolchain (:class:`Toolchain`) beside the recipe; ``lock --check`` and
``lint`` report ``toolchain-drift`` when the running one differs; a bake records the
toolchain it used in ``bake-result.json``, and ``evidence`` and ``witness`` compare
theirs field by field. The nix and lima backends pin nixpkgs to the locked rev and
run mkosi with a PATH of Nix tools only, so the lock decides the toolchain there.
"""

from __future__ import annotations

import functools
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, cast

from .diagnostics import Diagnostic, Severity
from .errors import LockfileError

MkosiSource = Literal["pip", "nix", "host"]
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
NIXPKGS_BRANCH = "nixos-unstable"
"""The nixpkgs branch ``tundravm lock`` resolves to a rev for the nix and lima backends."""
TOOLCHAIN_UPDATE = "toolchain"
"""The ``lock --update`` name that resolves the toolchain again instead of keeping the lock's."""
ROSETTA = "not guaranteed (rosetta)"
"""What a bake under Lima's Rosetta translation records as ``reproducible``."""


@dataclass(frozen=True, slots=True)
class MkosiTool:
    """The mkosi that runs: its *version*, where it comes from and, if known, its *rev*.

    *source* is ``nix`` (the pinned nixpkgs' mkosi), ``pip`` (installed in tundravm's
    Python environment) or ``host`` (found on PATH). *rev* is the nixpkgs rev for
    ``nix`` and the git commit of a pip install from git.
    """

    version: str
    source: MkosiSource
    rev: str | None = None

    def label(self) -> str:
        rev = f" @{self.rev[:12]}" if self.rev else ""
        return f"{self.version} ({self.source}{rev})"


@dataclass(frozen=True, slots=True)
class NixpkgsPin:
    """A nixpkgs revision and the ``narHash`` of its source tree (what a flake.lock holds)."""

    rev: str
    nar_hash: str
    last_modified: int = 0


@dataclass(frozen=True, slots=True)
class Toolchain:
    """What builds the image: tundravm, mkosi, systemd (ukify/systemd-boot), nixpkgs, measure tools.

    *tundravm* is ``<version>`` or ``<version>+<commit>``. *systemd* is the version
    of the ukify/systemd-boot mkosi uses. *nixpkgs* is set for the nix and lima
    backends. *measure* maps the built-in measurement replay and its GCP firmware table to
    their versions.
    *rosetta* is true for a Lima VM translating x86-64 with Rosetta, which makes the
    bake ``reproducible: not guaranteed (rosetta)``.
    """

    tundravm: str
    mkosi: MkosiTool | None = None
    systemd: str | None = None
    nixpkgs: NixpkgsPin | None = None
    measure: tuple[tuple[str, str], ...] = field(default=())
    rosetta: bool = False
    unpinned: str | None = None
    """Why a nix-sourced toolchain has no nixpkgs pin (offline lock, no nix on this host)."""

    @property
    def reproducible(self) -> str | None:
        """``not guaranteed (rosetta)`` under Rosetta, else ``None`` (nothing known against it)."""
        return ROSETTA if self.rosetta else None

    def payload(self) -> dict[str, Any]:
        """The JSON form the lockfile, ``bake-result.json``, evidence and witnesses hold."""
        out: dict[str, Any] = {"tundravm": self.tundravm}
        if self.mkosi is not None:
            mkosi: dict[str, Any] = {"version": self.mkosi.version, "source": self.mkosi.source}
            if self.mkosi.rev:
                mkosi["rev"] = self.mkosi.rev
            out["mkosi"] = mkosi
        if self.systemd is not None:
            out["systemd"] = self.systemd
        if self.nixpkgs is not None:
            out["nixpkgs"] = {
                "rev": self.nixpkgs.rev,
                "narHash": self.nixpkgs.nar_hash,
                "lastModified": self.nixpkgs.last_modified,
            }
        if self.measure:
            out["measure"] = dict(self.measure)
        if self.rosetta:
            out["rosetta"] = True
            out["reproducible"] = ROSETTA
        if self.unpinned:
            out["unpinned"] = self.unpinned
        return out

    @classmethod
    def from_payload(cls, raw: object) -> Toolchain:
        """Parse :meth:`payload`'s form; raise ``LockfileError`` on a malformed one."""
        if not isinstance(raw, Mapping) or not isinstance(raw.get("tundravm"), str):
            raise LockfileError(
                "Invalid lockfile `toolchain` value.",
                hint="The lockfile is generated: run `tundravm lock RECIPE` to rewrite it.",
            )
        mkosi = raw.get("mkosi")
        nix = raw.get("nixpkgs")
        measure = raw.get("measure") or {}
        return cls(
            tundravm=str(raw["tundravm"]),
            mkosi=None
            if not isinstance(mkosi, Mapping)
            else MkosiTool(
                version=str(mkosi.get("version", "")),
                source=cast(MkosiSource, str(mkosi.get("source", "host"))),
                rev=None if mkosi.get("rev") is None else str(mkosi["rev"]),
            ),
            systemd=None if raw.get("systemd") is None else str(raw["systemd"]),
            nixpkgs=None
            if not isinstance(nix, Mapping)
            else NixpkgsPin(
                rev=str(nix.get("rev", "")),
                nar_hash=str(nix.get("narHash", "")),
                last_modified=int(nix.get("lastModified", 0) or 0),
            ),
            measure=tuple(sorted((str(k), str(v)) for k, v in dict(measure).items())),
            rosetta=bool(raw.get("rosetta", False)),
            unpinned=None if raw.get("unpinned") is None else str(raw["unpinned"]),
        )

    def summary(self) -> str:
        """One line: ``mkosi 25.3 (nix @…), systemd 257, nixpkgs <rev12>, tundravm 0.1.0+…``."""
        parts = [f"mkosi {self.mkosi.label() if self.mkosi else 'none'}"]
        if self.systemd is not None:
            parts.append(f"systemd {self.systemd}")
        if self.nixpkgs is not None:
            parts.append(f"nixpkgs {self.nixpkgs.rev[:12]}")
        parts.extend(f"{tool} {version}" for tool, version in self.measure)
        parts.append(f"tundravm {self.tundravm}")
        if self.rosetta:
            parts.append(f"reproducible: {ROSETTA}")
        return ", ".join(parts)

    def fields(self) -> dict[str, str]:
        """One comparable label per recorded tool: ``mkosi``, ``systemd``, ``measure.<tool>``…"""
        out = {"tundravm": self.tundravm}
        if self.mkosi is not None:
            out["mkosi"] = self.mkosi.label()
        if self.systemd is not None:
            out["systemd"] = self.systemd
        if self.nixpkgs is not None:
            out["nixpkgs"] = self.nixpkgs.rev[:12]
        for tool, version in self.measure:
            out[f"measure.{tool}"] = version
        out["rosetta"] = "on" if self.rosetta else "off"
        return out


@dataclass(frozen=True, slots=True)
class ToolchainChange:
    """One field two toolchains disagree on; ``None`` on the side that does not record it."""

    field: str
    left: str | None
    right: str | None

    def render(self) -> str:
        return f"{self.field} {self.left or 'none'} vs {self.right or 'none'}"


def compare_toolchains(left: Toolchain, right: Toolchain) -> tuple[ToolchainChange, ...]:
    """Every field *left* and *right* disagree on, sorted by field name."""
    a, b = left.fields(), right.fields()
    return tuple(
        ToolchainChange(name, a.get(name), b.get(name))
        for name in sorted({*a, *b})
        if a.get(name) != b.get(name)
    )


def describe_changes(changes: Sequence[ToolchainChange]) -> str:
    """``same`` or ``different (mkosi 26 (nix) vs 25 (host), …)``."""
    if not changes:
        return "same"
    return "different (" + ", ".join(change.render() for change in changes) + ")"


def drift_severity(*, strict: bool) -> Severity:
    """``toolchain-drift`` is a warning, an error under ``strict_toolchain``/``--strict``."""
    return "error" if strict else "warning"


def toolchain_drift(
    locked: Toolchain, running: Toolchain, *, strict: bool = False
) -> tuple[Diagnostic, ...]:
    """One ``toolchain-drift`` diagnostic per field the running toolchain differs on."""
    return tuple(
        Diagnostic(
            "toolchain-drift",
            drift_severity(strict=strict),
            f"toolchain {change.field}: locked {change.left or 'none'}, "
            f"running {change.right or 'none'}",
            subject=f"toolchain.{change.field}",
            hint="Builders with this lockfile should run the locked tools; run "
            "`tundravm lock --update toolchain` to record this toolchain instead.",
        )
        for change in compare_toolchains(locked, running)
    )


# ── probes ───────────────────────────────────────────────────────────────

MEASURE_TOOLS = ("tundravm-measure", "gcp-firmware-table")
"""What ``toolchain.measure`` records: the built-in replay's version and the GCP firmware
table it embeds (both ship with tundravm, so a different version means other values)."""


def run_probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* with a 10s timeout, capturing output (what ``doctor`` runs)."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)


def _output(runner: ProbeRunner, argv: Sequence[str]) -> str | None:
    try:
        result = runner(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or result.stderr or "").strip() or None


def version_token(text: str) -> str:
    """The version in a ``--version`` line: ``mkosi 25.3`` → ``25.3``, ``systemd 257`` → ``257``."""
    words = text.splitlines()[0].split() if text else []
    for word in words:
        token = word.strip("(),")
        token = token[1:] if token[:1] == "v" and token[1:2].isdigit() else token
        if token[:1].isdigit():
            return token
    return words[-1] if words else ""


def _vcs_commit(distribution: str) -> str | None:
    """The git commit a pip install of *distribution* from a VCS url records, if any."""
    try:
        raw = metadata.distribution(distribution).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    try:
        info = json.loads(raw or "{}").get("vcs_info", {})
    except ValueError:
        return None
    commit = info.get("commit_id") if isinstance(info, dict) else None
    return str(commit) if commit else None


@functools.cache
def tundravm_identity() -> str:
    """``<version>+<commit>``: the commit from a VCS install or this source checkout's HEAD."""
    from . import __version__

    commit = _vcs_commit("tundravm")
    root = Path(__file__).resolve().parents[2]
    if commit is None and (root / ".git").exists():
        head = _output(run_probe, ("git", "-C", str(root), "rev-parse", "HEAD"))
        commit = head.splitlines()[0] if head else None
    return f"{__version__}+{commit[:12]}" if commit else __version__


def installed_version(distribution: str) -> str | None:
    """The version of *distribution* installed in tundravm's Python environment, if any."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def probe_mkosi(runner: ProbeRunner) -> MkosiTool | None:
    """mkosi from tundravm's Python environment (``pip``), else from PATH (``host``)."""
    version = installed_version("mkosi")
    if version is None:
        text = _output(runner, ("mkosi", "--version"))
        return None if text is None else MkosiTool(version_token(text), "host")
    return MkosiTool(version, "pip", _vcs_commit("mkosi"))


def probe_systemd(runner: ProbeRunner) -> str | None:
    """The host's ukify (else systemctl) version, which mkosi's UKI and boot loader come from."""
    for argv in (("ukify", "--version"), ("systemctl", "--version")):
        text = _output(runner, argv)
        if text is not None:
            return version_token(text)
    return None


def probe_measure(runner: ProbeRunner | None = None) -> tuple[tuple[str, str], ...]:
    """The measurement implementation: built into tundravm, so nothing is probed."""
    from .measure.gcp_firmware import TABLE_VERSION
    from .measure.model import MEASURE_VERSION

    return tuple(sorted(zip(MEASURE_TOOLS, (MEASURE_VERSION, TABLE_VERSION), strict=True)))


# ── nixpkgs ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NixpkgsResolution:
    """A resolved nixpkgs *pin* and the mkosi and systemd versions it provides."""

    pin: NixpkgsPin
    mkosi: str
    systemd: str


NixpkgsResolver = Callable[[], NixpkgsResolution]
"""Resolves the current :data:`NIXPKGS_BRANCH` (tests inject one; the default runs nix)."""


def _run_nix(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=600, check=False)


def resolve_nixpkgs(runner: ProbeRunner = _run_nix) -> NixpkgsResolution:
    """Resolve :data:`NIXPKGS_BRANCH` to a rev + narHash and read its mkosi and systemd versions.

    Needs ``nix`` with flakes on this host and the network; raises ``LockfileError``
    saying why otherwise.
    """
    ref = f"github:NixOS/nixpkgs/{NIXPKGS_BRANCH}"
    text = _output(runner, ("nix", "flake", "metadata", "--json", ref))
    try:
        locked = json.loads(text or "").get("locked", {})
        pin = NixpkgsPin(str(locked["rev"]), str(locked["narHash"]), int(locked["lastModified"]))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LockfileError(
            f"Could not resolve nixpkgs {NIXPKGS_BRANCH} to pin the toolchain.",
            hint="The nix and lima backends pin nixpkgs: run `tundravm lock` where `nix` "
            "(with flakes) and the network are available, or keep the previous lock's pin.",
        ) from exc
    versions = []
    for attr in ("mkosi", "systemd"):
        out = _output(
            runner, ("nix", "eval", "--raw", f"github:NixOS/nixpkgs/{pin.rev}#{attr}.version")
        )
        versions.append(out or "unknown")
    return NixpkgsResolution(pin, versions[0], versions[1])


def running_toolchain(
    backend: object,
    *,
    runner: ProbeRunner | None = None,
    locked: Toolchain | None = None,
    resolver: NixpkgsResolver | None = None,
) -> Toolchain:
    """The toolchain *backend* runs with: pinned from *locked*, resolved, or probed on the host.

    A backend whose ``toolchain_source`` is ``nix`` (nix, lima) runs the nixpkgs the
    lock pins, so *locked*'s nixpkgs, mkosi and systemd are what runs; without one,
    *resolver* resolves nixpkgs now (``None``: unresolved, mkosi follows the branch).
    ``host`` probes mkosi and ukify on this host; a backend without one (in-process)
    runs no mkosi. The measurement implementation ships with tundravm.
    """
    run = runner if runner is not None else run_probe
    source = getattr(backend, "toolchain_source", None)
    mkosi: MkosiTool | None = None
    systemd: str | None = None
    nixpkgs: NixpkgsPin | None = None
    if source == "nix":
        if locked is not None and locked.nixpkgs is not None:
            nixpkgs, mkosi, systemd = locked.nixpkgs, locked.mkosi, locked.systemd
        elif resolver is not None:
            found = resolver()
            nixpkgs, systemd = found.pin, found.systemd
            mkosi = MkosiTool(found.mkosi, "nix", found.pin.rev)
        else:
            mkosi = MkosiTool(NIXPKGS_BRANCH, "nix")
    elif source == "host":
        mkosi, systemd = probe_mkosi(run), probe_systemd(run)
    return Toolchain(
        tundravm=tundravm_identity(),
        mkosi=mkosi,
        systemd=systemd,
        nixpkgs=nixpkgs,
        measure=probe_measure(run),
        rosetta=bool(getattr(backend, "rosetta", False)),
    )


def _default_resolver() -> NixpkgsResolution:
    return resolve_nixpkgs()


def lock_toolchain(
    backend: object,
    *,
    previous: Toolchain | None = None,
    update: bool = False,
    offline: bool = False,
    runner: ProbeRunner | None = None,
    resolver: NixpkgsResolver | None = None,
) -> Toolchain:
    """The toolchain ``tundravm lock`` records for *backend*.

    A nix-sourced backend keeps *previous*'s nixpkgs pin unless *update*; else it
    resolves :data:`NIXPKGS_BRANCH` now (*resolver*, default :func:`resolve_nixpkgs`).
    When that is impossible (*offline*, no nix here) the toolchain records why it is
    ``unpinned`` and the build follows the branch. Other backends probe this host.
    """
    keep = None if update else previous
    if getattr(backend, "toolchain_source", None) != "nix" or (
        keep is not None and keep.nixpkgs is not None
    ):
        return running_toolchain(backend, runner=runner, locked=keep)
    if offline:
        reason = "offline lock: nixpkgs was not resolved"
        return replace(running_toolchain(backend, runner=runner), unpinned=reason)
    try:
        return running_toolchain(
            backend, runner=runner, resolver=resolver if resolver is not None else _default_resolver
        )
    except LockfileError as exc:
        reason = f"{exc.args[0]} {exc.hint or ''}".strip()
        return replace(running_toolchain(backend, runner=runner), unpinned=reason)
