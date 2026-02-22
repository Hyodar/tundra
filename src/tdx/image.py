"""Core image object for SDK recipe declarations."""

from __future__ import annotations

import hashlib
import json
import shutil
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

from .cache import BuildCacheInput, BuildCacheStore, cache_key
from .compiler import PHASE_ORDER, emit_mkosi_tree
from .deploy import get_adapter
from .errors import DeploymentError, LockfileError, MeasurementError, ValidationError
from .lockfile import build_lockfile, read_lockfile, recipe_digest, write_lockfile
from .measure import Measurements, derive_measurements
from .models import (
    DEFAULT_DEBLOAT_MASK,
    DEFAULT_DEBLOAT_REMOVE,
    Arch,
    ArtifactRef,
    BakeResult,
    CommandSpec,
    DeployRequest,
    DeployResult,
    FileEntry,
    HookSpec,
    OutputTarget,
    PartitionSpec,
    Phase,
    ProfileBuildResult,
    ProfileState,
    RecipeState,
    RepositorySpec,
    SecretSchema,
    SecretSpec,
    SecretTarget,
    ServiceSpec,
    TemplateEntry,
    UserSpec,
)
from .observability import StructuredLogger
from .policy import Policy, ensure_bake_policy


@runtime_checkable
class ImageModule(Protocol):
    """Runtime protocol for modules that support setup/install composition."""

    def setup(self, image: Image) -> None:
        """Apply one-time module setup to the recipe."""

    def install(self, image: Image) -> None:
        """Apply module installation/runtime declarations to the recipe."""


@runtime_checkable
class DependencyAwareImageModule(ImageModule, Protocol):
    """Optional protocol for modules with host command prerequisites."""

    def required_host_commands(self) -> tuple[str, ...]:
        """Return host commands that must exist before this module can be used."""


