"""Native Nix + mkosi build backend.

Runs mkosi on the host Linux system inside ``nix develop path:{emit_dir}``, the
flake pinned to the lockfile's nixpkgs (whose mkosi version the flake guards). It
enters that shell even when the current process is already inside another Nix
shell (``IN_NIX_SHELL``/``NIX_STORE``): an ambient mkosi is never run. After the
build it asks the same shell which mkosi it resolved (the ``/nix/store`` executable
and its ``--version``), refuses one that differs from the locked version, and
keeps that identity as :attr:`NixMkosiBackend.executed_mkosi`, which the bake
records as the toolchain that ran.

This backend requires:
- Linux host
- ``nix`` available in PATH with flakes enabled
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from tundravm.backends.base import (
    MKOSI_IDENTITY_SCRIPT,
    MountSpec,
    Requirement,
    build_sources_args,
    collect_artifacts,
    failure_message,
    mkosi_project,
    network_args,
    pinned_mkosi,
    run_streaming,
    write_flake_nix,
)
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult
from tundravm.toolchain import MkosiTool


def _capture(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run *argv* in *cwd*, capturing its output (the identity probe; tests replace it)."""
    return subprocess.run(
        list(argv), cwd=str(cwd), capture_output=True, text=True, timeout=600, check=False
    )


@dataclass(slots=True)
class NixMkosiBackend:
    """Native Nix backend — runs mkosi via ``nix develop`` on the host."""

    name: str = "nix_mkosi"
    mkosi_args: list[str] = field(default_factory=list)
    toolchain_source: ClassVar[str] = "nix"
    """mkosi comes from the flake's nixpkgs, pinned to the lockfile's toolchain."""
    executed_mkosi: MkosiTool | None = field(default=None, init=False, repr=False, compare=False)
    """The mkosi the last :meth:`execute` ran (resolved store path, version); ``None`` before."""

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
        write_flake_nix(request.emit_dir, request.toolchain)

    def execute(self, request: BakeRequest) -> BakeResult:
        self._ensure_prerequisites()

        output_dir = request.build_dir.resolve() / request.profile / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        mkosi_dir = self._resolve_mkosi_dir(request)
        mkosi_cmd = self._build_mkosi_args(request, output_dir)

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

        self.executed_mkosi = self._observe_mkosi(request, flake_ref, mkosi_dir)
        profile_result = ProfileBuildResult(profile=request.profile)
        profile_result.artifacts = collect_artifacts(output_dir)
        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        pass

    def fingerprint(self) -> dict[str, object]:
        """The backend name and the extra mkosi arguments; mkosi comes from the flake."""
        return {"name": self.name, "mkosi_args": list(self.mkosi_args)}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _observe_mkosi(self, request: BakeRequest, flake_ref: str, cwd: Path) -> MkosiTool:
        """The mkosi the pinned shell *flake_ref* runs: its resolved executable and version.

        ``BackendExecutionError`` when the shell cannot say, resolves mkosi outside the Nix
        store, or runs a version other than the one the lockfile pins.
        """
        argv = ["nix", "develop", flake_ref, "-c", "sh", "-c", MKOSI_IDENTITY_SCRIPT]
        context = {
            "backend": self.name,
            "operation": "execute",
            "profile": request.profile,
            "command": " ".join(argv),
        }
        hint = f"Run `nix develop {flake_ref} -c mkosi --version` to see what the shell provides."
        try:
            probe = _capture(argv, cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendExecutionError(
                f"Could not identify the mkosi the pinned Nix shell runs: {exc}.",
                hint=hint,
                context=context,
            ) from exc
        return pinned_mkosi(probe, request, context=context, hint=hint)

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
