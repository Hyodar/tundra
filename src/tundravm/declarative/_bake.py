"""Bake a :class:`~tundravm.declarative._lowered.Lowered` recipe through its backend (internal).

:func:`bake` lints, verifies the lockfile (frozen bakes), compiles, checks the
host-fetched checkouts the build mounts, builds each active profile and writes
its ``report.json`` and the ``bake-result.json`` manifest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tundravm._source import SOURCES_DIRNAME, KernelSource, is_fetched
from tundravm.backends.base import BuildBackend
from tundravm.check import check
from tundravm.compiler import PHASE_ORDER, MkosiEmission
from tundravm.errors import LintError, LockfileError, StateError, ValidationError
from tundravm.lockfile import compare_lock, read_lockfile, recipe_digest
from tundravm.models import (
    ArtifactRef,
    BakeRequest,
    BakeResult,
    OutputTarget,
    Phase,
    ProfileBuildResult,
)
from tundravm.observability import (
    Progress,
    Reporter,
    StructuredLogger,
    display_path,
    format_duration,
    format_size,
)
from tundravm.policy import ensure_bake_policy

from ._compile import emit
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
    artifacts with sizes, the report path, and a final ``done``. Without a
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
        emission_root = destination / "mkosi"
        with progress.phase("compile", "compile", profile=scope):
            _, emission = emit(lowered, emission_root)
        if lowered.backend is None:
            raise ValidationError(
                "No build backend configured.",
                hint=(
                    "Pass --backend (lima, nix or local), or bind "
                    "`backend = LimaMkosiBackend()` in the recipe file."
                ),
            )
        backend = lowered.backend
        sources_dir = _fetched_sources(lowered, destination, backend)
        profiles: dict[str, ProfileBuildResult] = {}
        for name in sorted(active):
            started = progress.elapsed()
            (destination / name).mkdir(parents=True, exist_ok=True)
            request = BakeRequest(
                profile=name,
                build_dir=destination,
                emit_dir=emission_root,
                output_targets=lowered.state.effective_profile(name).output_targets,
                on_output=None if reporter is None else progress.output(name),
                on_notice=None if reporter is None else progress.notice(name),
                sources_dir=sources_dir
                if lowered.state.effective_profile(name).source_builds
                or lowered.kernel_source(name)
                else None,
            )
            logger.log(
                operation="bake_profile_start",
                profile=name,
                phase="build",
                module="image",
                builder=backend.name,
                message=f"Starting variant bake via {backend.name} backend.",
            )
            result = _build(backend, request, progress)
            digests = _hash_artifacts(result, request, progress)
            report = {
                "profile": name,
                "lock_digest": lock_digest,
                "backend": backend.name,
                "debloat": lowered.explain_debloat(profile=name),
                "artifact_digests": digests,
                "emitted_scripts": _script_checksums(emission, name),
                "artifacts": {target: str(ref.path) for target, ref in result.artifacts.items()},
                "logs": logger.records_for_profile(name),
            }
            result.report_path = destination / name / "report.json"
            result.report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            progress.emit(
                "artifact",
                name,
                f"report {display_path(result.report_path)}",
                role="report",
                path=str(result.report_path),
            )
            result.duration_s = progress.elapsed() - started
            profiles[name] = result
            logger.log(
                operation="bake_profile_complete",
                profile=name,
                phase="build",
                module="image",
                builder=backend.name,
                message="Completed variant bake.",
            )
        total = progress.elapsed()
        baked = BakeResult(
            profiles=profiles,
            lock_digest=lock_digest,
            backend=backend.name,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_s=total,
        )
        baked.save(destination)
        noun = "variant" if len(profiles) == 1 else "variants"
        progress.emit(
            "done",
            scope,
            f"baked {len(profiles)} {noun} in {format_duration(total)}",
            status="ok",
            duration_s=f"{total:.3f}",
        )
    return baked


def _lint(lowered: Lowered, progress: Progress) -> None:
    """Report warnings to *progress*; raise ``LintError`` on error-level diagnostics."""
    diagnostics = check(lowered)
    for diagnostic in diagnostics:
        if diagnostic.level in ("warning", "error"):
            progress.emit(
                "warning",
                diagnostic.profile,
                f"{diagnostic.code}: {diagnostic.message}",
                level=diagnostic.level,
                code=diagnostic.code,
            )
    errors = [d for d in diagnostics if d.level == "error"]
    if errors:
        raise LintError(
            f"Recipe has {len(errors)} error-level diagnostics.",
            hint="Run `tundravm lint RECIPE` to see them.",
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


def _hash_artifacts(
    result: ProfileBuildResult, request: BakeRequest, progress: Progress
) -> dict[str, str]:
    """Record each existing artifact's sha256 on *result* (chunked: images can be GiBs)."""
    digests: dict[str, str] = {}
    for target, artifact in sorted(result.artifacts.items()):
        path = Path(artifact.path)
        if not path.exists():
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        digests[target] = digest
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
    return digests


