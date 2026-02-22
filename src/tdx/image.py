"""Core image object for SDK recipe declarations."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from .compiler import emit_mkosi_tree
from .errors import DeploymentError, ValidationError
from .models import (
    Arch,
    ArtifactRef,
    BakeResult,
    CommandSpec,
    DeployResult,
    OutputTarget,
    Phase,
    ProfileBuildResult,
    ProfileState,
    RecipeState,
)


@dataclass(slots=True)
class Image:
    """Represents an image recipe root."""

    build_dir: Path = field(default_factory=lambda: Path("build"))
    base: str = "debian/bookworm"
    arch: Arch = "x86_64"
    default_profile: str = "default"
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

    def output_targets(self, *targets: OutputTarget) -> Self:
        if not targets:
            raise ValidationError("output_targets() requires at least one target.")
        deduped = tuple(dict.fromkeys(targets))
        for profile in self._iter_active_profiles():
            profile.output_targets = deduped
        return self

    def run(
        self,
        phase: Phase,
        *argv: str,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        shell: bool = False,
    ) -> Self:
        if not argv:
            raise ValidationError("run() requires a command argv.")
        env_data = dict(env or {})
        for profile in self._iter_active_profiles():
            command = CommandSpec(
                argv=tuple(argv),
                env=dict(env_data),
                cwd=cwd,
                shell=shell,
            )
            profile.phases.setdefault(phase, []).append(command)
        return self

    def lock(self, path: str | Path | None = None) -> Path:
        lock_path = self._normalize_path(path, fallback=self.build_dir / "tdx.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._recipe_payload(profile_names=self._active_profiles)
        lock_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return lock_path

    def emit_mkosi(self, path: str | Path) -> Path:
        destination = self._normalize_path(path)
        emit_mkosi_tree(
            recipe=self._state,
            destination=destination,
            profile_names=self._active_profiles,
            base=self.base,
        )
        return destination

    def bake(self, output_dir: str | Path | None = None) -> BakeResult:
        destination = self._normalize_path(output_dir, fallback=self.build_dir)
        destination.mkdir(parents=True, exist_ok=True)
        profiles_result: dict[str, ProfileBuildResult] = {}
        for profile_name in self._sorted_active_profile_names():
            profile = self._state.profiles[profile_name]
            profile_dir = destination / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)
            profile_result = ProfileBuildResult(profile=profile_name)
            source_artifact = profile_dir / "image.raw"
            source_artifact.write_text(
                f"tdxvm base artifact: profile={profile_name}\n",
                encoding="utf-8",
            )
            for target in profile.output_targets:
                artifact_ref = self._convert_artifact(
                    source_artifact=source_artifact,
                    profile_name=profile_name,
                    target=target,
                )
                profile_result.artifacts[target] = artifact_ref
            profiles_result[profile_name] = profile_result
        bake_result = BakeResult(profiles=profiles_result)
        self._last_bake_result = bake_result
        return bake_result

    def deploy(
        self,
        *,
        target: OutputTarget,
        profile: str | None = None,
        parameters: Mapping[str, str] | None = None,
    ) -> DeployResult:
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

        deployment_id = f"local-{selected_profile}-{target}"
        metadata: dict[str, str] = {"artifact_path": str(artifact.path)}
        if parameters is not None:
            metadata.update(parameters)
        return DeployResult(
            target=target,
            deployment_id=deployment_id,
            metadata=metadata,
        )

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

    def _sorted_active_profile_names(self) -> list[str]:
        return sorted(self._active_profiles)

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
            profiles_data[profile_name] = {
                "packages": sorted(profile.packages),
                "build_packages": sorted(profile.build_packages),
                "output_targets": list(profile.output_targets),
                "phases": phases,
            }

        return {
            "base": self._state.base,
            "arch": self._state.arch,
            "default_profile": self._state.default_profile,
            "profiles": profiles_data,
        }

    def _convert_artifact(
        self,
        *,
        source_artifact: Path,
        profile_name: str,
        target: OutputTarget,
    ) -> ArtifactRef:
        artifact_path = source_artifact.parent / self._artifact_filename(target)
        artifact_path.write_text(
            (
                "tdxvm converted artifact:\n"
                f"profile={profile_name}\n"
                f"target={target}\n"
                f"source={source_artifact.name}\n"
            ),
            encoding="utf-8",
        )
        return ArtifactRef(target=target, path=artifact_path)
