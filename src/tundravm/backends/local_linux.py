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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from tundravm.backends.base import (
    TOOLS_TREE_NAMES,
    MountSpec,
    Requirement,
    build_sources_args,
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


PEFILE = Requirement(
    tool="pefile",
    probe=("python3", "-c", "import pefile; print(pefile.__version__)"),
    hint="install python3-pefile, or let mkosi use its tools tree "
    '(Setting("Build", "ToolsTree", ("default",)))',
    optional=True,
)
"""Python's ``pefile``: mkosi reads the installed kernel with it, directory builds included.

``requirements`` probes it with the interpreter that runs mkosi (:func:`mkosi_python`)."""


HOST_BUILD_TOOLS: tuple[Requirement, ...] = (
    *(
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
    ),
    PEFILE,
)
"""Host tools mkosi runs without a tools tree; the backend adds one when ``ukify`` or
``pefile`` is missing and the build needs it."""

HOST_TOOL_PACKAGES = {"ukify": "systemd-ukify", "pefile": "python3-pefile"}
"""The package that provides each host tool a missing one of which adds a tools tree."""


CLOUD_IMAGE_TOOLS: tuple[tuple[str, Requirement], ...] = (
    (
        "azure",
        Requirement(
            tool="qemu-img",
            probe=("qemu-img", "--version"),
            hint="The azure variant converts its disk to a VHD with it: "
            "install qemu-utils, or bake --variant default",
            optional=True,
        ),
    ),
    (
        "gcp",
        Requirement(
            tool="sgdisk",
            probe=("sgdisk", "--version"),
            hint="The gcp variant partitions its disk with it: "
            "install gdisk, or bake --variant default",
            optional=True,
        ),
    ),
)
"""(target, tool) for the commands the azure and gcp postoutput scripts run besides mtools."""


def cloud_tools(targets: Iterable[str]) -> tuple[Requirement, ...]:
    """The cloud postoutput tools *targets* need, once each, in table order."""
    wanted = set(targets)
    return tuple(req for target, req in CLOUD_IMAGE_TOOLS if target in wanted)


CLOUD_TOOLS_TREE_PACKAGES = ("qemu-utils", "gdisk", "parted")
"""What a default tools tree gets for the azure and gcp postoutput scripts, which run in it."""
CLOUD_TOOLS_TREE_BINARIES = ("qemu-img", "sgdisk", "parted")
"""The commands of :data:`CLOUD_TOOLS_TREE_PACKAGES` a reused tools tree must hold."""


def tree_has_tools(tree: Path, tools: Iterable[str]) -> bool:
    """Whether the tools tree at *tree* ships every one of *tools*."""
    return all(
        any(os.path.lexists(tree / "usr" / sub / tool) for sub in ("bin", "sbin")) for tool in tools
    )


UKIFY_FORMATS = frozenset({"uki", "esp"})
"""Output formats mkosi always builds a UKI for."""

DISABLED = frozenset({"no", "false", "0", "off", "disabled"})
ENABLED = frozenset({"yes", "true", "1", "on", "enabled"})


def _last_arg(args: list[str], *names: str) -> str | None:
    """The value the last of *names* gets in *args* (``--name=value`` or ``--name value``)."""
    value: str | None = None
    for index, arg in enumerate(args):
        for name in names:
            if arg.startswith(f"{name}="):
                value = arg.split("=", 1)[1]
            elif arg == name and index + 1 < len(args):
                value = args[index + 1]
    return value


def needs_ukify(mkosi_dir: Path, mkosi_args: list[str]) -> bool:
    """Whether the build makes a UKI: ``Format=uki``/``esp``, or ``Bootable=yes``.

    A ``Bootable=auto`` disk only uses ``ukify`` when the host has it, and a
    ``Bootable=no`` or directory build never does, so neither needs a tools tree.
    """
    fmt = _last_arg(mkosi_args, "--format", "-t") or mkosi_setting(mkosi_dir, "Format") or "disk"
    if fmt.lower() in UKIFY_FORMATS:
        return True
    bootable = _last_arg(mkosi_args, "--bootable") or mkosi_setting(mkosi_dir, "Bootable")
    return (bootable or "auto").lower() in ENABLED


def installs_kernel(mkosi_dir: Path, mkosi_args: list[str]) -> bool:
    """Whether mkosi may read an installed kernel with pefile: any build but ``Bootable=no``."""
    bootable = _last_arg(mkosi_args, "--bootable") or mkosi_setting(mkosi_dir, "Bootable")
    return (bootable or "auto").lower() not in DISABLED


def host_has_ukify() -> bool:
    """Whether mkosi would find ``ukify`` on this host."""
    return shutil.which("ukify") is not None or any(os.path.exists(p) for p in UKIFY_PATHS)


def mkosi_python() -> str:
    """The interpreter that runs mkosi: its script's shebang, else ``python3``."""
    path = shutil.which("mkosi")
    try:
        with open(path or "", "rb") as script:
            first = script.readline(256)
    except OSError:
        return "python3"
    words = first[2:].decode(errors="replace").split() if first.startswith(b"#!") else []
    if words and os.path.basename(words[0]) == "env":
        words = words[1:]
    return words[0] if words and "python" in os.path.basename(words[0]) else "python3"


def host_has_pefile() -> bool:
    """Whether the Python that runs mkosi on the host imports ``pefile``."""
    try:
        result = subprocess.run(
            [mkosi_python(), "-c", "import pefile"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def missing_host_tools(mkosi_dir: Path, mkosi_args: list[str]) -> tuple[str, ...]:
    """The host tools the build needs and lacks that mkosi's default tools tree provides."""
    missing: list[str] = []
    if needs_ukify(mkosi_dir, mkosi_args) and not host_has_ukify():
        missing.append("ukify")
    if installs_kernel(mkosi_dir, mkosi_args) and not host_has_pefile():
        missing.append("pefile")
    return tuple(missing)


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
        tools = tuple(
            replace(req, probe=(mkosi_python(), *req.probe[1:])) if req is PEFILE else req
            for req in HOST_BUILD_TOOLS
        )
        return (mkosi, *privilege, *tools)

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        self._ensure_local_prerequisites()
        self._ensure_cloud_tools(request)
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
        ``ukify`` (see :meth:`tools_tree`). Host-fetched sources
        (``request.sources_dir``) are mounted with :func:`build_sources_args`.
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
            if tools == "default" and cloud_tools(request.output_targets):
                cmd.append(f"--tools-tree-package={','.join(CLOUD_TOOLS_TREE_PACKAGES)}")
        if request.sources_dir is not None:
            cmd.extend(
                build_sources_args(
                    mkosi_dir,
                    sources=str(request.sources_dir.resolve()),
                    config_dir=str(mkosi_dir),
                    mkosi_args=self.mkosi_args,
                )
            )
        if native:
            cmd.append(f"--profile={request.profile}")
        cmd.extend([*self.mkosi_args, "build"])
        return cmd

    def tools_tree(self, request: BakeRequest) -> str | None:
        """The ``--tools-tree`` value to pass, or ``None`` to leave mkosi's choice alone.

        A recipe that sets no ``ToolsTree=`` gets ``default`` when the host lacks a
        tool the build needs (:func:`missing_host_tools`: ``ukify`` for a UKI,
        ``pefile`` for any bootable build); other builds keep the host tools.
        ``ToolsTree=default`` reuses the tree an earlier bake into the same build
        directory left in ``.mkosi/mkosi.tools`` instead of rebuilding it, unless
        an azure or gcp variant needs the cloud tools that tree lacks; ``command``
        then adds them with ``--tools-tree-package``.
        """
        value, message = self._tools_tree_choice(request)
        if message is not None:
            request.notice("info", message)
        return value

    def _tools_tree_choice(self, request: BakeRequest) -> tuple[str | None, str | None]:
        """:meth:`tools_tree`'s value and the notice that explains it, if any."""
        build_dir = request.build_dir.resolve()
        mkosi_dir, _ = mkosi_project(request.emit_dir.resolve(), request.profile)
        if any(arg.startswith("--tools-tree") for arg in self.mkosi_args):
            return None, None
        configured = mkosi_setting(mkosi_dir, "ToolsTree")
        if configured is not None and configured not in ("default", "yes"):
            return None, None
        missing = () if configured is not None else missing_host_tools(mkosi_dir, self.mkosi_args)
        if configured is None and not missing:
            return None, None
        cached = build_dir / STATE_DIRNAME / "mkosi.tools"
        cloud = bool(cloud_tools(request.output_targets))
        reuse = cached.is_dir() and (not cloud or tree_has_tools(cached, CLOUD_TOOLS_TREE_BINARIES))
        value = str(cached) if reuse else "default"
        message: str | None = None
        if configured is None:
            how = f"reusing {cached}" if reuse else "--tools-tree=default"
            install = " and ".join(HOST_TOOL_PACKAGES[tool] for tool in missing)
            message = (
                f"{' and '.join(missing)} not found on the host; building with mkosi's default "
                f"tools tree ({how}). Install {install} to use the host tools."
            )
        return value, message

    def _scripts_in_tools_tree(self, request: BakeRequest) -> bool:
        """Whether mkosi runs this build's scripts in a tools tree, not on the host."""
        passed = _last_arg(self.mkosi_args, "--tools-tree")
        if passed is not None:
            return passed.lower() not in DISABLED
        mkosi_dir, _ = mkosi_project(request.emit_dir.resolve(), request.profile)
        configured = mkosi_setting(mkosi_dir, "ToolsTree")
        if configured is not None:
            return configured.lower() not in DISABLED
        return self._tools_tree_choice(request)[0] is not None

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

    def _ensure_cloud_tools(self, request: BakeRequest) -> None:
        """Fail before mkosi runs when a cloud postoutput script would miss its tool.

        Only for host-tools builds: in a tools tree the scripts run there, and a
        default one gets :data:`CLOUD_TOOLS_TREE_PACKAGES`.
        """
        required = cloud_tools(request.output_targets)
        if not required or self._scripts_in_tools_tree(request):
            return
        for requirement in required:
            if shutil.which(requirement.tool) is None:
                raise BackendExecutionError(
                    f"Variant {request.profile!r} needs `{requirement.tool}` on the host "
                    "for its cloud disk image.",
                    hint=requirement.hint,
                    context={
                        "backend": self.name,
                        "operation": "prepare",
                        "profile": request.profile,
                        "tool": requirement.tool,
                    },
                )

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
