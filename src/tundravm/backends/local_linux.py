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
from pathlib import Path
from typing import Literal

from tundravm.backends.base import (
    TOOLS_TREE_NAMES,
    MountSpec,
    Requirement,
    collect_artifacts,
    failure_message,
    mkosi_project,
    mkosi_setting,
    run_streaming,
)
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult

MINIMUM_MKOSI_VERSION = (25, 0)

STATE_DIRNAME = ".mkosi"
"""Build-directory subdirectory for mkosi's workspace, cache and tools tree."""

UKIFY_PATHS = ("/usr/lib/systemd/ukify",)
"""Where mkosi looks for ``ukify`` besides PATH."""

TOOLS_TREE_FIX = (
    'mkosi can use its own tools tree: add Setting("Build", "ToolsTree", ("default",)) '
    "to the recipe, or install {package}"
)
"""Hint for a host tool a default mkosi tools tree would provide."""


HOST_BUILD_TOOLS: tuple[Requirement, ...] = tuple(
    Requirement(
        tool=tool,
        probe=(tool, "--version"),
        hint=TOOLS_TREE_FIX.format(package=package),
        optional=True,
    )
    for tool, package in (
        ("ukify", "systemd-ukify"),
        ("systemd-repart", "systemd-repart"),
        ("apt", "apt (Debian and Ubuntu images)"),
    )
)
"""Host tools mkosi runs without a tools tree; the backend adds one when ``ukify`` is missing."""


def host_has_ukify() -> bool:
    """Whether mkosi would find ``ukify`` on this host."""
    return shutil.which("ukify") is not None or any(os.path.exists(p) for p in UKIFY_PATHS)


