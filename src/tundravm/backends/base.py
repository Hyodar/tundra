"""Protocol for bake execution backends."""

from __future__ import annotations

import os
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


def collect_artifacts(output_dir: Path) -> dict[OutputTarget, ArtifactRef]:
    """Scan *output_dir* for mkosi build artifacts."""
    artifacts: dict[OutputTarget, ArtifactRef] = {}
    if not output_dir.exists():
        return artifacts

    for efi in sorted(output_dir.glob("*.efi*")):
        artifacts["qemu"] = ArtifactRef(target="qemu", path=efi)
        break

    for raw in sorted(output_dir.glob("*.raw*")):
        if "qemu" not in artifacts:
            artifacts["qemu"] = ArtifactRef(target="qemu", path=raw)
        break

    for qcow2 in sorted(output_dir.glob("*.qcow2*")):
        artifacts["qemu"] = ArtifactRef(target="qemu", path=qcow2)
        break

    for vhd in sorted(output_dir.glob("*.vhd*")):
        artifacts["azure"] = ArtifactRef(target="azure", path=vhd)
        break

    for tar_gz in sorted(output_dir.glob("*.tar.gz*")):
        artifacts["gcp"] = ArtifactRef(target="gcp", path=tar_gz)
        break

    return artifacts
