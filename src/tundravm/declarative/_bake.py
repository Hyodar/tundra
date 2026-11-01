"""Bake a :class:`~tundravm.declarative._lowered.Lowered` recipe through its backend (internal).

:func:`bake` lints, verifies the lockfile (frozen bakes), compiles, checks the
host-fetched checkouts the build mounts and builds each active profile.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from tundravm._reporting import (
    Progress,
    Reporter,
    StructuredLogger,
    display_path,
    format_duration,
    format_size,
)
from tundravm._source import (
    DEPS_DIRNAME,
    NOT_PREFETCHED,
    SOURCES_DIRNAME,
    DebFile,
    KernelFile,
    KernelSource,
    LanguageBuild,
    SourceBuild,
    deps_problem,
    is_fetched,
)
from tundravm.backends.base import BuildBackend, drop_intermediates
from tundravm.check import check
from tundravm.errors import LintError, LockfileError, PolicyError, StateError, ValidationError
from tundravm.lockfile import LOCKFILE_VERSION, compare_lock, read_lockfile, recipe_digest
from tundravm.models import (
    ArtifactRef,
    BakeRequest,
    BakeResult,
    OutputTarget,
    ProfileBuildResult,
)
from tundravm.policy import ensure_bake_policy
from tundravm.toolchain import Toolchain

from ._compile import emit, mkosi_argv
from ._lowered import Lowered

_DRIFT_MESSAGE_LINES = 15
_ARTIFACT_FILENAMES: dict[OutputTarget, str] = {
    "qemu": "disk.qcow2",
    "azure": "disk.vhd",
    "gcp": "disk.raw.tar.gz",
}


def bake(
    lowered: Lowered, destination: Path, *, frozen: bool, reporter: Reporter | None
) -> BakeResult:
    """Compile, build and package *lowered*'s active profiles into *destination*.

    *reporter* receives progress events: lint/lock/compile phases, per-profile
    prepare and build phases with durations, every backend output line,
    artifacts with sizes, and a final ``done``. Without a
    reporter, backend output only surfaces in a failing backend's error.
    """
    progress = Progress(reporter)
    logger = StructuredLogger()
    active = lowered.active
    scope = active[0] if len(active) == 1 else None
    with (
        logger.attached(progress.reporter, started_at=progress.started),
        progress.guard(profile=scope),
    ):
        ensure_bake_policy(policy=lowered.policy, frozen=frozen)
        with progress.phase("lint", "lint", profile=scope):
            _lint(lowered, progress)
        if frozen:
            with progress.phase("lock", "verify lockfile", profile=scope):
                _assert_frozen_lock(lowered)
        destination.mkdir(parents=True, exist_ok=True)
        lock_digest = _lock_digest(lowered)
        lock_sha256 = _lock_sha256(lowered)
        locked_toolchain = _locked_toolchain(lowered)
        emission_root = destination / "mkosi"
        with progress.phase("compile", "compile", profile=scope):
            emit(lowered, emission_root)
        if lowered.backend is None:
            raise ValidationError(
                "No build backend configured.",
                hint=(
                    "Set TUNDRAVM_BACKEND (lima or local) or [tool.tundravm] backend, or "
                    "bind `backend = LimaMkosiBackend()` in the image file."
                ),
            )
        backend = lowered.backend
        offline = lowered.policy.network_mode == "offline"
        if offline and lowered.mkosi.with_network:
            raise PolicyError(
                "An offline build cannot build a tree whose build scripts reach the network "
                "(a component declared with build_network=True writes WithNetwork=yes).",
                hint="Build without --offline, or drop build_network=True from the component "
                "(the tree then writes WithNetwork=no).",
                context={"operation": "bake"},
            )
        sources_dir = _fetched_sources(lowered, destination, backend, offline=offline)
        profiles: dict[str, ProfileBuildResult] = {}
        for name in sorted(active):
            started = progress.elapsed()
            (destination / name).mkdir(parents=True, exist_ok=True)
            request = BakeRequest(
                profile=name,
                build_dir=destination,
                emit_dir=emission_root,
                output_targets=lowered.targets.get(name, ("qemu",)),
                on_output=None if reporter is None else progress.output(name),
                on_notice=None if reporter is None else progress.notice(name),
                sources_dir=sources_dir
                if lowered.state.effective_profile(name).source_builds
                or lowered.kernel_source(name)
                or lowered.deb_file(name)
                else None,
                mkosi_args=tuple(mkosi_argv(lowered, name)),
                toolchain=locked_toolchain,
            )
            logger.log(
                operation="bake_profile_start",
                profile=name,
                phase="build",
                module="image",
                builder=backend.name,
                message=f"Starting profile build via {backend.name} backend.",
            )
            result = _build(backend, request, progress)
            drop_intermediates(result.artifacts)  # azure.raw, gcp.raw: mkosi's, not artifacts
            _hash_artifacts(result, request, progress)
            _hash_manifests(result)
            _hash_boot_payloads(result)
            result.duration_s = progress.elapsed() - started
            profiles[name] = result
            logger.log(
                operation="bake_profile_complete",
                profile=name,
                phase="build",
                module="image",
                builder=backend.name,
                message="Completed profile build.",
            )
        total = progress.elapsed()
        baked = BakeResult(
            profiles=profiles,
            lock_digest=lock_digest,
            lock_sha256=lock_sha256,
            backend=backend.name,
            duration_s=total,
        )
        noun = "profile" if len(profiles) == 1 else "profiles"
        progress.emit(
            "done",
            scope,
            f"built {len(profiles)} {noun} in {format_duration(total)}",
            status="ok",
            duration_s=f"{total:.3f}",
        )
    return baked


def _lint(lowered: Lowered, progress: Progress) -> None:
    """Report warnings to *progress*; raise ``LintError`` on error-level diagnostics."""
    diagnostics = check(lowered)
    for diagnostic in diagnostics:
        if diagnostic.severity in ("warning", "error"):
            progress.emit(
                "warning",
                diagnostic.profile or None,
                f"{diagnostic.code}: {diagnostic.message}",
                level=diagnostic.severity,
                code=diagnostic.code,
            )
    errors = [d for d in diagnostics if d.severity == "error"]
    if errors:
        raise LintError(
            f"The image has {len(errors)} error-level diagnostics.",
            hint="Run `tundravm check IMAGE` to see them.",
            context={"codes": ", ".join(d.code for d in errors[:3])},
        )


def _build(backend: BuildBackend, request: BakeRequest, progress: Progress) -> ProfileBuildResult:
    """Run *backend* for *request*'s profile; its result, with artifacts found on disk added."""
    name = request.profile
    with progress.phase("prepare", f"prepare {backend.name}", profile=name):
        backend.prepare(request)
    try:
        with progress.phase("build", f"build via {backend.name}", profile=name):
            executed = backend.execute(request)
    finally:
        backend.cleanup(request)
    result = executed.profiles.get(name, ProfileBuildResult(profile=name))
    for target in request.output_targets:
        path = request.build_dir / name / _ARTIFACT_FILENAMES[target]
        if target not in result.artifacts and path.exists():
            result.artifacts[target] = ArtifactRef(target=target, path=path)
    return result