@dataclass(slots=True)
class LocalLinuxBackend:
    name: str = "local_linux"
    privilege: Literal["sudo", "unshare", "none"] = "sudo"
    mkosi_args: list[str] = field(default_factory=list)

    def requirements(self) -> tuple[Requirement, ...]:
        mkosi = Requirement(
            tool="mkosi",
            probe=("mkosi", "--version"),
            hint="Install mkosi v25+ (e.g. pip install mkosi) and put it on PATH.",
        )
        privilege: tuple[Requirement, ...] = ()
        if self.privilege == "sudo":
            privilege = (
                Requirement(
                    tool="sudo",
                    probe=("sudo", "--version"),
                    hint="Install sudo, or use LocalLinuxBackend(privilege='unshare').",
                    optional=os.getuid() == 0,
                ),
            )
        elif self.privilege == "unshare":
            privilege = (
                Requirement(
                    tool="unshare",
                    probe=("unshare", "--version"),
                    hint="Install util-linux for rootless `unshare --map-auto`.",
                ),
            )
        return (mkosi, *privilege, *HOST_BUILD_TOOLS)

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
        build_dir = request.build_dir.resolve()
        mkosi_dir, _ = mkosi_project(request.emit_dir.resolve(), request.profile)
        output_dir = build_dir / request.profile / "output"
        state_dir = build_dir / STATE_DIRNAME
        for path in (output_dir, state_dir / "workspace", state_dir / "cache"):
            path.mkdir(parents=True, exist_ok=True)

        cmd = self.command(request)
        try:
            result = run_streaming(cmd, cwd=mkosi_dir, on_output=request.on_output)
        finally:
            leftovers = tuple(mkosi_dir / name for name in TOOLS_TREE_NAMES)
            self._reclaim((output_dir, state_dir, *leftovers), request)
            self._stash_tools_tree(mkosi_dir, state_dir, request)

        if result.returncode != 0:
            raise BackendExecutionError(
                failure_message(
                    "mkosi build failed.", result, streamed=request.on_output is not None
                ),
                hint="Check mkosi output for details.",
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

    def command(self, request: BakeRequest) -> list[str]:
        """The mkosi command line for *request*; every path in it is absolute.

        mkosi's workspace and incremental cache go under ``<build_dir>/.mkosi/``
        unless the recipe sets them; a tools tree is added when the host lacks
        ``ukify`` (see :meth:`tools_tree`).
        """
        build_dir = request.build_dir.resolve()
        mkosi_dir, native = mkosi_project(request.emit_dir.resolve(), request.profile)
        state_dir = build_dir / STATE_DIRNAME
        # sudo resets PATH, so pass mkosi's absolute path
        cmd: list[str] = []
        if self.privilege == "unshare" and shutil.which("unshare"):
            cmd.extend(["unshare", "--map-auto", "--map-current-user"])
        elif self._uses_sudo():
            cmd.append("sudo")
        cmd.extend(
            [
                shutil.which("mkosi") or "mkosi",
                f"--directory={mkosi_dir}",
                "--force",
                f"--image-id={request.profile}",
                f"--output-dir={build_dir / request.profile / 'output'}",
            ]
        )
        if not self._sets(mkosi_dir, "WorkspaceDirectory", "--workspace-directory"):
            cmd.append(f"--workspace-directory={state_dir / 'workspace'}")
        if not self._sets(mkosi_dir, "CacheDirectory", "--cache-directory"):
            cmd.append(f"--cache-directory={state_dir / 'cache'}")
        tools = self.tools_tree(request)
        if tools is not None:
            cmd.append(f"--tools-tree={tools}")
        if native:
            cmd.append(f"--profile={request.profile}")
        cmd.extend([*self.mkosi_args, "build"])
        return cmd

    def tools_tree(self, request: BakeRequest) -> str | None:
        """The ``--tools-tree`` value to pass, or ``None`` to leave mkosi's choice alone.

        A recipe that sets no ``ToolsTree=`` on a host without ``ukify`` gets
        ``default``. ``ToolsTree=default`` reuses the tree an earlier bake into the
        same build directory left in ``.mkosi/mkosi.tools`` instead of rebuilding it.
        """
        build_dir = request.build_dir.resolve()
        mkosi_dir, _ = mkosi_project(request.emit_dir.resolve(), request.profile)
        if any(arg.startswith("--tools-tree") for arg in self.mkosi_args):
            return None
        configured = mkosi_setting(mkosi_dir, "ToolsTree")
        if configured is not None and configured not in ("default", "yes"):
            return None
        if configured is None and host_has_ukify():
            return None
        cached = build_dir / STATE_DIRNAME / "mkosi.tools"
        value = str(cached) if cached.is_dir() else "default"
        if configured is None:
            how = f"reusing {cached}" if value != "default" else "--tools-tree=default"
            request.notice(
                "info",
                f"ukify not found on the host; building with mkosi's default tools tree "
                f"({how}). Install systemd-ukify to use the host tools.",
            )
        return value

    def cleanup(self, request: BakeRequest) -> None:
        pass

    def _uses_sudo(self) -> bool:
        return self.privilege == "sudo" and os.getuid() != 0

    def _sets(self, mkosi_dir: Path, key: str, flag: str) -> bool:
        """Whether the recipe or ``mkosi_args`` already choose *key*."""
        passed = any(arg.startswith(flag) for arg in self.mkosi_args)
        return passed or mkosi_setting(mkosi_dir, key) is not None

    def _stash_tools_tree(self, mkosi_dir: Path, state_dir: Path, request: BakeRequest) -> None:
        """Move a tools tree mkosi built into the config directory out of the emitted tree.

        mkosi always writes ``ToolsTree=default`` next to its config; left there, the
        next compile could not replace the variant directory. Runs after
        :meth:`_reclaim`: moving a directory to another parent needs write access to it.
        """
        for name in TOOLS_TREE_NAMES:
            source = mkosi_dir / name
            if not os.path.lexists(source):
                continue
            target = state_dir / name
            try:
                if os.path.lexists(target):
                    self._remove(target)
                os.replace(source, target)
            except OSError as exc:
                request.notice("warning", f"could not move {source} to {target}: {exc}")

    def _remove(self, path: Path) -> None:
        if self._uses_sudo():
            subprocess.run(["sudo", "rm", "-rf", str(path)], capture_output=True, check=False)
        elif path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _reclaim(self, paths: tuple[Path, ...], request: BakeRequest) -> None:
        """Hand what a sudo mkosi run wrote back to the invoking user."""
        existing = [str(path) for path in paths if path.exists()]
        if not self._uses_sudo() or not existing:
            return
        owner = f"{os.getuid()}:{os.getgid()}"
        result = subprocess.run(
            ["sudo", "chown", "-R", owner, *existing],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            request.notice(
                "warning",
                f"could not chown build output to {owner}: "
                f"{detail[-1] if detail else 'exit ' + str(result.returncode)}; "
                f"run `sudo chown -R {owner} {request.build_dir}`",
            )

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
