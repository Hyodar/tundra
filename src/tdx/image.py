"""Core image object for SDK recipe declarations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from .errors import ValidationError
from .models import (
    Arch,
    ArtifactRef,
    BakeResult,
    CommandSpec,
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
                env=env_data,
                cwd=cwd,
                shell=shell,
            )
            profile.phases.setdefault(phase, []).append(command)
        return self

    def lock(self, path: str | Path | None = None) -> Path:
        lock_path = self._normalize_path(path, fallback=self.build_dir / "tdx.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._recipe_payload()
        lock_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return lock_path

    def emit_mkosi(self, path: str | Path) -> Path:
        destination = self._normalize_path(path)
        destination.mkdir(parents=True, exist_ok=True)
        for profile_name in sorted(self._state.profiles):
            profile = self._state.profiles[profile_name]
            packages = " ".join(sorted(profile.packages))
            lines = [
                "[Distribution]",
                f"Base={self.base}",
                "",
                "[Output]",
                f"ImageId={profile_name}",
                "",
                "[Content]",
                f"Packages={packages}",
            ]
            conf_path = destination / f"mkosi.{profile_name}.conf"
            conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination

    def bake(self, output_dir: str | Path | None = None) -> BakeResult:
        destination = self._normalize_path(output_dir, fallback=self.build_dir)
        destination.mkdir(parents=True, exist_ok=True)
        profiles_result: dict[str, ProfileBuildResult] = {}
        for profile_name in sorted(self._state.profiles):
            profile = self._state.profiles[profile_name]
            profile_dir = destination / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)
            profile_result = ProfileBuildResult(profile=profile_name)
            for target in profile.output_targets:
                artifact_path = profile_dir / self._artifact_filename(target)
                artifact_path.write_text(
                    f"tdxvm placeholder artifact: profile={profile_name}, target={target}\n",
                    encoding="utf-8",
                )
                profile_result.artifacts[target] = ArtifactRef(target=target, path=artifact_path)
            profiles_result[profile_name] = profile_result
        return BakeResult(profiles=profiles_result)

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

    def _iter_active_profiles(self) -> list[ProfileState]:
        profiles: list[ProfileState] = []
        for profile_name in self._active_profiles:
            profiles.append(self._state.ensure_profile(profile_name))
        return profiles

    def _recipe_payload(self) -> dict[str, object]:
        profiles_data: dict[str, dict[str, object]] = {}
        for profile_name in sorted(self._state.profiles):
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
