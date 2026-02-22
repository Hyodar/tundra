"""Deterministic mkosi emission pipeline."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tdx.errors import ValidationError
from tdx.models import CommandSpec, Phase, RecipeState

PHASE_ORDER: tuple[Phase, ...] = (
    "sync",
    "skeleton",
    "prepare",
    "build",
    "extra",
    "postinst",
    "finalize",
    "postoutput",
    "clean",
    "repart",
    "boot",
)

PHASE_TO_MKOSI_KEY: dict[Phase, str] = {
    "sync": "SyncScripts",
    "skeleton": "SkeletonScripts",
    "prepare": "PrepareScripts",
    "build": "BuildScripts",
    "extra": "BuildSourcesEphemeral",
    "postinst": "PostInstallationScripts",
    "finalize": "FinalizeScripts",
    "postoutput": "PostOutputScripts",
    "clean": "CleanScripts",
    "repart": "RepartScripts",
    "boot": "BootScripts",
}


@dataclass(frozen=True, slots=True)
class MkosiEmission:
    root: Path
    profile_paths: dict[str, Path] = field(default_factory=dict)
    script_paths: dict[str, dict[Phase, Path]] = field(default_factory=dict)


class MkosiEmitter(Protocol):
    def emit(
        self,
        *,
        recipe: RecipeState,
        destination: Path,
        profile_names: tuple[str, ...],
        base: str,
    ) -> MkosiEmission:
        """Emit mkosi tree and return metadata about generated files."""


class DeterministicMkosiEmitter:
    """Emit deterministic mkosi profile trees with ordered phase scripts."""

    def emit(
        self,
        *,
        recipe: RecipeState,
        destination: Path,
        profile_names: tuple[str, ...],
        base: str,
    ) -> MkosiEmission:
        destination.mkdir(parents=True, exist_ok=True)
        profile_paths: dict[str, Path] = {}
        script_paths: dict[str, dict[Phase, Path]] = {}
        for profile_name in sorted(profile_names):
            profile = recipe.profiles.get(profile_name)
            if profile is None:
                raise ValidationError(
                    "Profile does not exist for mkosi emission.",
                    hint="Create the profile before calling emit_mkosi().",
                    context={"profile": profile_name, "operation": "emit_mkosi"},
                )
            self._validate_profile_phases(profile_name=profile_name, recipe=recipe)
            profile_dir = destination / profile_name
            scripts_dir = profile_dir / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)

            phase_scripts = self._emit_phase_scripts(
                profile_name=profile_name,
                scripts_dir=scripts_dir,
                recipe=recipe,
            )
            conf_content = self._render_conf(
                profile_name=profile_name,
                base=base,
                packages=sorted(profile.packages),
                build_packages=sorted(profile.build_packages),
                phase_scripts=phase_scripts,
            )

            conf_path = profile_dir / "mkosi.conf"
            conf_path.write_text(conf_content, encoding="utf-8")

            profile_paths[profile_name] = conf_path
            script_paths[profile_name] = phase_scripts
        return MkosiEmission(
            root=destination,
            profile_paths=profile_paths,
            script_paths=script_paths,
        )

    def _validate_profile_phases(self, *, profile_name: str, recipe: RecipeState) -> None:
        profile = recipe.profiles[profile_name]
        allowed = set(PHASE_ORDER)
        for phase in profile.phases:
            if phase not in allowed:
                raise ValidationError(
                    "Invalid phase name for mkosi emission.",
                    hint="Use a phase from the documented phase order.",
                    context={"phase": str(phase), "profile": profile_name},
                )

    def _emit_phase_scripts(
        self,
        *,
        profile_name: str,
        scripts_dir: Path,
        recipe: RecipeState,
    ) -> dict[Phase, Path]:
        profile = recipe.profiles[profile_name]
        script_paths: dict[Phase, Path] = {}
        for index, phase in enumerate(PHASE_ORDER, start=1):
            commands = profile.phases.get(phase, [])
            if not commands:
                continue
            script_name = f"{index:02d}-{phase}.sh"
            script_path = scripts_dir / script_name
            script_path.write_text(self._render_script(commands), encoding="utf-8")
            script_paths[phase] = script_path
        return script_paths

    def _render_conf(
        self,
        *,
        profile_name: str,
        base: str,
        packages: list[str],
        build_packages: list[str],
        phase_scripts: dict[Phase, Path],
    ) -> str:
        lines = [
            "[Distribution]",
            f"Base={base}",
            "",
            "[Output]",
            f"ImageId={profile_name}",
            "",
            "[Content]",
            f"Packages={' '.join(packages)}",
            f"BuildPackages={' '.join(build_packages)}",
        ]
        if phase_scripts:
            lines.extend(["", "[Scripts]"])
            for phase in PHASE_ORDER:
                script_path = phase_scripts.get(phase)
                if script_path is None:
                    continue
                lines.append(f"{PHASE_TO_MKOSI_KEY[phase]}=scripts/{script_path.name}")
        return "\n".join(lines) + "\n"

    def _render_script(self, commands: list[CommandSpec]) -> str:
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for command in commands:
            env_prefix = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in sorted(command.env.items())
            )
            rendered_argv = " ".join(shlex.quote(part) for part in command.argv)
            rendered = rendered_argv if not env_prefix else f"{env_prefix} {rendered_argv}"
            if command.cwd is not None:
                rendered = f"(cd {shlex.quote(command.cwd)} && {rendered})"
            if command.shell:
                rendered = f"bash -lc {shlex.quote(rendered)}"
            lines.append(rendered)
        lines.append("")
        return "\n".join(lines)


def emit_mkosi_tree(
    *,
    recipe: RecipeState,
    destination: Path,
    profile_names: tuple[str, ...],
    base: str,
) -> MkosiEmission:
    emitter = DeterministicMkosiEmitter()
    return emitter.emit(
        recipe=recipe,
        destination=destination,
        profile_names=profile_names,
        base=base,
    )
