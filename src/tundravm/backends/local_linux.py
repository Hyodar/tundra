"""Linux x86-64 builds: the pinned mkosi, run in-process through :mod:`tundravm._mkosi`.

mkosi is a dependency of tundravm, so nothing is looked up on ``PATH``: every verb runs
through the adapter, which forks with mkosi's own ``fork_and_wait`` and lets mkosi acquire
a user namespace in the child. Every build tool besides mkosi comes from mkosi's tools tree
(the tree sets ``ToolsTree=default`` and pins it), so the host needs only Python, apt/dpkg
with the Debian archive keyring (mkosi bootstraps the Debian tools tree with them) and
either unprivileged user namespaces or ``sudo``.

``privilege="auto"`` (the default) runs mkosi unprivileged when this host can give it a user
namespace and under ``sudo -E <python> -m tundravm._mkosi`` when it cannot (a kernel,
container or LSM policy forbids them; Ubuntu 24.04's
``kernel.apparmor_restrict_unprivileged_userns=1`` does); ``privilege="none"`` never uses
sudo and lets mkosi fail with its own explanation.

Before each build the backend runs ``mkosi --force summary --json`` with the same arguments
(unprivileged) and keeps the resolved configuration beside the output
(``<build>/<variant>/mkosi-summary.json``); the tools tree it names is what the bake records
in its toolchain.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tundravm._reporting import format_size
from tundravm.backends.base import (
    BOOT_PAYLOAD_NAME,
    BUILD_DIRNAME,
    INCREMENTAL_DIRNAME,
    MountSpec,
    Requirement,
    build_sources_args,
    cache_manifests,
    cache_stamp,
    cache_use,
    collect_artifacts,
    incremental_args,
    mkosi_project,
    run_streaming,
)
from tundravm.compiler.emit_mkosi import UKI_MAX_SIZE, esp_bytes, uki_capacity
from tundravm.errors import BackendExecutionError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult
from tundravm.toolchain import MkosiTool, ToolsTree, installed_mkosi

if TYPE_CHECKING:
    from tundravm._mkosi import MkosiSummary

STATE_DIRNAME = ".mkosi"
"""Build-directory subdirectory mkosi runs in: its workspace, cache and tools tree."""

CACHE_DIRNAME = "cache"
"""``<build>/.mkosi/cache``: a production build's mkosi cache directory, repository metadata
only; a development build's is the sibling :data:`~tundravm.backends.base.INCREMENTAL_DIRNAME`."""


def package_cache() -> Path:
    """Where every build on this host keeps the packages mkosi downloads:
    ``$XDG_CACHE_HOME/tundravm/packages`` (``~/.cache`` without it), shared by builds with and
    without sudo. apt verifies each package against the snapshot's signed index before use."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tundravm" / "packages"


LOG_FILENAME = "mkosi.log"
"""What mkosi logged for a variant's build, beside its ``output/``."""

SUMMARY_FILENAME = "mkosi-summary.json"
"""``mkosi summary --json`` of a variant, as resolved right before its build."""

DEBIAN_KEYRING = "/usr/share/keyrings/debian-archive-keyring.gpg"
"""The Debian archive keyring mkosi verifies the tools tree's packages with."""

Privilege = Literal["auto", "none"]


HOST_BUILD_TOOLS: tuple[Requirement, ...] = (
    Requirement(
        tool="apt",
        probe=("apt-get", "--version"),
        hint="mkosi bootstraps its Debian tools tree with apt: install apt",
    ),
    Requirement(
        tool="dpkg",
        probe=("dpkg", "--version"),
        hint="mkosi bootstraps its Debian tools tree with dpkg: install dpkg",
    ),
    Requirement(
        tool="debian-archive-keyring",
        probe=("test", "-e", DEBIAN_KEYRING),
        hint="install debian-archive-keyring (it verifies the tools tree's packages)",
    ),
)
"""What the host provides besides Python and its kernel; everything else is in the tools tree."""


