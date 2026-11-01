"""The build toolchain: what ran mkosi, recorded in the lockfile and compared everywhere.

Two builders with the same lockfile should run identical tools. mkosi is a dependency of
tundravm, pinned to a release tag (:data:`MKOSI_PIN`), and every build tool besides it
(systemd, ukify, systemd-boot, kmod, qemu, OVMF, pefile) comes from mkosi's tools tree
(``ToolsTree=default``), whose distribution, release and snapshot are pinned explicitly.
``tundravm lock`` records the toolchain (:class:`Toolchain`) beside the recipe; ``lock
--check`` and ``check`` report ``toolchain-drift`` when the running one differs; a
build records the lock's in its manifest (``inputs/toolchain.json``).
"""

from __future__ import annotations

import functools
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from .diagnostics import Diagnostic
from .errors import LockfileError

TOOLCHAIN_UPDATE = "toolchain"
"""The ``lock --update`` name that records the running toolchain instead of keeping the lock's."""


@dataclass(frozen=True, slots=True)
class MkosiPin:
    """The mkosi release tundravm depends on: a git tag of *repository* (mkosi is not on
    PyPI) and the *commit* it resolves to, which ``uv.lock`` records too."""

    version: str
    repository: str
    tag: str
    commit: str

    @property
    def requirement(self) -> str:
        """A pip requirement for exactly this commit."""
        return f"mkosi @ git+{self.repository}@{self.commit}"


MKOSI_PIN = MkosiPin(
    version="27.1",
    repository="https://github.com/systemd/mkosi",
    tag="v27.1",
    commit="ecf7f5702bb2052b7a69cab3fe7d37bc3b9ad77b",
)
"""The mkosi every backend runs: the dependency in ``pyproject.toml`` locally, the same
commit inside the Lima guest."""


@dataclass(frozen=True, slots=True)
class MkosiTool:
    """The mkosi that runs: its *version* and, when installed from git, the commit (*rev*)."""

    version: str
    rev: str | None = None

    def label(self) -> str:
        rev = f" @{self.rev[:12]}" if self.rev else ""
        return f"{self.version}{rev}"


@dataclass(frozen=True, slots=True)
class ToolsTree:
    """mkosi's tools tree: the *distribution*, *release* and *snapshot* every build tool
    (systemd, ukify, kmod, qemu, OVMF, pefile) is installed from."""

    distribution: str
    release: str
    snapshot: str | None = None

    def label(self) -> str:
        at = f"@{self.snapshot}" if self.snapshot else " (unpinned snapshot)"
        return f"{self.distribution}/{self.release}{at}"

    @classmethod
    def from_summary(cls, tools: Mapping[str, Any] | None) -> ToolsTree | None:
        """The tools tree of ``mkosi summary --json``'s ``Tools`` config, if it has one."""
        if not tools or not tools.get("Distribution"):
            return None
        snapshot = tools.get("Snapshot")
        return cls(
            distribution=str(tools["Distribution"]),
            release=str(tools.get("Release") or ""),
            snapshot=None if not snapshot else str(snapshot),
        )


@dataclass(frozen=True, slots=True)
class Toolchain:
    """What builds the image: tundravm, mkosi, the tools tree and the measurement tools.

    *tundravm* is ``<version>`` or ``<version>+<commit>``. *tools_tree* is the tools tree
    the build installs its tools from (``None`` when unknown: a lock written before the tree
    was resolved). *measure* maps the built-in measurement replay and its GCP firmware table
    to their versions.
    """

    tundravm: str
    mkosi: MkosiTool | None = None
    tools_tree: ToolsTree | None = None
    measure: tuple[tuple[str, str], ...] = field(default=())

    def payload(self) -> dict[str, Any]:
        """The JSON form the lockfile and a build's ``inputs/toolchain.json`` hold."""
        out: dict[str, Any] = {"tundravm": self.tundravm}
        if self.mkosi is not None:
            mkosi: dict[str, Any] = {"version": self.mkosi.version}
            if self.mkosi.rev:
                mkosi["rev"] = self.mkosi.rev
            out["mkosi"] = mkosi
        if self.tools_tree is not None:
            out["tools_tree"] = {
                "distribution": self.tools_tree.distribution,
                "release": self.tools_tree.release,
                "snapshot": self.tools_tree.snapshot,
            }
        if self.measure:
            out["measure"] = dict(self.measure)
        return out

    @classmethod
    def from_payload(cls, raw: object) -> Toolchain:
        """Parse :meth:`payload`'s form; raise ``LockfileError`` on a malformed one."""
        if not isinstance(raw, Mapping) or not isinstance(raw.get("tundravm"), str):
            raise LockfileError(
                "Invalid lockfile `toolchain` value.",
                hint="The lockfile is generated: run `tundravm lock IMAGE` to rewrite it.",
            )
        mkosi = raw.get("mkosi")
        tree = raw.get("tools_tree")
        measure = raw.get("measure") or {}
        return cls(
            tundravm=str(raw["tundravm"]),
            mkosi=None
            if not isinstance(mkosi, Mapping)
            else MkosiTool(
                version=str(mkosi.get("version", "")),
                rev=None if mkosi.get("rev") is None else str(mkosi["rev"]),
            ),
            tools_tree=None
            if not isinstance(tree, Mapping)
            else ToolsTree(
                distribution=str(tree.get("distribution", "")),
                release=str(tree.get("release", "")),
                snapshot=None if tree.get("snapshot") is None else str(tree["snapshot"]),
            ),
            measure=tuple(sorted((str(k), str(v)) for k, v in dict(measure).items())),
        )

    def summary(self) -> str:
        """One line: ``mkosi 27.1 @ecf7f5702bb2, tools debian/trixie@…, tundravm 0.1.0+…``."""
        parts = [f"mkosi {self.mkosi.label() if self.mkosi else 'none'}"]
        if self.tools_tree is not None:
            parts.append(f"tools {self.tools_tree.label()}")
        parts.extend(f"{tool} {version}" for tool, version in self.measure)
        parts.append(f"tundravm {self.tundravm}")
        return ", ".join(parts)

    def fields(self) -> dict[str, str]:
        """One comparable label per recorded tool: ``mkosi``, ``tools_tree``, ``measure.<tool>``…"""
        out = {"tundravm": self.tundravm}
        if self.mkosi is not None:
            out["mkosi"] = self.mkosi.label()
        if self.tools_tree is not None:
            out["tools_tree"] = self.tools_tree.label()
        for tool, version in self.measure:
            out[f"measure.{tool}"] = version
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


