"""The lowered recipe's lifecycle glue; not a public API.

``tundravm.declarative.lower`` builds the compiler's ``RecipeState`` (through
``tundravm.declarative.state``) and wraps it, with the recipe-wide mkosi
options and kernels, in an :class:`Image`, which compiles, locks and bakes it.
Nothing outside ``tundravm`` should build one by hand.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ._modules.base import Module
from ._modules.init import Init
from ._options import MkosiOptions
from ._source import (
    SOURCES_DIRNAME,
    KernelSource,
    NamedSource,
    Resolver,
    SourceBuild,
    is_fetched,
    source_drift,
)
from .backends.base import BuildBackend
from .check import check as run_checks
from .compiler import (
    PHASE_ORDER,
    EmitConfig,
    MkosiEmission,
    emit_mkosi_tree,
)
from .errors import (
    LintError,
    LockfileError,
    PolicyError,
    StateError,
    ValidationError,
)
from .lockfile import (
    LockDrift,
    LockedFetch,
    Lockfile,
    compare_lock,
    read_lockfile,
    recipe_digest,
    unselected_sources,
)
from .models import (
    NETWORK_SETUP_UNIT,
    Arch,
    ArtifactRef,
    BakeRequest,
    BakeResult,
    CompileResult,
    InitScriptEntry,
    Kernel,
    OutputTarget,
    Phase,
    ProfileBuildResult,
    ProfileState,
    RecipeState,
    ServiceSpec,
    enable_unit,
    ships_unit,
)
from .observability import (
    Progress,
    Reporter,
    StructuredLogger,
    display_path,
    format_duration,
    format_size,
)
from .policy import Policy, ensure_bake_policy

_DRIFT_MESSAGE_LINES = 15


def _short_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _init_scripts_payload(entries: Sequence[InitScriptEntry]) -> list[dict[str, object]]:
    return [
        {"priority": entry.priority, "sha256": hashlib.sha256(entry.script.encode()).hexdigest()}
        for entry in sorted(entries, key=lambda item: (item.priority, item.script))
    ]


@dataclass(slots=True, kw_only=True)
class Image:
    """The lowered recipe: per-profile state the compiler emits as an mkosi tree.

    ``declarative.lower`` builds :attr:`state` and the runtime tools applied
    to each profile (:attr:`modules`); ``compile()`` emits the tree and
    ``bake()`` runs the backend. mkosi-only knobs live in
    :class:`~tundravm._options.MkosiOptions`.
    """

    state: RecipeState
    modules: dict[str, list[Module]] = field(default_factory=dict, repr=False)
    """The runtime tools configured on each profile, which ``check()`` inspects."""
    backend: BuildBackend | None = None
    build_dir: Path = field(default_factory=lambda: Path("build"))
    reproducible: bool = True
    policy: Policy = field(default_factory=Policy)
    kernel: Kernel | None = None
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    snapshot: str | None = None
    mkosi: MkosiOptions = field(default_factory=MkosiOptions)
    profile_mkosi: dict[str, MkosiOptions] = field(default_factory=dict)
    """Profiles whose mkosi options differ from ``mkosi``."""
    profile_kernels: dict[str, Kernel | None] = field(default_factory=dict)
    """Profiles whose kernel differs from ``kernel`` (``None``: no kernel)."""
    lock_file: Path | None = None
    """The lockfile to read instead of ``<build_dir>/tundravm.lock``."""
    fetched_pins: dict[str, LockedFetch] = field(default_factory=dict, repr=False)
    """Pins ``tundravm fetch`` resolved for sources the lockfile does not pin (unlocked bakes)."""
    logger: StructuredLogger = field(init=False, default_factory=StructuredLogger, repr=False)
    init: Init = field(init=False, default_factory=Init, repr=False)
    _active_profiles: tuple[str, ...] = field(init=False, repr=False)
    _last_compile_digest: str | None = field(init=False, default=None, repr=False)
    _last_compile_path: Path | None = field(init=False, default=None, repr=False)
    _last_compile_emission: MkosiEmission | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.build_dir = Path(self.build_dir)
        self._active_profiles = (self.default_profile,)

    @property
    def base(self) -> str:
        return self.state.base

    @property
    def arch(self) -> Arch:
        return self.state.arch

    @property
    def default_profile(self) -> str:
        return self.state.default_profile

    @property
    def profile_names(self) -> tuple[str, ...]:
        """Every profile declared so far, sorted."""
        return tuple(sorted(self.state.profiles))

    def applied_modules(
        self, profile: str | None = None, *, inherited: bool = False
    ) -> tuple[Module, ...]:
        """``Module`` instances applied to *profile* (default: the active profile), in order.

        With ``inherited=True`` the modules of the profile it extends come first.
        """
        selected = self._resolve_operation_profile(profile)
        own = tuple(self.modules.get(selected, ()))
        extends = self.state.ensure_profile(selected).extends
        if not inherited or extends is None:
            return own
        base = tuple(self.modules.get(extends, ()))
        return base + tuple(m for m in own if not any(m is b for b in base))

    @property
    def fetches_sources(self) -> bool:
        """Whether source builds build host-fetched checkouts (every dialect but nethermind-v1).

        Their hooks, and the kernel build script, copy
        ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``, which ``tundravm fetch``
        writes to ``<build_dir>/.sources`` and the backend mounts; under
        ``nethermind-v1`` the hooks and the kernel script fetch in the build sandbox.
        """
        return self.mkosi.dialect != "nethermind-v1"

    def source_builds(self, *, profile: str | None = None) -> dict[str, SourceBuild]:
        """Source builds declared for *profile* (default: every active profile), by name."""
        names = (profile,) if profile is not None else self._active_profiles
        builds: dict[str, SourceBuild] = {}
        for name in names:
            builds.update(self.state.effective_profile(name).source_builds)
        return dict(sorted(builds.items()))

    def kernel_source(self, profile: str) -> KernelSource | None:
        """The source of the kernel *profile* builds, under its lockfile name.

        ``None`` when the profile builds no kernel (none, or one without a
        config). The name is ``kernel``, or ``kernel-<profile>`` when the
        profile's kernel source differs from the default profile's.
        """
        kernel = self.kernel_for(profile)
        if kernel is None or not kernel.config_file:
            return None
        spec = KernelSource.of(kernel)
        if profile in self.profile_kernels and (
            self.kernel is None or KernelSource.of(self.kernel).source != spec.source
        ):
            return replace(spec, name=f"kernel-{profile}")
        return spec

    def lock_sources(self) -> dict[str, NamedSource]:
        """What ``lock`` pins and ``fetch`` checks out for the active profiles, by name.

        The source builds and, outside ``nethermind-v1`` (:attr:`fetches_sources`),
        the built kernels' sources (:meth:`kernel_source`).
        """
        builds = self.source_builds()
        sources: dict[str, NamedSource] = dict(builds)
        if not self.fetches_sources:
            return sources
        for profile in self._active_profiles:
            spec = self.kernel_source(profile)
            if spec is None:
                continue
            if spec.name in builds:
                raise ValidationError(
                    f"Source build {spec.name!r} takes the name of the kernel's source.",
                    hint=(
                        f"Rename the Build: {spec.name!r} names the kernel's lockfile pin "
                        "and checkout."
                    ),
                    context={"profile": profile},
                )
            sources[spec.name] = spec
        return dict(sorted(sources.items()))

    def source_pins(self, path: str | Path | None = None) -> dict[str, LockedFetch]:
        """Source-build entries of the lockfile at *path* (default ``<build_dir>/tundravm.lock``).

        Empty when the lockfile does not exist.
        """
        lock_path = self._normalize_path(path, fallback=self._default_lock_path())
        if not lock_path.exists():
            return {}
        lock = read_lockfile(lock_path)
        return {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}

    def unpinned_sources(self, path: str | Path | None = None) -> list[str]:
        """Names of the active :meth:`lock_sources` the lockfile at *path* does not pin."""
        pins = self.source_pins(path)
        return [name for name, spec in self.lock_sources().items() if spec.pin_from(pins) is None]

    def explain_debloat(self, *, profile: str | None = None) -> dict[str, object]:
        selected_profile = self._resolve_operation_profile(profile)
        config = self.state.effective_profile(selected_profile).debloat
        return {
            "profile": selected_profile,
            "enabled": config.enabled,
            "paths_remove": list(config.effective_paths_remove),
            "paths_skip": list(config.paths_skip),
            "systemd_minimize": config.systemd_minimize,
            "systemd_units_keep": list(config.effective_units_keep),
            "systemd_bins_keep": list(config.systemd_bins_keep),
        }

    def init_scripts(self, profile: str | None = None) -> tuple[InitScriptEntry, ...]:
        """Runtime-init fragments *profile* (default: the active one) runs, deduplicated.

        Registration order, the default profile's first for a profile that extends it.
        """
        selected = self._resolve_operation_profile(profile)
        entries = self.state.effective_profile(selected).init_scripts
        return tuple({(e.priority, e.script): e for e in entries}.values())

    def lock_status(
        self,
        path: str | Path | None = None,
        *,
        resolver: Resolver | None = None,
        profiles: Sequence[str] | None = None,
    ) -> LockDrift:
        """Compare the lockfile at *path* with the current recipe, section by section.

        Reads ``<build_dir>/tundravm.lock`` by default and never writes. The
        returned :class:`~tundravm.lockfile.LockDrift` lists changed, added and
        removed sections; ``render()`` prints them (``~ variants.default.packages:
        +htop``) or ``lock is up to date``. A lockfile written before section
        digests existed reports every section as added. Source builds report as
        ``+ sources.<name>: source <name> is not pinned`` or ``~ sources.<name>: <old>
        -> <new>`` (pinned for another ref, or, with *resolver*, the ref has moved);
        without *resolver* nothing touches the network. Raises
        :class:`LockfileError` when the lockfile is missing or unreadable.

        *profiles* that leave out some declared profile are checked against
        their own sections only (see :func:`~tundravm.lockfile.compare_lock`).
        """
        lock_path = self._normalize_path(path, fallback=self._default_lock_path())
        lock = read_lockfile(lock_path)
        with self._operation_scope(profiles) as names:
            partial = not set(self.state.profiles) <= set(names)
            return self._lock_drift(lock, resolver=resolver, partial=partial)

    def _lock_drift(self, lock: Lockfile, *, resolver: Resolver | None, partial: bool) -> LockDrift:
        """Section and source drift of the active profiles against *lock*."""
        payload = self._recipe_payload(profile_names=self._active_profiles)
        drift = compare_lock(lock, payload, partial=partial)
        pins = {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}
        added, changed, removed, details = source_drift(
            self.lock_sources(), pins, resolver=resolver
        )
        if partial:
            elsewhere = unselected_sources(lock, payload)
            removed = [s for s in removed if s.removeprefix("sources.") not in elsewhere]
        return replace(
            drift,
            added=(*drift.added, *added),
            changed=(*drift.changed, *changed),
            removed=(*drift.removed, *removed),
            details={**drift.details, **details},
        )

    def compile(
        self,
        path: str | Path,
        *,
        force: bool = False,
        profiles: Sequence[str] | None = None,
    ) -> CompileResult:
        """Emit the mkosi tree for *profiles* (default: the active ones) to *path*."""
        with self._operation_scope(profiles):
            return self._compile(self._normalize_path(path), force=force)

    def _compile(self, destination: Path, *, force: bool) -> CompileResult:
        # Runtime-init is generated into a scratch copy so compiling never changes what
        # later steps (lint, lock, a second compile) see.
        declared = self.state
        self.state = copy.deepcopy(declared)
        try:
            return self._emit(destination, force=force)
        finally:
            self.state = declared

    def _emit(self, destination: Path, *, force: bool) -> CompileResult:
        self._apply_init()
        digest = recipe_digest(self._recipe_payload(profile_names=self._active_profiles))
        pins = self._build_pins()
        self._enforce_source_policy(pins)
        config = self._emit_config(pins)
        # The mkosi options and kernel shape the tree without entering the digest.
        compile_key = self._compile_key(digest, pins) + ":" + _short_sha(repr(config))
        if (
            not force
            and self._last_compile_digest == compile_key
            and self._last_compile_path == destination
            and destination.exists()
        ):
            return CompileResult(
                path=destination,
                profiles=self._active_profiles,
                digest=digest,
            )
        self._last_compile_emission = emit_mkosi_tree(
            recipe=self._pinned_state(pins),
            destination=destination,
            profile_names=self._active_profiles,
            base=self.base,
            config=config,
        )
        self._last_compile_digest = compile_key
        self._last_compile_path = destination
        return CompileResult(
            path=destination,
            profiles=self._active_profiles,
            digest=digest,
        )

    def bake(
        self,
        output_dir: str | Path | None = None,
        *,
        frozen: bool = False,
        force: bool = False,
        reporter: Reporter | None = None,
        profiles: Sequence[str] | None = None,
    ) -> BakeResult:
        """Compile, build, and package *profiles* (default: the active ones) via the backend.

        *reporter* receives progress events: lint/lock/compile phases, per-profile
        prepare and build phases with durations, every backend output line,
        artifacts with sizes, the report path, and a final ``done``. Without a
        reporter, backend output only surfaces in a failing backend's error.
        """
        with self._operation_scope(profiles):
            return self._bake(output_dir, frozen=frozen, force=force, reporter=reporter)

    def _bake(
        self,
        output_dir: str | Path | None,
        *,
        frozen: bool,
        force: bool,
        reporter: Reporter | None,
    ) -> BakeResult:
        progress = Progress(reporter)
        active = self._active_profiles
        scope = active[0] if len(active) == 1 else None
        with (
            self.logger.attached(progress.reporter, started_at=progress.started),
            progress.guard(profile=scope),
        ):
            ensure_bake_policy(policy=self.policy, frozen=frozen)
            with progress.phase("lint", "lint", profile=scope):
                diagnostics = run_checks(self)
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
            if frozen:
                with progress.phase("lock", "verify lockfile", profile=scope):
                    self._assert_frozen_lock(profile_names=self._active_profiles)
            destination = self._normalize_path(output_dir, fallback=self.build_dir)
            destination.mkdir(parents=True, exist_ok=True)
            recipe_lock_digest = recipe_digest(
                self._recipe_payload(profile_names=self._active_profiles),
            )
            lock_digest = self._compute_lock_digest(recipe_lock_digest)

            # Compile the mkosi tree (skips if unchanged)
            emission_root = destination / "mkosi"
            with progress.phase("compile", "compile", profile=scope):
                self.compile(emission_root, force=force)
            emission = self._last_compile_emission
            assert emission is not None

            # Validate backend
            if self.backend is None:
                raise ValidationError(
                    "No build backend configured.",
                    hint=(
                        "Pass --backend (lima, nix or local), or bind "
                        "`backend = LimaMkosiBackend()` in the recipe file."
                    ),
                )
            backend = self.backend
            sources_dir = self._fetched_sources(destination, backend)

            profiles_result: dict[str, ProfileBuildResult] = {}
            for profile_name in self._sorted_active_profile_names():
                profile_started = progress.elapsed()
                profile = self.state.effective_profile(profile_name)
                profile_dir = destination / profile_name
                profile_dir.mkdir(parents=True, exist_ok=True)

                self.logger.log(
                    operation="bake_profile_start",
                    profile=profile_name,
                    phase="build",
                    module="image",
                    builder=backend.name,
                    message=f"Starting variant bake via {backend.name} backend.",
                )

                # Build via the real backend, streaming its output to the reporter
                request = BakeRequest(
                    profile=profile_name,
                    build_dir=destination,
                    emit_dir=emission_root,
                    output_targets=profile.output_targets,
                    on_output=None if reporter is None else progress.output(profile_name),
                    on_notice=None if reporter is None else progress.notice(profile_name),
                    sources_dir=sources_dir
                    if profile.source_builds or self.kernel_source(profile_name)
                    else None,
                )

                with progress.phase("prepare", f"prepare {backend.name}", profile=profile_name):
                    backend.prepare(request)
                try:
                    with progress.phase("build", f"build via {backend.name}", profile=profile_name):
                        backend_result = backend.execute(request)
                finally:
                    backend.cleanup(request)

                # Merge backend artifacts into profile result
                profile_result = backend_result.profiles.get(
                    profile_name, ProfileBuildResult(profile=profile_name)
                )

                # If the backend didn't find typed artifacts for all targets,
                # check if the output files exist with expected names
                for target in profile.output_targets:
                    if target not in profile_result.artifacts:
                        artifact_path = profile_dir / self._artifact_filename(target)
                        if artifact_path.exists():
                            profile_result.artifacts[target] = ArtifactRef(
                                target=target,
                                path=artifact_path,
                            )

                # Hash artifacts (chunked; images can be several GiB)
                artifact_digests: dict[str, str] = {}
                for target, artifact in sorted(profile_result.artifacts.items()):
                    path = Path(artifact.path)
                    if not path.exists():
                        continue
                    with path.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    artifact_digests[target] = digest
                    profile_result.artifacts[target] = replace(artifact, digest=digest)
                    size = path.stat().st_size
                    progress.emit(
                        "artifact",
                        profile_name,
                        f"artifact {target} {display_path(path)} ({format_size(size)})",
                        role="image",
                        target=target,
                        path=str(path),
                        size_bytes=str(size),
                        sha256=digest,
                    )

                # Generate build report
                script_checksums = self._script_checksums(
                    emission.script_paths.get(profile_name, {})
                )
                profile_logs = self.logger.records_for_profile(profile_name)

                report_path = profile_dir / "report.json"
                report_payload = {
                    "profile": profile_name,
                    "lock_digest": lock_digest,
                    "backend": backend.name,
                    "debloat": self.explain_debloat(profile=profile_name),
                    "artifact_digests": artifact_digests,
                    "emitted_scripts": script_checksums,
                    "artifacts": {
                        target: str(artifact.path)
                        for target, artifact in profile_result.artifacts.items()
                    },
                    "logs": profile_logs,
                }
                report_path.write_text(
                    json.dumps(report_payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                progress.emit(
                    "artifact",
                    profile_name,
                    f"report {display_path(report_path)}",
                    role="report",
                    path=str(report_path),
                )
                profile_result.report_path = report_path
                profile_result.duration_s = progress.elapsed() - profile_started
                profiles_result[profile_name] = profile_result

                self.logger.log(
                    operation="bake_profile_complete",
                    profile=profile_name,
                    phase="build",
                    module="image",
                    builder=backend.name,
                    message="Completed variant bake.",
                )

            total = progress.elapsed()
            bake_result = BakeResult(
                profiles=profiles_result,
                lock_digest=lock_digest,
                backend=backend.name,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                duration_s=total,
            )
            bake_result.save(destination)
            noun = "variant" if len(profiles_result) == 1 else "variants"
            progress.emit(
                "done",
                scope,
                f"baked {len(profiles_result)} {noun} in {format_duration(total)}",
                status="ok",
                duration_s=f"{total:.3f}",
            )
        return bake_result

    def mkosi_for(self, profile: str) -> MkosiOptions:
        """The mkosi options profile *profile* is emitted with."""
        return self.profile_mkosi.get(profile, self.mkosi)

    def kernel_for(self, profile: str) -> Kernel | None:
        """The kernel profile *profile* builds."""
        return self.profile_kernels.get(profile, self.kernel)

    def _emit_config(self, pins: Mapping[str, LockedFetch]) -> EmitConfig:
        """Build an EmitConfig from the Image's settings and ``self.mkosi``.

        Profiles with their own options or kernel get their own configuration.
        Outside ``nethermind-v1`` each built kernel carries its pin from *pins*.
        """
        own = sorted(set(self.profile_mkosi) | set(self.profile_kernels))
        default = self._kernel_pin(self.kernel_source(self.default_profile), pins)
        return replace(
            self._emit_config_for(self.mkosi, self.kernel, default),
            profiles={
                name: self._emit_config_for(
                    self.mkosi_for(name),
                    self.kernel_for(name),
                    self._kernel_pin(self.kernel_source(name), pins),
                )
                for name in own
            },
        )

    def _kernel_pin(
        self, spec: KernelSource | None, pins: Mapping[str, LockedFetch]
    ) -> dict[str, object]:
        """``EmitConfig`` fields for the kernel source *spec*: its pin and checkout directory."""
        pin = None if spec is None or not self.fetches_sources else spec.pin_from(pins)
        if spec is None or pin is None:
            return {}
        return {"kernel_pin": pin, "kernel_checkout": spec.pin_dir(pin)}

    def _emit_config_for(
        self, options: MkosiOptions, kernel: Kernel | None, pinned: Mapping[str, object]
    ) -> EmitConfig:
        emit_kwargs: dict[str, object] = {
            "base": self.base,
            "arch": self.arch,
            "reproducible": self.reproducible,
            "kernel": kernel,
            "mirror": self.mirror,
            "tools_tree_mirror": self.tools_tree_mirror,
            "snapshot": self.snapshot,
            "with_network": options.with_network,
            "clean_package_metadata": options.clean_package_metadata,
            "manifest_format": options.manifest_format,
            "compress_output": options.compress_output,
            "output_directory": options.output_directory,
            "sandbox_trees": options.sandbox_trees,
            "sandbox_files": options.sandbox_files,
            "package_cache_directory": options.package_cache_directory,
            "init_script": options.init_script,
            "generate_version_script": options.generate_version_script,
            "generate_cloud_postoutput": options.generate_cloud_postoutput,
            "emit_mode": options.emit_mode,
            "environment": dict(options.environment) or None,
            "environment_passthrough": options.environment_passthrough,
            "settings": options.settings,
            "bootable": options.bootable,
            "dialect": options.dialect,
        }
        if options.seed is not None:
            emit_kwargs["seed"] = options.seed
        emit_kwargs.update(pinned)
        return EmitConfig(**emit_kwargs)  # type: ignore[arg-type]

    def _artifact_filename(self, target: OutputTarget) -> str:
        mapping: dict[OutputTarget, str] = {
            "qemu": "disk.qcow2",
            "azure": "disk.vhd",
            "gcp": "disk.raw.tar.gz",
        }
        return mapping[target]

    def _normalize_path(self, path: str | Path | None, *, fallback: Path | None = None) -> Path:
        if path is None:
            if fallback is None:
                raise ValidationError(
                    "A path value is required.",
                    hint="Pass the output directory, e.g. 'build/mkosi'.",
                )
            return fallback
        return Path(path)

    def _normalize_profile_names(self, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise ValidationError(
                "At least one profile name is required.",
                hint="Name at least one variant, e.g. 'default'.",
            )
        normalized: list[str] = []
        for name in names:
            if not name:
                raise ValidationError(
                    "Profile names must be non-empty.",
                    hint="Use a declared variant name, e.g. 'default'.",
                )
            if name not in normalized:
                normalized.append(name)
        return tuple(normalized)

    @contextmanager
    def _operation_scope(self, profiles: Sequence[str] | None) -> Iterator[tuple[str, ...]]:
        """Run an operation on *profiles*, or on the active selection when ``None``."""
        if profiles is None:
            yield self._active_profiles
            return
        names = self._normalize_profile_names(
            (profiles,) if isinstance(profiles, str) else tuple(profiles)
        )
        unknown = [name for name in names if name not in self.state.profiles]
        if unknown:
            raise ValidationError(
                f"Unknown variant(s): {', '.join(unknown)}.",
                hint=f"Declared variants: {', '.join(self.profile_names)}",
            )
        previous = self._active_profiles
        self._active_profiles = names
        try:
            yield names
        finally:
            self._active_profiles = previous

    def _sorted_active_profile_names(self) -> list[str]:
        return sorted(self._active_profiles)

    def _default_lock_path(self) -> Path:
        return self.lock_file if self.lock_file is not None else self.build_dir / "tundravm.lock"

    def _compute_lock_digest(self, fallback_digest: str) -> str:
        lock_path = self._default_lock_path()
        if not lock_path.exists():
            return fallback_digest
        return hashlib.sha256(lock_path.read_bytes()).hexdigest()

    def _resolve_operation_profile(self, profile: str | None) -> str:
        if profile is not None:
            return profile
        if len(self._active_profiles) == 1:
            return self._active_profiles[0]
        raise ValidationError(
            "Operation requires an explicit profile when multiple profiles are active.",
            hint="Pass profile='name' to the operation.",
            context={"operation": "resolve_profile"},
        )

    def _apply_init(self) -> None:
        """Apply Init: generate runtime-init files and inject deps into services.

        Runs on every active profile and on the default profile they extend. An
        extending profile only gets its own runtime-init files when it adds init
        scripts; otherwise it inherits the default profile's.
        """
        if self.init is None:
            return
        default_name = self.state.default_profile
        names = list(self._active_profiles)
        if any(self.state.ensure_profile(n).extends is not None for n in names):
            names.insert(0, default_name)
        targets = [self.state.ensure_profile(n) for n in dict.fromkeys(names)]
        generators: list[ProfileState] = []
        for profile in targets:
            merged = self.init_scripts(profile.name)
            if not merged:
                continue
            network_setup = self._init_needs_network_setup(profile.name)
            if profile.extends is not None and not profile.init_scripts:
                # inherits the default profile's runtime-init files and enablement,
                # unless its own network-setup.service changes the unit
                if network_setup != self._init_needs_network_setup(profile.extends):
                    self.init.apply(profile, scripts=merged, network_setup=network_setup)
                continue
            self.init.apply(profile, scripts=merged, network_setup=network_setup)
            generators.append(profile)
        init_svc = self.init.service_name
        # Inject After/Requires runtime-init.service into the services of every
        # profile that runs runtime-init
        for profile in targets:
            if not self.init_scripts(profile.name):
                continue
            patched: list[ServiceSpec] = []
            for svc in profile.services:
                if svc.name == init_svc or svc.name.endswith(".target") or not svc.after_init:
                    patched.append(svc)
                    continue
                after = svc.after if init_svc in svc.after else (init_svc, *svc.after)
                requires = svc.requires if init_svc in svc.requires else (init_svc, *svc.requires)
                patched.append(replace(svc, after=after, requires=requires))
            profile.services = patched
        # Register runtime-init for enablement (systemctl enable + minimal.target.wants)
        # in each generating profile that does not have it yet.
        for profile in generators:
            if not any(s.name == init_svc for s in profile.services):
                enable_unit(profile, init_svc)

    def _init_needs_network_setup(self, profile_name: str) -> bool:
        """Whether runtime-init requires ``network-setup.service`` in *profile_name*.

        Always under ``nethermind-v1``; otherwise only when the profile declares
        that unit (else runtime-init waits for ``network-online.target``).
        """
        if self.mkosi_for(profile_name).dialect == "nethermind-v1":
            return True
        return ships_unit(self.state.effective_profile(profile_name), NETWORK_SETUP_UNIT)

    def _recipe_payload(self, *, profile_names: tuple[str, ...]) -> dict[str, object]:
        profiles_data: dict[str, dict[str, object]] = {}
        for profile_name in sorted(profile_names):
            profile = self.state.effective_profile(profile_name)
            phases = {
                phase: [
                    {
                        "argv": list(command.argv),
                        "env": dict(command.env),
                        "cwd": command.cwd,
                    }
                    for command in commands
                ]
                for phase, commands in sorted(profile.phases.items())
            }
            repositories = [
                {
                    "name": repository.name,
                    "url": repository.url,
                    "suite": repository.suite,
                    "components": list(repository.components),
                    "keyring": repository.keyring,
                    "priority": repository.priority,
                    **({} if repository.in_image else {"in_image": False}),
                }
                for repository in sorted(
                    profile.repositories,
                    key=lambda item: (item.priority, item.name, item.url),
                )
            ]
            files = [
                {
                    "path": file_entry.path,
                    "mode": file_entry.mode,
                    "sha256": hashlib.sha256(file_entry.data).hexdigest(),
                }
                for file_entry in sorted(profile.files, key=lambda item: item.path)
            ]
            templates = [
                {
                    "path": tmpl.path,
                    "mode": tmpl.mode,
                    "sha256": hashlib.sha256(tmpl.rendered.encode()).hexdigest(),
                    "variables": dict(sorted(tmpl.variables.items())),
                }
                for tmpl in sorted(profile.templates, key=lambda item: item.path)
            ]
            users = [
                {
                    "name": u.name,
                    "system": u.system,
                    "home": u.home,
                    "uid": u.uid,
                    "gid": u.gid,
                    "shell": u.shell,
                    "groups": list(u.groups),
                }
                for u in sorted(profile.users, key=lambda item: item.name)
            ]
            services = [
                {
                    "name": svc.name,
                    "command": list(svc.command),
                    "user": svc.user,
                    "after": list(svc.after),
                    "requires": list(svc.requires),
                    "wants": list(svc.wants),
                    "restart": svc.restart,
                    "enabled": svc.enabled,
                    "security_profile": svc.security_profile,
                    **svc.extras(),
                }
                for svc in sorted(profile.services, key=lambda item: item.name)
            ]
            partitions = [
                {
                    "name": partition.name,
                    "size": partition.size,
                    "mount": partition.mount_at,
                    "fs": partition.fs,
                }
                for partition in sorted(profile.partitions, key=lambda item: item.name)
            ]
            # "after_phase" is no longer declarable; the key stays so digests do not move.
            hooks = [
                {"phase": hook.phase, "after_phase": None, "argv": list(hook.command.argv)}
                for hook in sorted(
                    profile.hooks,
                    key=lambda item: (PHASE_ORDER.index(item.phase), item.command.argv),
                )
            ]
            secrets = [
                {
                    "name": secret.name,
                    "required": secret.required,
                    "schema": None
                    if secret.schema is None
                    else {
                        "kind": secret.schema.kind,
                        "min_length": secret.schema.min_length,
                        "max_length": secret.schema.max_length,
                        "pattern": secret.schema.pattern,
                        "enum": list(secret.schema.enum),
                    },
                    "targets": [
                        {
                            "kind": t.kind,
                            "location": t.location,
                            "mode": t.mode,
                            "scope": t.scope,
                        }
                        for t in secret.targets
                    ],
                }
                for secret in sorted(profile.secrets, key=lambda item: item.name)
            ]
            skeleton_files = [
                {
                    "path": file_entry.path,
                    "mode": file_entry.mode,
                    "sha256": hashlib.sha256(file_entry.data).hexdigest(),
                }
                for file_entry in sorted(profile.skeleton_files, key=lambda item: item.path)
            ]
            # The default profile's payload carries no "extends" key so default-only
            # recipes keep their digests.
            inheritance: dict[str, object] = (
                {} if profile_name == self.state.default_profile else {"extends": profile.extends}
            )
            # Groups and unit states are keyed only when declared so older recipes keep
            # their digests.
            declared: dict[str, object] = {}
            if profile.groups:
                declared["groups"] = [
                    {"name": g.name, "system": g.system, "gid": g.gid}
                    for g in sorted(profile.groups, key=lambda item: item.name)
                ]
            # The default profile's fragments are the top-level "init_scripts".
            own_init = self.state.ensure_profile(profile_name).init_scripts
            if profile_name != self.state.default_profile and own_init:
                declared["init_scripts"] = _init_scripts_payload(own_init)
            if profile.source_builds:
                declared["source_builds"] = {
                    name: spec.to_payload() for name, spec in sorted(profile.source_builds.items())
                }
            if profile.unit_states:
                declared["unit_states"] = {
                    action: [s.unit for s in profile.unit_states if s.action == action]
                    for action in sorted({s.action for s in profile.unit_states})
                }
            profiles_data[profile_name] = {
                **inheritance,
                **declared,
                "packages": sorted(profile.packages),
                "build_packages": sorted(profile.build_packages),
                "build_sources": profile.build_sources,
                "output_targets": list(profile.output_targets),
                "phases": phases,
                "repositories": repositories,
                "files": files,
                "skeleton_files": skeleton_files,
                "templates": templates,
                "users": users,
                "services": services,
                "partitions": partitions,
                "hooks": hooks,
                "secrets": secrets,
                "debloat": {
                    "enabled": profile.debloat.enabled,
                    "paths_remove": list(profile.debloat.effective_paths_remove),
                    "systemd_minimize": profile.debloat.systemd_minimize,
                },
            }

        return {
            "base": self.state.base,
            "arch": self.state.arch,
            "default_profile": self.state.default_profile,
            "init_scripts": _init_scripts_payload(
                self.state.ensure_profile(self.state.default_profile).init_scripts
            ),
            "profiles": profiles_data,
        }

    def _script_checksums(self, scripts: dict[Phase, Path]) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for phase, path in sorted(scripts.items(), key=lambda item: PHASE_ORDER.index(item[0])):
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            checksums[f"{phase}:{path.name}"] = checksum
        return checksums

    def _compile_key(self, digest: str, pins: Mapping[str, LockedFetch]) -> str:
        used = {
            name: pin
            for name, spec in self.lock_sources().items()
            if (pin := spec.pin_from(pins)) is not None
        }
        if not used:
            return digest
        return digest + ":" + hashlib.sha256(json.dumps(used, sort_keys=True).encode()).hexdigest()

    def _enforce_source_policy(self, pins: Mapping[str, LockedFetch]) -> None:
        """Refuse unpinned source builds when ``policy.mutable_ref_policy`` is ``"error"``.

        Under ``"warn"`` compile stays silent: the ``source-unpinned`` check and
        explain's ``pinned=-`` report it, and frozen bakes refuse it.
        """
        if self.policy.mutable_ref_policy != "error":
            return
        unpinned = [spec for spec in self.lock_sources().values() if spec.pin_from(pins) is None]
        if not unpinned:
            return
        names = ", ".join(f"{spec.name}@{spec.source.requested}" for spec in unpinned)
        raise PolicyError(
            f"Unpinned source builds are not allowed by policy: {names}.",
            hint="Run `tundravm lock RECIPE` to pin them, or relax mutable_ref_policy.",
            context={"operation": "compile", "sources": names},
        )

    def _build_pins(self) -> dict[str, LockedFetch]:
        """The lockfile's source pins, then :attr:`fetched_pins` for the sources it lacks."""
        return {**self.source_pins(), **self.fetched_pins}

    def _pinned_state(self, pins: Mapping[str, LockedFetch]) -> RecipeState:
        """The recipe state with every pinned source build's hook rendered at its pin."""
        profiles: dict[str, ProfileState] = {}
        changed = False
        mounted = self.fetches_sources
        for name, profile in self.state.profiles.items():
            swaps = {
                spec.render(mounted=mounted): pinned
                for spec in profile.source_builds.values()
                if (pinned := spec.render(spec.pin_from(pins), mounted=mounted))
                != spec.render(mounted=mounted)
            }
            if not swaps:
                profiles[name] = profile
                continue
            changed = True
            commands = {
                id(hook.command): replace(hook.command, argv=(swaps[hook.command.argv[0]],))
                for hook in profile.hooks
                if hook.command.argv and hook.command.argv[0] in swaps
            }
            profiles[name] = replace(
                profile,
                phases={
                    phase: [commands.get(id(cmd), cmd) for cmd in cmds]
                    for phase, cmds in profile.phases.items()
                },
                hooks=[
                    replace(hook, command=commands[id(hook.command)])
                    if id(hook.command) in commands
                    else hook
                    for hook in profile.hooks
                ],
            )
        return replace(self.state, profiles=profiles) if changed else self.state

    def _fetched_sources(self, destination: Path, backend: BuildBackend) -> Path | None:
        """``<destination>/.sources`` when the build mounts it; fails if a checkout is missing.

        ``None`` under ``nethermind-v1``, without source builds or a built
        kernel, and for the in-process backend, which builds nothing.
        """
        builds = self.lock_sources()
        if not self.fetches_sources or not builds or backend.name == "inprocess":
            return None
        root = destination / SOURCES_DIRNAME
        pins = self._build_pins()
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

    def _assert_frozen_lock(self, *, profile_names: tuple[str, ...]) -> None:
        lock_path = self._default_lock_path()
        lock = read_lockfile(lock_path)
        current_recipe = self._recipe_payload(profile_names=profile_names)
        # A lock written for more variants than this bake selects covers it when
        # every section of the selected variants and the recipe-wide ones matches.
        drift = compare_lock(lock, current_recipe, partial=True)
        if lock.recipe_digest == recipe_digest(current_recipe) or drift.is_clean:
            self._assert_sources_pinned(lock_path)
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

    def _assert_sources_pinned(self, lock_path: Path) -> None:
        unpinned = self.unpinned_sources(lock_path)
        if unpinned:
            raise LockfileError(
                f"Frozen bake requires every source build to be pinned; unpinned: "
                f"{', '.join(unpinned)}.",
                hint="run tundravm lock RECIPE to pin them.",
                context={"lock": str(lock_path), "sources": ", ".join(unpinned)},
            )