@dataclass(slots=True)
class LocalLinuxBackend:
    """Build on this Linux x86-64 host with the mkosi tundravm depends on."""

    name: str = "local_linux"
    privilege: Privilege = "auto"
    mkosi_args: list[str] = field(default_factory=list)
    tools_tree: ToolsTree | None = field(default=None, init=False, repr=False, compare=False)
    """The tools tree the last build resolved (``mkosi summary --json``'s ``Tools``)."""

    @property
    def mkosi_tool(self) -> MkosiTool | None:
        """The mkosi this backend runs: the one installed with tundravm."""
        return installed_mkosi()

    def requirements(self) -> tuple[Requirement, ...]:
        return HOST_BUILD_TOOLS

    def fingerprint(self) -> dict[str, object]:
        """The backend name, ``privilege``, ``mkosi_args`` and the mkosi it runs."""
        mkosi = self.mkosi_tool
        return {
            "name": self.name,
            "privilege": self.privilege,
            "mkosi_args": list(self.mkosi_args),
            "mkosi": None if mkosi is None else mkosi.label(),
        }

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        self._ensure_linux()
        for mount in self.mount_plan(request):
            mount.source.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BakeRequest) -> BakeResult:
        self._ensure_linux()
        build_dir = request.build_dir.resolve()
        profile_dir = build_dir / request.profile
        output_dir = profile_dir / "output"
        state_dir = build_dir / STATE_DIRNAME
        cache_dir = self.cache_dir(request)
        for path in (output_dir, state_dir / "workspace", cache_dir, package_cache()):
            path.mkdir(parents=True, exist_ok=True)

        argv = self.arguments(request)
        sudo = self.uses_sudo()
        manifests: dict[str, Path] = {}
        before: dict[str, tuple[int, int] | None] = {}
        try:
            resolved = self.summarize(request, argv)
            self.tools_tree = ToolsTree.from_summary(resolved.tools)
            (profile_dir / SUMMARY_FILENAME).write_text(
                json.dumps({"Tools": resolved.tools, "Images": list(resolved.images)}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            if request.cache_key is not None:
                manifests = cache_manifests(cache_dir, resolved.images, request.cache_key)
                before = {name: cache_stamp(path) for name, path in manifests.items()}
            if sudo:
                request.notice(
                    "info",
                    "this host gives no unprivileged user namespaces: running mkosi with sudo",
                )
            try:
                self.run_mkosi(
                    [*argv, "build"],
                    cwd=state_dir,
                    log=profile_dir / LOG_FILENAME,
                    on_output=request.on_output,
                    sudo=sudo,
                )
            except BackendExecutionError as exc:
                overflow = esp_overflow(
                    output_dir / BOOT_PAYLOAD_NAME, profile_dir / LOG_FILENAME, request
                )
                if overflow is None:
                    raise
                raise overflow from exc
        finally:
            if sudo:
                reclaimed = (
                    output_dir,
                    state_dir / "workspace",
                    state_dir / CACHE_DIRNAME,
                    package_cache(),
                    profile_dir / LOG_FILENAME,
                )
                self._reclaim(reclaimed, request)

        profile_result = ProfileBuildResult(profile=request.profile)
        profile_result.artifacts = collect_artifacts(output_dir, request.output_targets)
        if request.cache_key is not None:
            profile_result.cache = cache_use(cache_dir, request.cache_key, manifests, before)
        return BakeResult(profiles={request.profile: profile_result})

    def cache_dir(self, request: BakeRequest) -> Path:
        """mkosi's cache directory for *request*: ``<build_dir>/.mkosi/cache`` (repository
        metadata) for a build from scratch, ``<build_dir>/.mkosi/incremental`` (the cached
        base trees too) for an incremental one. A production build's request names no
        ``cache_key``, so the incremental directory is never on its command line; under sudo
        it also stays root-owned, as mkosi reuses a cached tree only from its own uid."""
        state_dir = request.build_dir.resolve() / STATE_DIRNAME
        return state_dir / (CACHE_DIRNAME if request.cache_key is None else INCREMENTAL_DIRNAME)

    def arguments(self, request: BakeRequest) -> list[str]:
        """mkosi's arguments for *request*, before the verb; every path in them is absolute.

        Only paths and the selection: the tree carries every mkosi setting. mkosi runs in
        ``<build_dir>/.mkosi`` (where ``ToolsTree=default`` puts the tools tree, so later
        bakes reuse it), with its workspace and cache (:meth:`cache_dir`) there too; the
        output goes to ``<build_dir>/<variant>/output``. A request with a ``cache_key`` adds
        :func:`incremental_args`. Host-fetched sources (``request.sources_dir``)
        are mounted with :func:`build_sources_args`; ``request.mkosi_args`` selects the
        variant, then the backend's own ``mkosi_args`` follow.
        """
        build_dir = request.build_dir.resolve()
        mkosi_dir = mkosi_project(request.emit_dir.resolve())
        state_dir = build_dir / STATE_DIRNAME
        argv = [
            f"--directory={mkosi_dir}",
            "--force",
            f"--output-directory={build_dir / request.profile / 'output'}",
            f"--workspace-directory={state_dir / 'workspace'}",
            f"--cache-directory={self.cache_dir(request)}",
            f"--package-cache-directory={package_cache()}",
        ]
        if request.cache_key is not None:
            build = self.cache_dir(request) / BUILD_DIRNAME
            argv.extend(incremental_args(request.cache_key, str(build)))
        if request.sources_dir is not None:
            argv.extend(
                build_sources_args(
                    mkosi_dir,
                    sources=str(request.sources_dir.resolve()),
                    config_dir=str(mkosi_dir),
                    mkosi_args=self.mkosi_args,
                )
            )
        argv.extend(request.mkosi_args)
        argv.extend(self.mkosi_args)
        return argv

    def summarize(self, request: BakeRequest, argv: Sequence[str]) -> MkosiSummary:
        """``mkosi summary --json`` with *argv*: what the build will resolve to."""
        from tundravm import _mkosi

        state_dir = request.build_dir.resolve() / STATE_DIRNAME
        log = request.build_dir.resolve() / request.profile / "mkosi-summary.log"
        try:
            return _mkosi.parse_summary(
                self.run_mkosi(
                    [*argv, "--no-pager", "summary", "--json"],
                    cwd=state_dir,
                    log=log,
                    on_output=_discard,
                    stdout=True,
                )
            )
        except ValueError as exc:
            raise BackendExecutionError(
                f"mkosi summary --json printed no JSON: {exc}.",
                hint=f"See {log}.",
                context={"backend": self.name, "profile": request.profile},
            ) from exc

    def run_mkosi(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        log: Path,
        on_output: Callable[[str], None] | None,
        stdout: bool = False,
        sudo: bool = False,
    ) -> str:
        """Run one mkosi verb (in-process, or as root through sudo); return its stdout.

        ``BackendExecutionError`` (with the log's tail unless *on_output* saw it) when
        mkosi fails.
        """
        from tundravm import _mkosi

        try:
            if sudo:
                return self._run_sudo(argv, cwd=cwd, log=log, on_output=on_output, stdout=stdout)
            sink = on_output if on_output is not None else _discard
            return _mkosi.invoke(argv, cwd=cwd, log=log, on_output=sink, stdout=stdout).stdout
        except _mkosi.MkosiError as exc:
            verb = next((arg for arg in reversed(argv) if not arg.startswith("-")), "mkosi")
            tail = "" if on_output is not None else _tail(exc.log_text)
            raise BackendExecutionError(
                f"mkosi {verb} failed (exit {exc.returncode}).{tail}",
                hint=f"mkosi's log is {log}.",
                context={
                    "backend": self.name,
                    "returncode": str(exc.returncode),
                    "command": " ".join(["mkosi", *argv]),
                },
            ) from exc

    def _run_sudo(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        log: Path,
        on_output: Callable[[str], None] | None,
        stdout: bool,
    ) -> str:
        """``sudo -E <python> -m tundravm._mkosi … -- ARGV``, streamed; its outcome from the
        result file it writes."""
        from tundravm import _mkosi

        with tempfile.TemporaryDirectory(prefix="tundravm-sudo-") as scratch:
            result = Path(scratch) / "result.json"
            command = _mkosi.sudo_argv(argv, cwd=cwd, log=log, result=result, stdout=stdout)
            streamed = run_streaming(command, on_output=on_output)
            outcome = _mkosi.read_result(result)
        if outcome is None:
            text = _read(log) or "\n".join(streamed.tail)
            raise _mkosi.MkosiError(argv, text, streamed.returncode or 1)
        if outcome.returncode != 0:
            raise _mkosi.MkosiError(argv, _read(log), outcome.returncode)
        return outcome.stdout

    def uses_sudo(self) -> bool:
        """Whether mkosi runs under sudo: ``privilege="auto"`` on a host without user
        namespaces for this unprivileged process (:func:`tundravm._mkosi.needs_sudo`)."""
        if self.privilege == "none":
            return False
        from tundravm import _mkosi

        return _mkosi.needs_sudo()

    def cleanup(self, request: BakeRequest) -> None:
        pass

    def _reclaim(self, paths: tuple[Path, ...], request: BakeRequest) -> None:
        """Hand what a sudo mkosi run wrote back to the invoking user."""
        existing = [str(path) for path in paths if path.exists()]
        if not existing:
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

    def _ensure_linux(self) -> None:
        if not sys.platform.startswith("linux") or platform.machine() not in ("x86_64", "AMD64"):
            raise BackendExecutionError(
                f"The Linux backend builds on an x86-64 Linux host; this is "
                f"{sys.platform}/{platform.machine()}.",
                hint="Use the Lima backend (Backend('lima')) on macOS and other hosts.",
                context={"backend": self.name, "operation": "prepare"},
            )
        if shutil.which("sudo") is None and self.uses_sudo():
            raise BackendExecutionError(
                "mkosi needs a user namespace or root, and this host gives neither: "
                "unprivileged user namespaces are off and sudo is not installed.",
                hint="Allow unprivileged user namespaces (on Ubuntu: `sysctl "
                "kernel.apparmor_restrict_unprivileged_userns=0`), or install sudo.",
                context={"backend": self.name, "operation": "prepare"},
            )


def esp_overflow(uki: Path, log: Path, request: BakeRequest) -> BackendExecutionError | None:
    """The error a failed ``mkosi build`` is when the payload subimage left a UKI *uki* larger
    than the ESP every disk of *request*'s tree holds (``emit_mkosi.esp_bytes``): mkosi got
    as far as copying it onto the disk, which failed. ``None`` for any other failure."""
    try:
        size = uki.stat().st_size
    except OSError:
        return None
    esp = esp_bytes(request.root, request.uki_max_size)
    capacity = uki_capacity(esp)
    if size <= capacity:
        return None
    if request.root == "verity":
        hint = (
            "The verity ESP is a fixed size for the stub, the kernel and the initrd: make the "
            "UKI smaller with fewer kernel modules or initrd contents."
        )
    else:
        hint = (
            f"Raise the UKI's ceiling with RootFS.initramfs(uki_max_size=...) (now "
            f"{request.uki_max_size or UKI_MAX_SIZE}), which sizes the ESP; or keep the OS out "
            'of the UKI with rootfs=RootFS.verity(filesystem="erofs"), or make the payload '
            "smaller with fewer packages in Image(packages=...)."
        )
    return BackendExecutionError(
        f"The UKI is {format_size(size)}: the {format_size(esp)} ESP every disk boots "
        f"it from holds {format_size(capacity)}, so mkosi could not copy it onto the disk.",
        hint=hint,
        context={
            "uki_bytes": str(size),
            "esp_bytes": str(esp),
            "capacity_bytes": str(capacity),
            "log": str(log),
        },
    )


def _discard(line: str) -> None:
    del line


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(text: str, lines: int = 20) -> str:
    kept = text.rstrip().splitlines()[-lines:]
    return "\nLast output:\n" + "\n".join(f"  | {line}" for line in kept) if kept else ""