def _script_checksums(emission: MkosiEmission, profile: str) -> dict[str, str]:
    scripts: dict[Phase, Path] = emission.script_paths.get(profile, {})
    return {
        f"{phase}:{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
        for phase, path in sorted(scripts.items(), key=lambda item: PHASE_ORDER.index(item[0]))
    }


def _lock_digest(lowered: Lowered) -> str:
    """The sha256 of the lockfile the bake reads, else the active profiles' recipe digest."""
    path = lowered.lock_path
    if not path.exists():
        return lowered.digest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetched_sources(lowered: Lowered, destination: Path, backend: BuildBackend) -> Path | None:
    """``<destination>/.sources`` when the build mounts it; fails if a checkout is missing.

    ``None`` under ``nethermind-v1``, without source builds or a built kernel,
    and for the in-process backend, which builds nothing.
    """
    builds = lowered.lock_sources()
    if not lowered.fetches_sources or not builds or backend.name == "inprocess":
        return None
    root = destination / SOURCES_DIRNAME
    pins = lowered.build_pins()
    for name, spec in builds.items():
        what = "Kernel source" if isinstance(spec, KernelSource) else "Source build"
        pin = spec.pin_from(pins)
        if pin is None:
            raise StateError(
                f"{what} {name!r} is not pinned, so nothing is fetched to build it from.",
                hint=(
                    "Run `tundravm lock RECIPE` and `tundravm fetch RECIPE`, or bake "
                    "without --no-fetch."
                ),
                context={"source": f"{spec.source.kind} {spec.source.url}"},
            )
        checkout = root / spec.pin_dir(pin)
        if not is_fetched(checkout, pin):
            raise StateError(
                f"{what} {name!r} is not fetched: {checkout} is missing or incomplete.",
                hint=(
                    f"Run `tundravm fetch RECIPE --out {destination}` first, or bake "
                    "without --no-fetch."
                ),
                context={"pin": pin},
            )
    return root


def _assert_frozen_lock(lowered: Lowered) -> None:
    lock_path = lowered.lock_path
    lock = read_lockfile(lock_path)
    current = lowered.payload()
    # A lock written for more variants than this bake selects covers it when
    # every section of the selected variants and the recipe-wide ones matches.
    drift = compare_lock(lock, current, partial=True)
    if lock.recipe_digest == recipe_digest(current) or drift.is_clean:
        unpinned = lowered.unpinned_sources(lock_path)
        if unpinned:
            raise LockfileError(
                f"Frozen bake requires every source build to be pinned; unpinned: "
                f"{', '.join(unpinned)}.",
                hint="run tundravm lock RECIPE to pin them.",
                context={"lock": str(lock_path), "sources": ", ".join(unpinned)},
            )
        return
    lines = drift.render().splitlines()
    if len(lines) > _DRIFT_MESSAGE_LINES:
        hidden = len(lines) - _DRIFT_MESSAGE_LINES
        lines = [
            *lines[:_DRIFT_MESSAGE_LINES],
            f"... {hidden} more; run `tundravm lock RECIPE --check` for the full list",
        ]
    sections = drift.sections
    changed = ", ".join(sections[:_DRIFT_MESSAGE_LINES])
    if len(sections) > _DRIFT_MESSAGE_LINES:
        changed += f", ... ({len(sections) - _DRIFT_MESSAGE_LINES} more)"
    raise LockfileError(
        "Frozen bake lockfile is stale for current recipe state:\n"
        + "\n".join(f"  {line}" for line in lines),
        hint="Run tundravm lock RECIPE to accept these changes, or revert them.",
        context={"lock": str(lock_path), "changed": changed},
    )


__all__ = ["bake"]