def _hash_artifacts(result: ProfileBuildResult, request: BakeRequest, progress: Progress) -> None:
    """Record each existing artifact's sha256 on *result* (chunked: images can be GiBs)."""
    for target, artifact in sorted(result.artifacts.items()):
        path = Path(artifact.path)
        if not path.exists():
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        result.artifacts[target] = replace(artifact, digest=digest)
        size = path.stat().st_size
        progress.emit(
            "artifact",
            request.profile,
            f"artifact {target} {display_path(path)} ({format_size(size)})",
            role="image",
            target=target,
            path=str(path),
            size_bytes=str(size),
            sha256=digest,
        )


def package_manifest_candidates(artifact: Path, variant: str) -> list[Path]:
    """Where mkosi may have written *variant*'s package manifest, in the order they are tried:
    ``<variant>.manifest``, ``<variant>.manifest.json``, then the same for the artifact stem."""
    directory = artifact.parent
    stem = artifact.name.split(".", 1)[0]
    names = dict.fromkeys(
        f"{name}{suffix}" for name in (variant, stem) for suffix in (".manifest", ".manifest.json")
    )
    return [directory / name for name in names]


def package_manifest(artifact: Path, variant: str) -> Path:
    """Where mkosi wrote the package manifest of *variant*'s *artifact*: ``<variant>.manifest``.

    ``<variant>.manifest.json`` and ``<artifact stem>.manifest`` beside it are taken
    too when they exist; the first candidate is returned when none does.
    """
    candidates = package_manifest_candidates(artifact, variant)
    return next((path for path in candidates if path.is_file()), candidates[0])


def _hash_manifests(result: ProfileBuildResult) -> None:
    """Record on *result* the sha256 of each artifact's package manifest, ``None`` if absent."""
    for target, artifact in sorted(result.artifacts.items()):
        manifest = package_manifest(Path(artifact.path), result.profile)
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None
        result.artifacts[target] = replace(artifact, manifest_digest=digest)


def _hash_boot_payloads(result: ProfileBuildResult) -> None:
    """Record on *result* the sha256 of the UKI each disk artifact boots; a payload the
    backend named but that is not a file is dropped (the relation is then unknown)."""
    for target, artifact in sorted(result.artifacts.items()):
        if artifact.boot_payload is None:
            continue
        path = Path(artifact.boot_payload)
        if not path.is_file():
            result.artifacts[target] = replace(artifact, boot_payload=None)
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        result.artifacts[target] = replace(artifact, boot_payload_digest=digest)


def _lock_digest(lowered: Lowered) -> str:
    """The sha256 of the lockfile the bake reads, else the active profiles' recipe digest."""
    path = lowered.lock_path
    if not path.exists():
        return lowered.digest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_sha256(lowered: Lowered) -> str | None:
    """The sha256 of the lockfile the bake reads, ``None`` when there is none."""
    path = lowered.lock_path
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _locked_toolchain(lowered: Lowered) -> Toolchain | None:
    """The toolchain the lockfile the bake reads records; ``None`` without a readable one."""
    path = lowered.lock_path
    if not path.is_file():
        return None
    try:
        return read_lockfile(path).toolchain
    except LockfileError:
        return None


