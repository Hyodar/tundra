"""Native Linux build execution via mkosi.

Invokes mkosi directly on the host.  By default uses ``sudo`` for privilege
escalation (mkosi needs root or user-namespace support).  Set
``privilege="unshare"`` to use rootless ``unshare --map-auto`` instead, or
``privilege="none"`` to run mkosi as the current user (only works as root).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Literal

from tundravm.backends.base import MountSpec, collect_artifacts
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult

MINIMUM_MKOSI_VERSION = (25, 0)


@dataclass(slots=True)
class LocalLinuxBackend:
    name: str = "local_linux"
    privilege: Literal["sudo", "unshare", "none"] = "sudo"
    mkosi_args: list[str] = field(default_factory=list)

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        self._ensure_local_prerequisites()
        for mount in self.mount_plan(request):
            mount.source.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BakeRequest) -> BakeResult:
        self._ensure_local_prerequisites()

        # Determine the mkosi project directory for this profile.
        # Native profiles mode: mkosi.profiles/<name>/ under emit_dir root
        native_profiles_dir = request.emit_dir / "mkosi.profiles" / request.profile
        if native_profiles_dir.exists():
            # Native profiles mode: cwd is the emission root
            mkosi_dir = request.emit_dir
        else:
            # Per-directory mode: cwd is the profile subdirectory
            mkosi_dir = request.emit_dir / request.profile
            if not mkosi_dir.exists():
                mkosi_dir = request.emit_dir

        output_dir = request.build_dir / request.profile / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build the mkosi command with privilege escalation
        # Resolve absolute path so sudo (which resets PATH) can find it
        mkosi_bin = shutil.which("mkosi") or "mkosi"
        cmd: list[str] = []
        if self.privilege == "unshare" and shutil.which("unshare"):
            cmd.extend(["unshare", "--map-auto", "--map-current-user"])
        elif self.privilege == "sudo" and os.getuid() != 0:
            cmd.append("sudo")

        cmd.extend(
            [
                mkosi_bin,
                "--force",
                f"--image-id={request.profile}",
                f"--output-dir={output_dir}",
            ]
        )

        # In native profiles mode, use --profile flag
        if native_profiles_dir.exists():
            cmd.append(f"--profile={request.profile}")

        cmd.extend(
            [
                *self.mkosi_args,
                "build",
            ]
        )

        result = subprocess.run(
            cmd,
            cwd=str(mkosi_dir),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise BackendExecutionError(
                "mkosi build failed.",
                hint="Check mkosi output for details.",
                context={
                    "backend": self.name,
                    "operation": "execute",
                    "profile": request.profile,
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[:2000] if result.stderr else "",
                    "command": " ".join(cmd),
                },
            )

        # Collect output artifacts
        profile_result = ProfileBuildResult(profile=request.profile)
        profile_result.artifacts = collect_artifacts(output_dir)

        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        pass

    def _ensure_local_prerequisites(self) -> None:
        if not sys.platform.startswith("linux"):
            raise BackendExecutionError(
                "Local Linux backend requires a Linux host.",
                hint="Use the Lima backend (default) instead.",
                context={"backend": self.name, "operation": "prepare"},
            )
        if shutil.which("mkosi") is None:
            raise BackendExecutionError(
                "Local Linux backend requires `mkosi` in PATH.",
                hint="Install mkosi and ensure it is available before running bake.",
                context={"backend": self.name, "operation": "prepare"},
            )
        self._check_mkosi_version()

    def _check_mkosi_version(self) -> None:
        """Verify mkosi version meets the minimum requirement."""
        result = subprocess.run(
            ["mkosi", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return  # Can't determine version, let mkosi itself fail later
        version_str = result.stdout.strip()
        # mkosi --version outputs something like "mkosi 25.3" or just "25.3"
        parts = version_str.replace("mkosi", "").strip().split(".")
        try:
            version_tuple = tuple(int(p) for p in parts[:2])
        except (ValueError, IndexError):
            return
        if version_tuple < MINIMUM_MKOSI_VERSION:
            raise BackendExecutionError(
                f"mkosi version {version_str} is below minimum "
                f"{'.'.join(str(v) for v in MINIMUM_MKOSI_VERSION)}.",
                hint="Upgrade mkosi: pip install --break-system-packages mkosi",
                context={
                    "backend": self.name,
                    "operation": "prepare",
                    "version": version_str,
                    "minimum": ".".join(str(v) for v in MINIMUM_MKOSI_VERSION),
                },
            )
