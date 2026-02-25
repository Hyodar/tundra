"""Native Nix + mkosi build backend.

Runs mkosi on the host Linux system via ``nix develop``.  If the current
process is already inside a Nix shell (detected via ``IN_NIX_SHELL`` or
``NIX_STORE`` environment variables), mkosi is invoked directly.
Otherwise ``nix develop path:{emit_dir} -c mkosi ...`` wraps the
invocation so that mkosi and all build dependencies are provided by the
flake.

This backend requires:
- Linux host
- ``nix`` available in PATH with flakes enabled
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tundravm.backends.base import MountSpec, collect_artifacts, write_flake_nix
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult


@dataclass(slots=True)
class NixMkosiBackend:
    """Native Nix backend — runs mkosi via ``nix develop`` on the host."""

    name: str = "nix_mkosi"
    mkosi_args: list[str] = field(default_factory=list)

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        """Local mounts — both directories live on the host."""
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        self._ensure_prerequisites()
        request.build_dir.mkdir(parents=True, exist_ok=True)
        request.emit_dir.mkdir(parents=True, exist_ok=True)
        write_flake_nix(request.emit_dir)

    def execute(self, request: BakeRequest) -> BakeResult:
        self._ensure_prerequisites()

        output_dir = request.build_dir / request.profile / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        mkosi_dir = self._resolve_mkosi_dir(request)
        mkosi_cmd = self._build_mkosi_args(request, output_dir)

        if self._in_nix_shell():
            cmd = mkosi_cmd
        else:
            flake_ref = f"path:{request.emit_dir}"
            cmd = ["nix", "develop", flake_ref, "-c", *mkosi_cmd]

        result = subprocess.run(
            cmd,
            cwd=str(mkosi_dir),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise BackendExecutionError(
                "mkosi build failed via nix backend.",
                hint="Check nix and mkosi output for details.",
                context={
                    "backend": self.name,
                    "operation": "execute",
                    "profile": request.profile,
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[-2000:] if result.stderr else "",
                    "stdout": result.stdout[-2000:] if result.stdout else "",
                },
            )

        profile_result = ProfileBuildResult(profile=request.profile)
        profile_result.artifacts = collect_artifacts(output_dir)
        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _in_nix_shell() -> bool:
        """Return True if we are already inside a ``nix develop`` shell."""
        return bool(os.environ.get("IN_NIX_SHELL") or os.environ.get("NIX_STORE"))

    def _resolve_mkosi_dir(self, request: BakeRequest) -> Path:
        """Return the directory from which mkosi should be invoked."""
        native_profiles = request.emit_dir / "mkosi.profiles" / request.profile
        if native_profiles.exists():
            return request.emit_dir
        per_dir = request.emit_dir / request.profile
        return per_dir if per_dir.exists() else request.emit_dir

    def _build_mkosi_args(self, request: BakeRequest, output_dir: Path) -> list[str]:
        cmd = [
            "mkosi",
            "--force",
            f"--image-id={request.profile}",
            f"--output-dir={output_dir}",
        ]
        native_profiles = request.emit_dir / "mkosi.profiles" / request.profile
        if native_profiles.exists():
            cmd.append(f"--profile={request.profile}")
        cmd.extend(self.mkosi_args)
        cmd.append("build")
        return cmd

    def _ensure_prerequisites(self) -> None:
        if not sys.platform.startswith("linux"):
            raise BackendExecutionError(
                "Nix mkosi backend requires a Linux host.",
                hint="Use the Lima backend on macOS.",
                context={"backend": self.name, "operation": "prepare"},
            )
        if shutil.which("nix") is None:
            raise BackendExecutionError(
                "Nix mkosi backend requires `nix` in PATH.",
                hint="Install Nix: https://nixos.org/download.html",
                context={"backend": self.name, "operation": "prepare"},
            )
