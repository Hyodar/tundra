"""Protocol for bake execution backends."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tundravm.models import ArtifactRef, BakeRequest, BakeResult, OutputTarget


@dataclass(frozen=True, slots=True)
class MountSpec:
    source: Path
    target: str
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class Requirement:
    """A host tool a backend needs; ``probe`` is run by ``tundravm doctor``."""

    tool: str
    probe: tuple[str, ...]
    hint: str
    optional: bool = False


class BuildBackend(Protocol):
    name: str

    def requirements(self) -> tuple[Requirement, ...]:
        """Host tools this backend needs (optional; ``doctor`` treats a missing method as none)."""

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        """Return deterministic host/guest mount mapping for this request."""

    def prepare(self, request: BakeRequest) -> None:
        """Prepare backend runtime resources."""

    def execute(self, request: BakeRequest) -> BakeResult:
        """Run bake request and return artifacts."""

    def cleanup(self, request: BakeRequest) -> None:
        """Release backend runtime resources."""


# ---------------------------------------------------------------------------
# Streaming subprocess execution
# ---------------------------------------------------------------------------

OUTPUT_TAIL_LINES = 20
"""Output lines a failed command keeps for its error message."""


@dataclass(frozen=True, slots=True)
class StreamResult:
    returncode: int
    tail: tuple[str, ...]


def run_streaming(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    on_output: Callable[[str], None] | None = None,
    tail_lines: int = OUTPUT_TAIL_LINES,
) -> StreamResult:
    """Run *argv* with stderr merged into stdout, passing each line to *on_output* as it arrives.

    The child is terminated if reading or the callback raises (e.g. Ctrl-C).
    """
    tail: deque[str] = deque(maxlen=tail_lines)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        list(argv),
        cwd=None if cwd is None else str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    stdout = process.stdout
    assert stdout is not None
    try:
        for raw in iter(stdout.readline, ""):
            line = raw.rstrip("\r\n")
            tail.append(line)
            if on_output is not None:
                on_output(line)
        returncode = process.wait()
    except BaseException:
        _terminate(process)
        raise
    finally:
        stdout.close()
    return StreamResult(returncode=returncode, tail=tuple(tail))


def failure_message(message: str, result: StreamResult, *, streamed: bool) -> str:
    """*message* with the exit code, plus the output tail unless ``on_output`` already saw it."""
    text = f"{message} (exit {result.returncode})"
    if streamed or not result.tail:
        return text
    body = "\n".join(f"  | {line}" for line in result.tail)
    return f"{text}\nLast output:\n{body}"


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# ---------------------------------------------------------------------------
# Shared utilities for backends that use mkosi
# ---------------------------------------------------------------------------

FLAKE_NIX_TEMPLATE = textwrap.dedent("""\
    {
      inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

      outputs = {
        self,
        nixpkgs,
      }: let
        mkosi = system: let
          pkgs = import nixpkgs {inherit system;};
          mkosi-unwrapped = pkgs.mkosi.override {
            extraDeps = with pkgs; [
              apt
              dpkg
              gnupg
              debootstrap
              squashfsTools
              dosfstools
              e2fsprogs
              mtools
              cryptsetup
              gptfdisk
              util-linux
              zstd
              which
              qemu-utils
              parted
              unzip
              jq
            ];
          };
        in
          pkgs.writeShellScriptBin "mkosi" ''
            exec ${"$"}{pkgs.util-linux}/bin/unshare \\
              --map-auto --map-current-user \\
              --setuid=0 --setgid=0 \\
              -- \\
              env PATH="$PATH" \\
              ${"$"}{mkosi-unwrapped}/bin/mkosi "$@"
          '';
      in {
        devShells = builtins.listToAttrs (map (system: {
          name = system;
          value.default = (import nixpkgs {inherit system;}).mkShell {
            nativeBuildInputs = [(mkosi system)];
            shellHook = ''
              mkdir -p mkosi.cache mkosi.builddir
            '';
          };
        }) ["x86_64-linux" "aarch64-linux"]);
      };
    }