def toolchain_drift(locked: Toolchain, running: Toolchain) -> tuple[Diagnostic, ...]:
    """One ``toolchain-drift`` warning per field the running toolchain differs on."""
    return tuple(
        Diagnostic(
            "toolchain-drift",
            "warning",
            f"toolchain {change.field}: locked {change.left or 'none'}, "
            f"running {change.right or 'none'}",
            subject=f"toolchain.{change.field}",
            hint="Builders with this lockfile should run the locked tools; run "
            "`tundravm lock --update toolchain` to record this toolchain instead.",
        )
        for change in compare_toolchains(locked, running)
    )


# ── what runs ────────────────────────────────────────────────────────────

MEASURE_TOOLS = ("tundravm-measure", "gcp-firmware-table")
"""What ``toolchain.measure`` records: the built-in replay's version and the GCP firmware
table it embeds (both ship with tundravm, so a different version means other values)."""


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = result.stdout.strip() if result.returncode == 0 else ""
    return head.splitlines()[0] if head else None


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
        commit = _git_head(root)
    return f"{__version__}+{commit[:12]}" if commit else __version__


def installed_version(distribution: str) -> str | None:
    """The version of *distribution* installed in tundravm's Python environment, if any."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def installed_mkosi() -> MkosiTool | None:
    """The mkosi in tundravm's Python environment (the dependency), with its git commit."""
    version = installed_version("mkosi")
    return None if version is None else MkosiTool(version, _vcs_commit("mkosi"))


def pinned_mkosi() -> MkosiTool:
    """:data:`MKOSI_PIN` as the :class:`MkosiTool` a backend that provisions it runs."""
    return MkosiTool(MKOSI_PIN.version, MKOSI_PIN.commit)


def probe_measure() -> tuple[tuple[str, str], ...]:
    """The measurement implementation: built into tundravm, so nothing is probed."""
    from .measure.gcp_firmware import TABLE_VERSION
    from .measure.model import MEASURE_VERSION

    return tuple(sorted(zip(MEASURE_TOOLS, (MEASURE_VERSION, TABLE_VERSION), strict=True)))


def running_toolchain(backend: object, *, locked: Toolchain | None = None) -> Toolchain:
    """The toolchain *backend* runs with.

    mkosi is what the backend says it runs (``mkosi_tool``: the installed dependency
    locally, the pinned commit in a Lima guest; a backend without one runs no mkosi). The
    tools tree is the one the backend resolved before its last build (``tools_tree``, from
    ``mkosi summary --json``), else *locked*'s. The measurement implementation ships with
    tundravm.
    """
    mkosi = getattr(backend, "mkosi_tool", None)
    tree = getattr(backend, "tools_tree", None)
    if not isinstance(tree, ToolsTree):
        tree = None if locked is None else locked.tools_tree
    return Toolchain(
        tundravm=tundravm_identity(),
        mkosi=mkosi if isinstance(mkosi, MkosiTool) else None,
        tools_tree=tree,
        measure=probe_measure(),
    )


def lock_toolchain(
    backend: object,
    *,
    previous: Toolchain | None = None,
    update: bool = False,
    tools_tree: ToolsTree | None = None,
) -> Toolchain:
    """The toolchain ``tundravm lock`` records for *backend*.

    *tools_tree* is the tools tree the image's mkosi tree pins; without one, *previous*'s
    (unless *update*) is kept.
    """
    keep = None if update else previous
    found = running_toolchain(backend, locked=keep)
    if tools_tree is not None:
        return Toolchain(found.tundravm, found.mkosi, tools_tree, found.measure)
    return found
