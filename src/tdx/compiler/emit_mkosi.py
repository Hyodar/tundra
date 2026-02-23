"""Real mkosi project tree emission pipeline.

Generates complete, buildable mkosi project directories per profile, including:
- mkosi.conf with proper Distribution, Output, Build, Content, Scripts sections
- mkosi.extra/ overlay tree (files, templates, systemd unit files)
- mkosi.skeleton/ pre-package-manager tree
- scripts/ for postinst (user creation, service enablement, debloat masking),
  finalize (debloat path removal), and user-defined phase commands
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tdx.errors import ValidationError
from tdx.models import (
    Arch,
    CommandSpec,
    Kernel,
    Phase,
    ProfileState,
    RecipeState,
    RepositorySpec,
    ServiceSpec,
    UserSpec,
)

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

# Stable seed for reproducible partition UUIDs
DEFAULT_SEED = "7a9ceb63-4a2c-4a85-9c36-1e0e3a8f7b5d"


@dataclass(frozen=True, slots=True)
class EmitConfig:
    """Configuration passed from Image to the emitter."""

    base: str
    arch: Arch = "x86_64"
    reproducible: bool = True
    kernel: Kernel | None = None
    output_format: str = "uki"
    seed: str = DEFAULT_SEED


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
        config: EmitConfig | None = None,
    ) -> MkosiEmission:
        """Emit mkosi tree and return metadata about generated files."""


def _parse_base(base: str) -> tuple[str, str]:
    """Parse 'debian/bookworm' into ('debian', 'bookworm')."""
    if "/" in base:
        parts = base.split("/", 1)
        return parts[0], parts[1]
    return base, ""


def _systemd_unit_content(svc: ServiceSpec) -> str:
    """Generate a real systemd .service unit file from a ServiceSpec."""
    lines: list[str] = ["[Unit]", f"Description={svc.name}"]

    if svc.after:
        lines.append(f"After={' '.join(svc.after)}")
    if svc.requires:
        lines.append(f"Requires={' '.join(svc.requires)}")
    if svc.wants:
        lines.append(f"Wants={' '.join(svc.wants)}")

    lines.append("")
    lines.append("[Service]")
    lines.append("Type=simple")

    if svc.exec:
        lines.append(f"ExecStart={' '.join(svc.exec)}")
    if svc.user:
        lines.append(f"User={svc.user}")
    if svc.restart != "no":
        lines.append(f"Restart={svc.restart}")
        lines.append("RestartSec=5")

    # Security hardening for strict profile
    if svc.security_profile == "strict":
        lines.extend([
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "NoNewPrivileges=yes",
            "ProtectKernelModules=yes",
            "ProtectKernelTunables=yes",
            "ProtectControlGroups=yes",
            "RestrictSUIDSGID=yes",
            "MemoryDenyWriteExecute=yes",
        ])

    # Extra unit directives
    if svc.extra_unit and "Service" in svc.extra_unit:
        for key, value in sorted(svc.extra_unit["Service"].items()):
            lines.append(f"{key}={value}")

    lines.append("")
    lines.append("[Install]")
    lines.append("WantedBy=minimal.target")
    lines.append("")

    return "\n".join(lines)


def _useradd_command(user: UserSpec) -> str:
    """Generate a useradd command from a UserSpec."""
    parts = ["useradd"]
    if user.system:
        parts.append("--system")
    if user.home:
        parts.extend(["--home-dir", user.home, "--create-home"])
    if user.shell:
        parts.extend(["--shell", user.shell])
    if user.uid is not None:
        parts.extend(["--uid", str(user.uid)])
    if user.gid is not None:
        parts.extend(["--gid", str(user.gid)])
    if user.groups:
        parts.extend(["--groups", ",".join(user.groups)])
    parts.append(user.name)
    return " ".join(parts)


class DeterministicMkosiEmitter:
    """Emit real, buildable mkosi project trees per profile."""

    def emit(
        self,
        *,
        recipe: RecipeState,
        destination: Path,
        profile_names: tuple[str, ...],
        base: str,
        config: EmitConfig | None = None,
    ) -> MkosiEmission:
        if config is None:
            config = EmitConfig(base=base)

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
            profile_dir.mkdir(parents=True, exist_ok=True)

            # Generate mkosi.extra/ overlay tree (files, templates, service units)
            self._emit_extra_tree(profile_dir, profile)

            # Generate mkosi.skeleton/ tree
            self._emit_skeleton_tree(profile_dir, profile)

            # Generate phase scripts + synthetic postinst/finalize
            phase_scripts = self._emit_all_scripts(
                profile_name=profile_name,
                profile_dir=profile_dir,
                profile=profile,
                recipe=recipe,
            )

            # Generate mkosi.conf
            conf_content = self._render_conf(
                profile_name=profile_name,
                config=config,
                packages=sorted(profile.packages),
                build_packages=sorted(profile.build_packages),
                repositories=profile.repositories,
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

    def _emit_extra_tree(self, profile_dir: Path, profile: ProfileState) -> None:
        """Generate mkosi.extra/ with files, templates, and systemd units."""
        extra_dir = profile_dir / "mkosi.extra"
        extra_dir.mkdir(parents=True, exist_ok=True)

        # Files from img.file()
        for entry in profile.files:
            dest = extra_dir / entry.path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(entry.content, encoding="utf-8")

        # Rendered templates from img.template()
        for tmpl in profile.templates:
            dest = extra_dir / tmpl.path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(tmpl.rendered, encoding="utf-8")

        # Systemd service unit files from img.service()
        for svc in profile.services:
            unit_name = svc.name if svc.name.endswith(".service") else f"{svc.name}.service"
            # Skip non-service targets (like secrets-ready.target)
            if svc.name.endswith(".target"):
                continue
            dest = extra_dir / "usr" / "lib" / "systemd" / "system" / unit_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_systemd_unit_content(svc), encoding="utf-8")

    def _emit_skeleton_tree(self, profile_dir: Path, profile: ProfileState) -> None:
        """Generate mkosi.skeleton/ with pre-package-manager files."""
        # Skeleton files are just files that need to exist before apt runs.
        # The SDK doesn't have a separate skeleton list yet - skeleton() maps to file().
        # If we add skeleton support later, emit them here.
        skeleton_dir = profile_dir / "mkosi.skeleton"
        skeleton_dir.mkdir(parents=True, exist_ok=True)

    def _emit_all_scripts(
        self,
        *,
        profile_name: str,
        profile_dir: Path,
        profile: ProfileState,
        recipe: RecipeState,
    ) -> dict[Phase, Path]:
        """Emit phase scripts + synthetic postinst/finalize."""
        scripts_dir = profile_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        phase_scripts: dict[Phase, Path] = {}

        # Emit user-defined phase scripts
        for index, phase in enumerate(PHASE_ORDER, start=1):
            commands = profile.phases.get(phase, [])

            # For postinst: prepend user creation, service enablement, debloat masking
            if phase == "postinst":
                synthetic = self._synthetic_postinst_commands(profile)
                all_commands = synthetic + list(commands)
                if all_commands:
                    script_name = f"{index:02d}-{phase}.sh"
                    script_path = scripts_dir / script_name
                    script_path.write_text(
                        self._render_postinst_script(all_commands, profile),
                        encoding="utf-8",
                    )
                    script_path.chmod(0o755)
                    phase_scripts[phase] = script_path
                continue

            # For finalize: prepend debloat path removal
            if phase == "finalize":
                synthetic_lines = self._synthetic_finalize_lines(profile)
                if synthetic_lines or commands:
                    script_name = f"{index:02d}-{phase}.sh"
                    script_path = scripts_dir / script_name
                    script_path.write_text(
                        self._render_finalize_script(synthetic_lines, commands),
                        encoding="utf-8",
                    )
                    script_path.chmod(0o755)
                    phase_scripts[phase] = script_path
                continue

            if not commands:
                continue
            script_name = f"{index:02d}-{phase}.sh"
            script_path = scripts_dir / script_name
            script_path.write_text(self._render_script(commands), encoding="utf-8")
            script_path.chmod(0o755)
            phase_scripts[phase] = script_path

        return phase_scripts

    def _synthetic_postinst_commands(self, profile: ProfileState) -> list[CommandSpec]:
        """Create synthetic commands for user creation and service enablement."""
        commands: list[CommandSpec] = []

        # User creation
        for user in profile.users:
            cmd = _useradd_command(user)
            commands.append(CommandSpec(argv=("sh", "-c", cmd)))

        # Service enablement via symlink in /etc (writable in chroot)
        for svc in profile.services:
            if svc.enabled:
                unit_name = svc.name if "." in svc.name else f"{svc.name}.service"
                wants_dir = "/etc/systemd/system/multi-user.target.wants"
                commands.append(
                    CommandSpec(argv=(
                        "sh", "-c",
                        f"mkdir -p {wants_dir} && "
                        f"ln -sf /usr/lib/systemd/system/{unit_name} "
                        f"{wants_dir}/{unit_name}",
                    ))
                )

        return commands

    def _synthetic_finalize_lines(self, profile: ProfileState) -> list[str]:
        """Generate debloat shell lines for the finalize script."""
        config = profile.debloat
        if not config.enabled:
            return []

        lines: list[str] = []

        # Path removal
        paths = config.effective_paths_remove
        if paths:
            lines.append("# Debloat: remove unnecessary paths")
            for path in sorted(paths):
                lines.append(f"rm -rf \"$BUILDROOT{path}\"")

        # Systemd binary cleanup
        if config.systemd_minimize:
            bins_keep = set(config.systemd_bins_keep)
            lines.append("")
            lines.append("# Debloat: remove unwanted systemd binaries")
            lines.append("for bin in \"$BUILDROOT\"/usr/lib/systemd/system-generators/*; do")
            lines.append("    rm -f \"$bin\"")
            lines.append("done")
            lines.append("for bin in \"$BUILDROOT\"/usr/lib/systemd/*; do")
            lines.append("    [ -d \"$bin\" ] && continue")
            lines.append("    name=$(basename \"$bin\")")
            keep_check = " || ".join(
                f'[ "$name" = "{b}" ]' for b in sorted(bins_keep)
            )
            if keep_check:
                lines.append(f"    if ! ({keep_check}); then")
                lines.append("        rm -f \"$bin\"")
                lines.append("    fi")
            lines.append("done")

        return lines

    def _render_postinst_script(
        self, commands: list[CommandSpec], profile: ProfileState
    ) -> str:
        """Render postinst script with user creation, service enablement, and masking."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]

        # Render all commands (synthetic + user-defined)
        for command in commands:
            lines.append(self._render_command_line(command))

        # Systemd unit masking for debloat (symlink to /dev/null in /etc)
        config = profile.debloat
        if config.enabled and config.systemd_minimize:
            units_keep = set(config.effective_units_keep)
            lines.append("")
            lines.append("# Debloat: mask unwanted systemd units")
            lines.append("mkdir -p /etc/systemd/system")
            lines.append(
                "for unit in /usr/lib/systemd/system/*.target "
                "/usr/lib/systemd/system/*.service "
                "/usr/lib/systemd/system/*.socket; do"
            )
            lines.append("    [ -e \"$unit\" ] || continue")
            lines.append("    name=$(basename \"$unit\")")
            keep_list = " ".join(f'"{u}"' for u in sorted(units_keep))
            lines.append(f"    keep=({keep_list})")
            lines.append("    found=0")
            lines.append("    for k in \"${keep[@]}\"; do")
            lines.append("        [ \"$name\" = \"$k\" ] && found=1 && break")
            lines.append("    done")
            lines.append(
                "    [ \"$found\" = \"0\" ] && "
                "ln -sf /dev/null \"/etc/systemd/system/$name\" 2>/dev/null || true"
            )
            lines.append("done")

        lines.append("")
        return "\n".join(lines)

    def _render_finalize_script(
        self, synthetic_lines: list[str], commands: list[CommandSpec]
    ) -> str:
        """Render finalize script with debloat path removal + user commands."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        lines.extend(synthetic_lines)

        if commands:
            lines.append("")
            lines.append("# User-defined finalize commands")
            for command in commands:
                lines.append(self._render_command_line(command))

        lines.append("")
        return "\n".join(lines)

    def _render_conf(
        self,
        *,
        profile_name: str,
        config: EmitConfig,
        packages: list[str],
        build_packages: list[str],
        repositories: list[RepositorySpec],
        phase_scripts: dict[Phase, Path],
    ) -> str:
        distribution, release = _parse_base(config.base)
        lines: list[str] = []

        # [Distribution]
        lines.append("[Distribution]")
        lines.append(f"Distribution={distribution}")
        if release:
            lines.append(f"Release={release}")
        lines.append("")

        # [Output]
        lines.append("[Output]")
        lines.append(f"@Format={config.output_format}")
        lines.append(f"@ImageId={profile_name}")
        if config.reproducible:
            lines.append("CompressOutput=zstd")
            lines.append(f"Seed={config.seed}")
        lines.append("")

        # [Build] - reproducibility settings
        if config.reproducible:
            lines.append("[Build]")
            lines.append("SourceDateEpoch=0")
            lines.append("Environment=SOURCE_DATE_EPOCH=0")
            lines.append("")

        # [Content]
        lines.append("[Content]")
        if packages:
            pkg_lines = "\n".join(f"    {p}" for p in packages)
            lines.append(f"Packages=\n{pkg_lines}")
        if build_packages:
            bpkg_lines = "\n".join(f"    {p}" for p in build_packages)
            lines.append(f"BuildPackages=\n{bpkg_lines}")

        # Kernel configuration
        if config.kernel:
            if config.output_format == "uki":
                lines.append("Bootable=yes")
            if config.kernel.cmdline:
                lines.append(f"KernelCommandLine={config.kernel.cmdline}")
            if config.kernel.version:
                lines.append(f"# KernelVersion={config.kernel.version}")
            if config.kernel.tdx:
                lines.append("# TDX-enabled kernel required")

        # Extra trees and skeleton
        lines.append("ExtraTrees=mkosi.extra")
        lines.append("SkeletonTrees=mkosi.skeleton")

        # Repositories
        if repositories:
            lines.append("")
            lines.append("# Additional repositories")
            for repo in repositories:
                lines.append(f"# repo: {repo.name} = {repo.url}")
                if repo.suite:
                    lines.append(f"# suite: {repo.suite}")

        # Script references (part of [Content] section)
        if phase_scripts:
            lines.append("")
            for phase in PHASE_ORDER:
                script_path = phase_scripts.get(phase)
                if script_path is None:
                    continue
                lines.append(f"{PHASE_TO_MKOSI_KEY[phase]}=scripts/{script_path.name}")

        return "\n".join(lines) + "\n"

    def _render_script(self, commands: list[CommandSpec]) -> str:
        """Render a standard phase script."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for command in commands:
            lines.append(self._render_command_line(command))
        lines.append("")
        return "\n".join(lines)

    def _render_command_line(self, command: CommandSpec) -> str:
        """Render a single CommandSpec to a shell line."""
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(command.env.items())
        )
        rendered_argv = " ".join(shlex.quote(part) for part in command.argv)
        rendered = rendered_argv if not env_prefix else f"{env_prefix} {rendered_argv}"
        if command.cwd is not None:
            rendered = f"(cd {shlex.quote(command.cwd)} && {rendered})"
        if command.shell:
            rendered = f"bash -lc {shlex.quote(rendered)}"
        return rendered


def emit_mkosi_tree(
    *,
    recipe: RecipeState,
    destination: Path,
    profile_names: tuple[str, ...],
    base: str,
    config: EmitConfig | None = None,
) -> MkosiEmission:
    emitter = DeterministicMkosiEmitter()
    return emitter.emit(
        recipe=recipe,
        destination=destination,
        profile_names=profile_names,
        base=base,
        config=config,
    )