@dataclass(slots=True)
class Image:
    """Represents an image recipe root."""

    build_dir: Path = field(default_factory=lambda: Path("build"))
    base: str = "debian/bookworm"
    arch: Arch = "x86_64"
    default_profile: str = "default"
    policy: Policy = field(default_factory=Policy)
    logger: StructuredLogger = field(default_factory=StructuredLogger)
    _state: RecipeState = field(init=False, repr=False)
    _active_profiles: tuple[str, ...] = field(init=False, repr=False)
    _last_bake_result: BakeResult | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._state = RecipeState.initialize(
            base=self.base,
            arch=self.arch,
            default_profile=self.default_profile,
        )
        self._active_profiles = (self.default_profile,)

    @property
    def state(self) -> RecipeState:
        return self._state

    def set_policy(self, policy: Policy) -> Self:
        self.policy = policy
        return self

    @contextmanager
    def profile(self, name: str) -> Iterator[Self]:
        with self.profiles(name):
            yield self

    @contextmanager
    def profiles(self, *names: str) -> Iterator[Self]:
        selected = self._normalize_profile_names(names)
        previous_profiles = self._active_profiles
        for profile_name in selected:
            self._state.ensure_profile(profile_name)
        self._active_profiles = selected
        try:
            yield self
        finally:
            self._active_profiles = previous_profiles

    @contextmanager
    def all_profiles(self) -> Iterator[Self]:
        with self.profiles(*tuple(sorted(self._state.profiles))):
            yield self

    def install(self, *packages: str) -> Self:
        if not packages:
            raise ValidationError("install() requires at least one package.")
        for package in packages:
            if not package:
                raise ValidationError("Package names must be non-empty.")
        for profile in self._iter_active_profiles():
            profile.packages.update(packages)
        return self

    def repository(self, name: str, url: str, *, priority: int = 100) -> Self:
        if not name:
            raise ValidationError("repository() requires a non-empty name.")
        if not url:
            raise ValidationError("repository() requires a non-empty URL.")
        for profile in self._iter_active_profiles():
            profile.repositories.append(RepositorySpec(name=name, url=url, priority=priority))
        return self

    def file(
        self,
        path: str,
        *,
        content: str | None = None,
        src: str | Path | None = None,
        mode: str = "0644",
    ) -> Self:
        if not path:
            raise ValidationError("file() requires a destination path.")
        if (content is None) == (src is None):
            raise ValidationError("file() requires exactly one of content= or src=.")
        if content is not None:
            resolved_content = content
        else:
            if src is None:
                raise ValidationError("file() requires src when content is not provided.")
            resolved_content = Path(src).read_text(encoding="utf-8")
        for profile in self._iter_active_profiles():
            profile.files.append(FileEntry(path=path, content=resolved_content, mode=mode))
        return self

    def template(
        self,
        path: str,
        *,
        template: str,
        variables: Mapping[str, str],
        mode: str = "0644",
    ) -> Self:
        if not path:
            raise ValidationError("template() requires a destination path.")
        ordered_variables = dict(sorted(variables.items()))
        try:
            rendered = template.format_map(ordered_variables)
        except KeyError as exc:
            raise ValidationError(
                "template() variables are missing required placeholders.",
                hint="Provide all placeholder keys used in the template string.",
                context={"path": path, "missing_key": str(exc)},
            ) from exc
        entry = TemplateEntry(
            path=path,
            template=template,
            variables=ordered_variables,
            rendered=rendered,
            mode=mode,
        )
        for profile in self._iter_active_profiles():
            profile.templates.append(entry)
        return self

    def user(
        self,
        name: str,
        *,
        uid: int | None = None,
        gid: int | None = None,
        shell: str = "/usr/sbin/nologin",
    ) -> Self:
        if not name:
            raise ValidationError("user() requires a non-empty user name.")
        entry = UserSpec(name=name, uid=uid, gid=gid, shell=shell)
        for profile in self._iter_active_profiles():
            profile.users.append(entry)
        return self

    def service(self, name: str, *, enabled: bool = True, wants: tuple[str, ...] = ()) -> Self:
        if not name:
            raise ValidationError("service() requires a non-empty service name.")
        entry = ServiceSpec(name=name, enabled=enabled, wants=tuple(dict.fromkeys(wants)))
        for profile in self._iter_active_profiles():
            profile.services.append(entry)
        return self

    def partition(self, name: str, *, size: str, mount: str, fs: str = "ext4") -> Self:
        if not name:
            raise ValidationError("partition() requires a non-empty name.")
        if not size or not mount:
            raise ValidationError("partition() requires both size and mount values.")
        entry = PartitionSpec(name=name, size=size, mount=mount, fs=fs)
        for profile in self._iter_active_profiles():
            profile.partitions.append(entry)
        return self

    def secret(
        self,
        name: str,
        *,
        required: bool = True,
        schema: SecretSchema | None = None,
        targets: tuple[SecretTarget, ...] = (),
    ) -> SecretSpec:
        if not name:
            raise ValidationError("secret() requires a non-empty secret name.")
        if not targets:
            raise ValidationError("secret() requires at least one delivery target.")
        entry = SecretSpec(name=name, required=required, schema=schema, targets=targets)
        for profile in self._iter_active_profiles():
            profile.secrets.append(entry)
        return entry

    def use(self, *modules: ImageModule) -> Self:
        if not modules:
            raise ValidationError("use() requires at least one module instance.")
        for module in modules:
            self._ensure_module_host_dependencies(module)
            module.setup(self)
            module.install(self)
        return self

    def output_targets(self, *targets: OutputTarget) -> Self:
        if not targets:
            raise ValidationError("output_targets() requires at least one target.")
        deduped = tuple(dict.fromkeys(targets))
        for profile in self._iter_active_profiles():
            profile.output_targets = deduped
        return self

    def debloat(
        self,
        *,
        enabled: bool = True,
        remove: tuple[str, ...] | None = None,
        mask: tuple[str, ...] | None = None,
    ) -> Self:
        remove_items = tuple(sorted(dict.fromkeys(remove or DEFAULT_DEBLOAT_REMOVE)))
        mask_items = tuple(sorted(dict.fromkeys(mask or DEFAULT_DEBLOAT_MASK)))
        for profile in self._iter_active_profiles():
            profile.debloat_enabled = enabled
            profile.debloat_remove = remove_items if enabled else ()
            profile.debloat_mask = mask_items if enabled else ()
        return self

    def explain_debloat(self, *, profile: str | None = None) -> dict[str, object]:
        selected_profile = self._resolve_operation_profile(profile)
        profile_state = self._state.ensure_profile(selected_profile)
        return {
            "profile": selected_profile,
            "enabled": profile_state.debloat_enabled,
            "remove": list(profile_state.debloat_remove),
            "mask": list(profile_state.debloat_mask),
        }

    def run(
        self,
        *argv: str,
        phase: Phase = "postinst",
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        shell: bool = False,
    ) -> Self:
        return self.hook(
            phase,
            *argv,
            env=env,
            cwd=cwd,
            shell=shell,
        )

    def hook(
        self,
        phase: Phase,
        *argv: str,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        shell: bool = False,
        after_phase: Phase | None = None,
    ) -> Self:
        if not argv:
            raise ValidationError("hook() requires a command argv.")
        self._validate_phase_order(phase=phase, after_phase=after_phase)
        env_data = dict(env or {})
        for profile in self._iter_active_profiles():
            command = CommandSpec(
                argv=tuple(argv),
                env=dict(env_data),
                cwd=cwd,
                shell=shell,
            )
            profile.phases.setdefault(phase, []).append(command)
            profile.hooks.append(HookSpec(phase=phase, command=command, after_phase=after_phase))
        return self

    def lock(self, path: str | Path | None = None) -> Path:
        lock_path = self._normalize_path(path, fallback=self._default_lock_path())
        payload = self._recipe_payload(profile_names=self._active_profiles)
        lock = build_lockfile(recipe=payload)
        return write_lockfile(lock, lock_path)

    def emit_mkosi(self, path: str | Path) -> Path:
        destination = self._normalize_path(path)
        emit_mkosi_tree(
            recipe=self._state,
            destination=destination,
            profile_names=self._active_profiles,
            base=self.base,
        )
        return destination

    def bake(self, output_dir: str | Path | None = None, *, frozen: bool = False) -> BakeResult:
        ensure_bake_policy(policy=self.policy, frozen=frozen)
        warnings.warn(
            (
                "Image.bake() is currently simulated: deterministic placeholder artifacts are "
                "produced and mkosi/backends are not invoked yet."
            ),
            RuntimeWarning,
            stacklevel=2,
        )
        if frozen:
            self._assert_frozen_lock(profile_names=self._active_profiles)
        destination = self._normalize_path(output_dir, fallback=self.build_dir)
        destination.mkdir(parents=True, exist_ok=True)
        recipe_lock_digest = recipe_digest(
            self._recipe_payload(profile_names=self._active_profiles),
        )
        lock_digest = self._compute_lock_digest(recipe_lock_digest)
        profiles_result: dict[str, ProfileBuildResult] = {}
        for profile_name in self._sorted_active_profile_names():
            profile = self._state.profiles[profile_name]
            profile_dir = destination / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)
            profile_result = ProfileBuildResult(profile=profile_name)
            self.logger.log(
                operation="bake_profile_start",
                profile=profile_name,
                phase="build",
                module="image",
                builder="converter",
                message="Starting profile bake.",
            )
            cache_hits: list[str] = []
            cache_misses: list[str] = []
            source_artifact = profile_dir / "image.raw"
            source_artifact.write_text(
                (
                    f"tdxvm base artifact: profile={profile_name}\n"
                    f"debloat_enabled={profile.debloat_enabled}\n"
                    f"debloat_remove={','.join(profile.debloat_remove)}\n"
                    f"debloat_mask={','.join(profile.debloat_mask)}\n"
                ),
                encoding="utf-8",
            )
            for target in profile.output_targets:
                artifact_ref, cache_hit = self._convert_artifact(
                    source_artifact=source_artifact,
                    profile_name=profile_name,
                    target=target,
                    dependencies=tuple(sorted(profile.packages)),
                )
                profile_result.artifacts[target] = artifact_ref
                if cache_hit:
                    cache_hits.append(target)
                else:
                    cache_misses.append(target)
                self.logger.log(
                    operation="convert_artifact",
                    profile=profile_name,
                    phase="build",
                    module="image",
                    builder="converter",
                    message="Converted target artifact.",
                    extra={"target": target, "cache_hit": cache_hit},
                )

            emission_root = profile_dir / "mkosi"
            emission = emit_mkosi_tree(
                recipe=self._state,
                destination=emission_root,
                profile_names=(profile_name,),
                base=self.base,
            )
            script_checksums = self._script_checksums(emission.script_paths.get(profile_name, {}))
            artifact_digests = {
                target: hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest()
                for target, artifact in sorted(profile_result.artifacts.items())
            }
            profile_logs = self.logger.records_for_profile(profile_name)

            report_path = profile_dir / "report.json"
            report_payload = {
                "profile": profile_name,
                "lock_digest": lock_digest,
                "implementation_mode": "simulated",
                "warnings": [
                    (
                        "Artifacts in this report were generated by the SDK simulation path. "
                        "Provider conversion and mkosi execution are not active yet."
                    ),
                ],
                "cache": {
                    "hits": cache_hits,
                    "misses": cache_misses,
                },
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
            profile_result.report_path = report_path
            profiles_result[profile_name] = profile_result
            self.logger.log(
                operation="bake_profile_complete",
                profile=profile_name,
                phase="build",
                module="image",
                builder="converter",
                message="Completed profile bake.",
            )
        bake_result = BakeResult(profiles=profiles_result)
        self._last_bake_result = bake_result
        return bake_result

    def measure(
        self,
        *,
        backend: Literal["rtmr", "azure", "gcp"],
        profile: str | None = None,
    ) -> Measurements:
        warnings.warn(
            (
                "Image.measure() currently derives deterministic simulated values from baked "
                "artifacts, not hardware-backed RTMR/PCR extraction."
            ),
            RuntimeWarning,
            stacklevel=2,
        )
        selected_profile = self._resolve_operation_profile(profile)
        if self._last_bake_result is None:
            raise MeasurementError(
                "No baked artifacts are available for measurement.",
                hint="Run bake() before measure().",
                context={"operation": "measure", "profile": selected_profile, "backend": backend},
            )
        profile_result = self._last_bake_result.profiles.get(selected_profile)
        if profile_result is None:
            raise MeasurementError(
                "Profile has no baked artifacts for measurement.",
                hint="Bake the selected profile before measure().",
                context={"operation": "measure", "profile": selected_profile, "backend": backend},
            )
        return derive_measurements(
            backend=backend,
            profile=selected_profile,
            profile_result=profile_result,
        )

    def deploy(
        self,
        *,
        target: OutputTarget,
        profile: str | None = None,
        parameters: Mapping[str, str] | None = None,
    ) -> DeployResult:
        warnings.warn(
            (
                "Image.deploy() currently returns simulated deployment metadata and does not "
                "call provider APIs yet."
            ),
            RuntimeWarning,
            stacklevel=2,
        )
        selected_profile = self._resolve_operation_profile(profile)
        if self._last_bake_result is None:
            raise DeploymentError(
                "No baked artifacts are available for deployment.",
                hint="Run bake() before deploy().",
                context={"operation": "deploy", "profile": selected_profile, "target": target},
            )

        artifact = self._last_bake_result.artifact_for(profile=selected_profile, target=target)
        if artifact is None:
            raise DeploymentError(
                "Requested deploy target artifact was not baked.",
                hint="Add the target via output_targets(...) and rerun bake().",
                context={"operation": "deploy", "profile": selected_profile, "target": target},
            )

        request = DeployRequest(
            profile=selected_profile,
            target=target,
            artifact_path=artifact.path,
            parameters=dict(parameters or {}),
        )
        return get_adapter(target).deploy(request)

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
                raise ValidationError("A path value is required.")
            return fallback
        return Path(path)

    def _normalize_profile_names(self, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise ValidationError("At least one profile name is required.")
        normalized: list[str] = []
        for name in names:
            if not name:
                raise ValidationError("Profile names must be non-empty.")
            if name not in normalized:
                normalized.append(name)
        return tuple(normalized)

    def _validate_phase_order(self, *, phase: Phase, after_phase: Phase | None) -> None:
        if after_phase is None:
            return
        phase_index = PHASE_ORDER.index(phase)
        after_index = PHASE_ORDER.index(after_phase)
        if after_index >= phase_index:
            raise ValidationError(
                "Invalid phase hook dependency order.",
                hint="after_phase must be earlier than the hook phase.",
                context={"phase": phase, "after_phase": after_phase},
            )

    def _sorted_active_profile_names(self) -> list[str]:
        return sorted(self._active_profiles)

    def _default_lock_path(self) -> Path:
        return self.build_dir / "tdx.lock"

    def _compute_lock_digest(self, fallback_digest: str) -> str:
        lock_path = self._default_lock_path()
        if not lock_path.exists():
            return fallback_digest
        return hashlib.sha256(lock_path.read_bytes()).hexdigest()

    def _cache_store(self) -> BuildCacheStore:
        return BuildCacheStore(self.build_dir / ".cache" / "conversion")

    def _resolve_operation_profile(self, profile: str | None) -> str:
        if profile is not None:
            return profile
        if len(self._active_profiles) == 1:
            return self._active_profiles[0]
        raise ValidationError(
            "Operation requires an explicit profile when multiple profiles are active.",
            hint="Pass profile='name' to deploy().",
            context={"operation": "deploy"},
        )

    def _iter_active_profiles(self) -> list[ProfileState]:
        profiles: list[ProfileState] = []
        for profile_name in self._active_profiles:
            profiles.append(self._state.ensure_profile(profile_name))
        return profiles

    def _ensure_module_host_dependencies(self, module: ImageModule) -> None:
        if not isinstance(module, DependencyAwareImageModule):
            return
        commands = tuple(dict.fromkeys(module.required_host_commands()))
        missing = tuple(command for command in commands if shutil.which(command) is None)
        if not missing:
            return
        raise ValidationError(
            "Module host dependencies are missing.",
            hint="Install required commands before applying the module.",
            context={
                "module": type(module).__name__,
                "missing_commands": ",".join(missing),
            },
        )

    def _recipe_payload(self, *, profile_names: tuple[str, ...]) -> dict[str, object]:
        profiles_data: dict[str, dict[str, object]] = {}
        for profile_name in sorted(profile_names):
            profile = self._state.profiles[profile_name]
            phases = {
                phase: [
                    {
                        "argv": list(command.argv),
                        "env": dict(command.env),
                        "cwd": command.cwd,
                        "shell": command.shell,
                    }
                    for command in commands
                ]
                for phase, commands in sorted(profile.phases.items())
            }
            repositories = [
                {
                    "name": repository.name,
                    "url": repository.url,
                    "priority": repository.priority,
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
                    "sha256": hashlib.sha256(file_entry.content.encode()).hexdigest(),
                }
                for file_entry in sorted(profile.files, key=lambda item: item.path)
            ]
            templates = [
                {
                    "path": template.path,
                    "mode": template.mode,
                    "sha256": hashlib.sha256(template.rendered.encode()).hexdigest(),
                    "variables": dict(sorted(template.variables.items())),
                }
                for template in sorted(profile.templates, key=lambda item: item.path)
            ]
            users = [
                {
                    "name": user.name,
                    "uid": user.uid,
                    "gid": user.gid,
                    "shell": user.shell,
                }
                for user in sorted(profile.users, key=lambda item: item.name)
            ]
            services = [
                {
                    "name": service.name,
                    "enabled": service.enabled,
                    "wants": list(service.wants),
                }
                for service in sorted(profile.services, key=lambda item: item.name)
            ]
            partitions = [
                {
                    "name": partition.name,
                    "size": partition.size,
                    "mount": partition.mount,
                    "fs": partition.fs,
                }
                for partition in sorted(profile.partitions, key=lambda item: item.name)
            ]
            hooks = [
                {
                    "phase": hook.phase,
                    "after_phase": hook.after_phase,
                    "argv": list(hook.command.argv),
                }
                for hook in sorted(
                    profile.hooks,
                    key=lambda item: (
                        PHASE_ORDER.index(item.phase),
                        item.after_phase or "",
                        item.command.argv,
                    ),
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
                            "kind": target.kind,
                            "location": target.location,
                            "mode": target.mode,
                            "scope": target.scope,
                        }
                        for target in secret.targets
                    ],
                }
                for secret in sorted(profile.secrets, key=lambda item: item.name)
            ]
            profiles_data[profile_name] = {
                "packages": sorted(profile.packages),
                "build_packages": sorted(profile.build_packages),
                "output_targets": list(profile.output_targets),
                "phases": phases,
                "repositories": repositories,
                "files": files,
                "templates": templates,
                "users": users,
                "services": services,
                "partitions": partitions,
                "hooks": hooks,
                "secrets": secrets,
                "debloat": {
                    "enabled": profile.debloat_enabled,
                    "remove": list(profile.debloat_remove),
                    "mask": list(profile.debloat_mask),
                },
            }

        return {
            "base": self._state.base,
            "arch": self._state.arch,
            "default_profile": self._state.default_profile,
            "profiles": profiles_data,
        }

    def _script_checksums(self, scripts: dict[Phase, Path]) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for phase, path in sorted(scripts.items(), key=lambda item: PHASE_ORDER.index(item[0])):
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            checksums[f"{phase}:{path.name}"] = checksum
        return checksums

    def _assert_frozen_lock(self, *, profile_names: tuple[str, ...]) -> None:
        lock_path = self._default_lock_path()
        lock = read_lockfile(lock_path)
        current_recipe = self._recipe_payload(profile_names=profile_names)
        current_digest = recipe_digest(current_recipe)
        if lock.recipe_digest != current_digest:
            raise LockfileError(
                "Frozen bake lockfile is stale for current recipe state.",
                hint="Re-run img.lock() and commit the updated lockfile.",
                context={
                    "operation": "bake",
                    "mode": "frozen",
                    "expected": current_digest,
                    "actual": lock.recipe_digest,
                    "path": str(lock_path),
                },
            )

    def _convert_artifact(
        self,
        *,
        source_artifact: Path,
        profile_name: str,
        target: OutputTarget,
        dependencies: tuple[str, ...],
    ) -> tuple[ArtifactRef, bool]:
        artifact_path = source_artifact.parent / self._artifact_filename(target)
        source_hash = hashlib.sha256(source_artifact.read_bytes()).hexdigest()
        inputs = BuildCacheInput(
            source_hash=source_hash,
            source_tree=source_hash,
            toolchain="converter-v1",
            flags=(f"target={target}",),
            dependencies=dependencies,
            env={},
            target=target,
        )
        key = cache_key(inputs)
        cache_store = self._cache_store()
        cached_payload = cache_store.load(key=key, expected_inputs=inputs)
        if cached_payload is not None:
            artifact_path.write_bytes(cached_payload)
            return ArtifactRef(target=target, path=artifact_path), True

        payload = (
            "tdxvm converted artifact:\n"
            f"profile={profile_name}\n"
            f"target={target}\n"
            f"source={source_artifact.name}\n"
        ).encode()
        artifact_path.write_bytes(payload)
        cache_store.save(inputs=inputs, artifact=payload)
        return ArtifactRef(target=target, path=artifact_path), False
