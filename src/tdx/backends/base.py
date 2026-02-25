"""Protocol for bake execution backends."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tdx.models import ArtifactRef, BakeRequest, BakeResult, OutputTarget


@dataclass(frozen=True, slots=True)
class MountSpec:
    source: Path
    target: str
    read_only: bool = False


class BuildBackend(Protocol):
    name: str

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        """Return deterministic host/guest mount mapping for this request."""

    def prepare(self, request: BakeRequest) -> None:
        """Prepare backend runtime resources."""

    def execute(self, request: BakeRequest) -> BakeResult:
        """Run bake request and return artifacts."""

    def cleanup(self, request: BakeRequest) -> None:
        """Release backend runtime resources."""


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