""")


def write_flake_nix(target_dir: Path) -> Path:
    """Write the mkosi Nix flake to *target_dir* and return its path."""
    flake_path = target_dir / "flake.nix"
    flake_path.write_text(FLAKE_NIX_TEMPLATE, encoding="utf-8")
    return flake_path


MKOSI_STATE_NAMES: frozenset[str] = frozenset(
    {
        ".mkosi-private",
        "mkosi.builddir",
        "mkosi.cache",
        "mkosi.output",
        "mkosi.pkgcache",
        "mkosi.tools",
        "mkosi.tools.build.cache",
        "mkosi.tools.manifest",
    }
)
"""Paths mkosi creates or reads next to its config; they are build state, never tree content."""

TOOLS_TREE_NAMES = ("mkosi.tools", "mkosi.tools.build.cache", "mkosi.tools.manifest")
"""What ``ToolsTree=default`` leaves in the mkosi config directory."""


def is_mkosi_state(rel: str) -> bool:
    """Whether *rel* (relative to an emitted tree) is mkosi build state.

    Only the tree root and its variant directories hold mkosi config, so deeper
    paths with these names (e.g. inside ``mkosi.extra/``) are content.
    """
    parts = Path(rel).parts
    return len(parts) <= 2 and parts[-1] in MKOSI_STATE_NAMES


def mkosi_project(emit_dir: Path, profile: str) -> tuple[Path, bool]:
    """The directory mkosi builds *profile* from, and whether it needs ``--profile``.

    Native profiles (``mkosi.profiles/<name>/``) build from *emit_dir*; per-directory
    trees from ``<emit_dir>/<profile>/``, else *emit_dir* itself.
    """
    if (emit_dir / "mkosi.profiles" / profile).exists():
        return emit_dir, True
    per_dir = emit_dir / profile
    return (per_dir if per_dir.exists() else emit_dir), False


def mkosi_setting(project: Path, key: str) -> str | None:
    """The value the mkosi config in *project* gives *key* last, or ``None`` if unset."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", re.MULTILINE)
    candidates = [
        project / "mkosi.conf",
        *sorted(project.glob("mkosi.conf.d/**/*.conf")),
        *sorted(project.glob("mkosi.profiles/**/*.conf")),
        project / "mkosi.local.conf",
    ]
    value: str | None = None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in pattern.finditer(text):
            value = match.group(1)
    return value


def build_sources_args(
    project: Path, *, sources: str, config_dir: str, mkosi_args: Sequence[str] = ()
) -> list[str]:
    """mkosi flags that mount the host-fetched checkouts *sources* at ``$SRCDIR/tundravm-sources``.

    *project* is the mkosi directory on this host; *config_dir* and *sources*
    are paths as mkosi sees them. Any ``--build-sources`` replaces mkosi's
    default (the config directory at ``$SRCDIR``, which build scripts run in),
    so it is passed again unless the recipe or *mkosi_args* set ``BuildSources=``.
    ``BuildSourcesEphemeral=yes`` (unless set) puts both behind an overlay: the
    mount point inside the config directory and anything a script writes there
    never reach the host.
    """
    from tundravm._source import SOURCES_MOUNT

    def chosen(key: str, flag: str) -> bool:
        passed = any(arg.startswith(flag) for arg in mkosi_args)
        return passed or mkosi_setting(project, key) is not None

    args: list[str] = []
    if not chosen("BuildSources", "--build-sources="):
        args.append(f"--build-sources={config_dir}")
    args.append(f"--build-sources={sources}:{SOURCES_MOUNT}")
    if not chosen("BuildSourcesEphemeral", "--build-sources-ephemeral"):
        args.append("--build-sources-ephemeral=yes")
    return args


OFFLINE_ARGS = ("--with-network=no",)
"""The mkosi flag of an offline bake: build and postinst scripts get no network.

It overrides the emitted ``WithNetwork=``; mkosi itself still installs the
distribution packages from the configured mirror."""


def network_args(request: BakeRequest) -> list[str]:
    """:data:`OFFLINE_ARGS` when *request* bakes offline, else nothing."""
    return [] if request.network else list(OFFLINE_ARGS)


def collect_artifacts(output_dir: Path) -> dict[OutputTarget, ArtifactRef]:
    """Scan *output_dir* for mkosi build artifacts (files only; unreadable dirs yield none)."""
    artifacts: dict[OutputTarget, ArtifactRef] = {}
    try:
        names = sorted(p for p in output_dir.iterdir() if p.is_file())
    except OSError:
        return artifacts

    def first(pattern: str) -> Path | None:
        return next((p for p in names if p.match(pattern)), None)

    if (efi := first("*.efi*")) is not None:
        artifacts["qemu"] = ArtifactRef(target="qemu", path=efi)
    if "qemu" not in artifacts and (raw := first("*.raw*")) is not None:
        artifacts["qemu"] = ArtifactRef(target="qemu", path=raw)
    if (qcow2 := first("*.qcow2*")) is not None:
        artifacts["qemu"] = ArtifactRef(target="qemu", path=qcow2)
    if (vhd := first("*.vhd*")) is not None:
        artifacts["azure"] = ArtifactRef(target="azure", path=vhd)
    if (tar_gz := first("*.tar.gz*")) is not None:
        artifacts["gcp"] = ArtifactRef(target="gcp", path=tar_gz)
    return artifacts
