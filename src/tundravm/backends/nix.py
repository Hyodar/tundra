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
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tundravm.backends.base import (
    MountSpec,
    Requirement,
    build_sources_args,
    collect_artifacts,
    failure_message,
    mkosi_project,
    network_args,
    run_streaming,
    write_flake_nix,
)
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult


@dataclass(slots=True)
class NixMkosiBackend:
    """Native Nix backend — runs mkosi via ``nix develop`` on the host."""

    name: str = "nix_mkosi"
    mkosi_args: list[str] = field(default_factory=list)

    def requirements(self) -> tuple[Requirement, ...]:
        return (
            Requirement(
                tool="nix",
                probe=("nix", "--version"),
                hint="Install Nix with flakes enabled: https://nixos.org/download.html",
            ),
        )

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

        output_dir = request.build_dir.resolve() / request.profile / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        mkosi_dir = self._resolve_mkosi_dir(request)
        mkosi_cmd = self._build_mkosi_args(request, output_dir)

        if self._in_nix_shell():
            cmd = mkosi_cmd
        else:
            flake_ref = f"path:{request.emit_dir.resolve()}"
            cmd = ["nix", "develop", flake_ref, "-c", *mkosi_cmd]

        result = run_streaming(cmd, cwd=mkosi_dir, on_output=request.on_output)

        if result.returncode != 0:
            raise BackendExecutionError(
                failure_message(
                    "mkosi build failed via nix backend.",
                    result,
                    streamed=request.on_output is not None,
                ),
                hint="Check nix and mkosi output for details.",
                context={
                    "backend": self.name,
                    "operation": "execute",
                    "profile": request.profile,
                    "returncode": str(result.returncode),
                    "command": " ".join(cmd),
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
        """Return the (absolute) directory from which mkosi should be invoked."""
        return mkosi_project(request.emit_dir.resolve(), request.profile)[0]

    def _build_mkosi_args(self, request: BakeRequest, output_dir: Path) -> list[str]:
        mkosi_dir, native = mkosi_project(request.emit_dir.resolve(), request.profile)
        cmd = [
            "mkosi",
            f"--directory={mkosi_dir}",
            "--force",
            f"--image-id={request.profile}",
            f"--output-dir={output_dir.resolve()}",
        ]
        if request.sources_dir is not None:
            cmd.extend(
                build_sources_args(
                    mkosi_dir,
                    sources=str(request.sources_dir.resolve()),
                    config_dir=str(mkosi_dir),
                    mkosi_args=self.mkosi_args,
                )
            )
        cmd.extend(network_args(request))
        if native:
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
