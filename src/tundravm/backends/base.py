"""Protocol for bake execution backends."""

from __future__ import annotations

import os
import re
import subprocess
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tundravm.models import (
    ArtifactRef,
    BakeRequest,
    BakeResult,
    CacheState,
    CacheUse,
    OutputTarget,
)


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

INCREMENTAL_DIRNAME = "incremental"
"""Where a development build keeps mkosi's incremental cache, under the backend's mkosi
state directory (``<build>/.mkosi/incremental`` on Linux): ``<key>.cache``, the base tree
after package install, ``<key>.build.cache``, its build overlay, and ``<key>.manifest``,
mkosi's record of what they were built from. A production build never gets this directory:
its cache directory is the sibling ``cache``, which holds repository metadata only."""

CACHE_KEY = "&d~&r~&a~&I~{key}"
"""``CacheKey=``: mkosi's default (distribution, release, architecture, image) and the
key tundravm appends (``BakeRequest.cache_key``), so what mkosi's cache manifest leaves out
(the skeleton and sandbox trees, the tools tree, mkosi's version, the profile) still
separates cache entries."""

CACHE_MANIFEST_SUFFIX = ".manifest"

BUILD_DIRNAME = "build"
"""``<incremental>/build``: mkosi's ``BuildDirectory=`` of a development build, kept across
builds and bound at :data:`BUILDDIR` in every build script and ``mkosi-chroot``; under its
``BuildKey=`` subdirectory the source hooks keep their artifact caches (``$BUILDDIR`` is
their cache root once it exists), ``go`` its build cache and ``cargo`` its build
directory. A production build sets no build directory, so its hooks cache in the build
overlay mkosi discards."""

BUILDDIR = "/work/build"
"""Where mkosi 27.1 binds ``BuildDirectory=`` for build scripts and inside ``mkosi-chroot``
(``run_build_scripts``: ``BUILDDIR`` and ``CHROOT_BUILDDIR``)."""

TOOLCHAIN_CACHES = (
    ("GOCACHE", f"{BUILDDIR}/go-build"),
    ("CARGO_BUILD_BUILD_DIR", f"{BUILDDIR}/cargo-build/{{workspace-path-hash}}"),
)
"""The compiler caches a development build keeps in the build directory, as the
environment its build scripts and ``mkosi-chroot`` get: Go's build cache, and cargo's
build directory (``build.build-dir``, cargo 1.91; the binary stays in ``target/``, where the
hook installs it from; an older cargo ignores the variable). Each is passed with
``PassEnvironment=`` too, so the payload subimage, whose hooks compile, inherits it."""


def incremental_args(key: str, build_dir: str) -> list[str]:
    """The mkosi options that make a build incremental under *key* (``BakeRequest.cache_key``)
    with *build_dir* (a path as mkosi sees it) as its ``BuildDirectory=``; the cache
    directory itself is the backend's."""
    return [
        "--incremental=yes",
        f"--cache-key={CACHE_KEY.format(key=key)}",
        f"--build-directory={build_dir}",
        *(f"--environment={name}={value}" for name, value in TOOLCHAIN_CACHES),
        *(f"--pass-environment={name}" for name, _ in TOOLCHAIN_CACHES),
    ]


def cache_manifests(
    cache_dir: Path, images: Sequence[Mapping[str, Any]], key: str
) -> dict[str, Path]:
    """Where mkosi writes each image's cache manifest for *key*: the image's name to
    ``<cache_dir>/<CacheKey expanded>.manifest``, with ``&d``, ``&r``, ``&a`` and ``&I``
    expanded from *images* (``mkosi summary --json``'s, the resolved configuration)."""
    found: dict[str, Path] = {}
    for image in images:
        name = str(image.get("Image") or "main")
        expanded = (
            CACHE_KEY.replace("&d", str(image.get("Distribution") or ""))
            .replace("&r", str(image.get("Release") or ""))
            .replace("&a", str(image.get("Architecture") or ""))
            .replace("&I", name)
            .format(key=key)
        )
        found[name] = cache_dir / f"{expanded}{CACHE_MANIFEST_SUFFIX}"
    return found


def cache_stamp(manifest: Path) -> tuple[int, int] | None:
    """The cache manifest *manifest* as it is now (mtime and size), ``None`` when absent:
    mkosi writes it only when it builds and stores an image's base tree, so a stamp that is
    the same after a build means the tree was reused."""
    try:
        found = manifest.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


def cache_use(
    cache_dir: Path,
    key: str,
    manifests: Mapping[str, Path],
    before: Mapping[str, tuple[int, int] | None],
) -> CacheUse:
    """What the build just run did with the cache, from each image's manifest stamp
    *before* it and now (:func:`cache_stamp`)."""
    images: dict[str, CacheState] = {}
    for name, manifest in manifests.items():
        after = cache_stamp(manifest)
        if after is None:
            images[name] = "none"
        elif after == before.get(name):
            images[name] = "hit"
        else:
            images[name] = "saved"
    return CacheUse(directory=cache_dir, key=key, images=images)


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


def mkosi_project(emit_dir: Path) -> Path:
    """The directory mkosi builds every variant from: the tree root. The variant is selected
    with ``BakeRequest.mkosi_args`` (``--profile=``, ``--dependency=``)."""
    return emit_dir


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


ARTIFACT_NAMES: dict[OutputTarget, str] = {
    "qemu": "qemu.raw",
    "azure": "azure.vhd",
    "gcp": "gcp.tar.gz",
}
"""What each target's image is called in the output directory: the main image's raw disk
and the cloud wrappers' converted disks."""
BOOT_PAYLOAD_NAME = "payload.efi"
"""The UKI every disk of the tree boots: the payload subimage's output."""
INTERMEDIATE_NAMES: dict[OutputTarget, str] = {"azure": "azure.raw", "gcp": "gcp.raw"}
"""The raw disk each cloud wrapper leaves beside its converted artifact (mkosi still reads it
after the postoutput scripts, so they keep it); the bake removes it once collected."""


def drop_intermediates(artifacts: Mapping[OutputTarget, ArtifactRef]) -> list[Path]:
    """Remove the cloud wrappers' raw disks beside *artifacts* (:data:`INTERMEDIATE_NAMES`);
    the published output keeps ``payload.efi``, ``qemu.raw`` and the converted disks.
    Returns what was removed."""
    removed: list[Path] = []
    for target, ref in artifacts.items():
        name = INTERMEDIATE_NAMES.get(target)
        raw = None if name is None else ref.path.parent / name
        if raw is not None and raw.is_file():
            raw.unlink()
            removed.append(raw)
    return removed


def collect_artifacts(
    output_dir: Path, targets: Sequence[OutputTarget]
) -> dict[OutputTarget, ArtifactRef]:
    """The artifacts of *targets* in *output_dir* (:data:`ARTIFACT_NAMES`), each with the
    UKI it boots (:data:`BOOT_PAYLOAD_NAME`) as its boot payload; a target whose image is
    missing has no artifact."""
    uki = output_dir / BOOT_PAYLOAD_NAME
    payload = uki if uki.is_file() else None
    artifacts: dict[OutputTarget, ArtifactRef] = {}
    for target in targets:
        path = output_dir / ARTIFACT_NAMES[target]
        if path.is_file():
            artifacts[target] = ArtifactRef(target=target, path=path, boot_payload=payload)
    return artifacts