def _fetched_sources(
    lowered: Lowered, destination: Path, backend: BuildBackend, *, offline: bool = False
) -> Path | None:
    """``<destination>/.sources`` when the build mounts it; fails if a checkout is missing.

    ``None`` without source builds, a built kernel or an ``EfiStub`` package. *offline*
    (no network in the build sandbox) also requires every language build's prefetched
    dependencies.
    """
    builds = lowered.lock_sources()
    if not builds:
        return None
    root = destination / SOURCES_DIRNAME
    pins = lowered.build_pins()
    for name, spec in builds.items():
        what = (
            "Kernel source"
            if isinstance(spec, KernelSource)
            else "EFI stub package"
            if isinstance(spec, DebFile)
            else "Kernel file"
            if isinstance(spec, KernelFile)
            else "Source build"
        )
        pin = spec.pin_from(pins)
        if pin is None:
            raise StateError(
                f"{what} {name!r} is not pinned, so nothing is fetched to build it from.",
                hint=(
                    "Run `tundravm lock IMAGE`, then `tundravm build`: it fetches what the "
                    "lock pins."
                ),
                context={"source": f"{spec.source.kind} {spec.source.url}"},
            )
        checkout = root / spec.pin_dir(pin)
        if not is_fetched(checkout, pin, spec.source, name=name):
            raise StateError(
                f"{what} {name!r} is not fetched: {checkout} is missing or incomplete.",
                hint=(
                    f"Run `tundravm build` once with the network: it fetches into "
                    f"{destination}/.sources, which `tundravm build --offline` reuses."
                ),
                context={"pin": pin},
            )
        if not offline or not isinstance(spec, SourceBuild):
            continue
        recipe = spec.build
        problem = deps_problem(spec, pin, root)
        if not isinstance(recipe, LanguageBuild) or problem is None:
            continue
        marker = str(root / DEPS_DIRNAME / (spec.deps_marker(pin) or ""))
        if problem == NOT_PREFETCHED:
            raise StateError(
                f"Source build {name!r} has no prefetched {recipe.kind} dependencies, "
                "and an offline build has no network to download them.",
                hint=(
                    f"Run `tundravm build` once with the network on a host with "
                    f"`{recipe.tool}` on PATH (it fills {root / DEPS_DIRNAME / recipe.deps}), "
                    "or build without --offline."
                ),
                context={"marker": marker},
            )
        raise StateError(
            f"Source build {name!r} has an incomplete {recipe.kind} dependency cache: "
            f"{problem}, and an offline build has no network to download it.",
            hint=(
                f"Run `tundravm build --clean-cache` with the network on a host with "
                f"`{recipe.tool}` on PATH: it prefetches the dependencies again. Or build "
                "without --offline."
            ),
            context={"marker": marker},
        )
    return root


def _assert_frozen_lock(lowered: Lowered) -> None:
    lock_path = lowered.lock_path
    lock = read_lockfile(lock_path)
    if lock.version < LOCKFILE_VERSION:
        raise LockfileError(
            f"A frozen build needs a version {LOCKFILE_VERSION} lockfile; {lock_path} is version "
            f"{lock.version}, which does not cover the distribution, compiler and kernel inputs.",
            hint="Run `tundravm lock IMAGE` to lock the image again, then commit the lockfile.",
            context={"lock": str(lock_path), "version": str(lock.version)},
        )
    current = lowered.payload()
    # A lock written for more variants than this bake selects covers it when
    # every section of the selected variants and the recipe-wide ones matches.
    drift = compare_lock(lock, current, partial=True)
    if lock.recipe_digest == recipe_digest(current) or drift.is_clean:
        unpinned = lowered.unpinned_sources(lock_path)
        if unpinned:
            raise LockfileError(
                f"A frozen build requires every source build to be pinned; unpinned: "
                f"{', '.join(unpinned)}.",
                hint="Run `tundravm lock IMAGE` to pin them.",
                context={"lock": str(lock_path), "sources": ", ".join(unpinned)},
            )
        return
    lines = drift.render().splitlines()
    if len(lines) > _DRIFT_MESSAGE_LINES:
        hidden = len(lines) - _DRIFT_MESSAGE_LINES
        lines = [
            *lines[:_DRIFT_MESSAGE_LINES],
            f"... {hidden} more; run `tundravm lock IMAGE --check` for the full list",
        ]
    sections = drift.sections
    changed = ", ".join(sections[:_DRIFT_MESSAGE_LINES])
    if len(sections) > _DRIFT_MESSAGE_LINES:
        changed += f", ... ({len(sections) - _DRIFT_MESSAGE_LINES} more)"
    raise LockfileError(
        "The lockfile of a frozen build is stale for the image as it is now:\n"
        + "\n".join(f"  {line}" for line in lines),
        hint="Run `tundravm lock IMAGE` to accept these changes, or revert them.",
        context={"lock": str(lock_path), "changed": changed},
    )


__all__ = ["bake", "package_manifest"]
