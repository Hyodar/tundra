"""Real mkosi project tree emission pipeline.

Generates complete, buildable mkosi project directories per profile, including:
- mkosi.conf with proper Distribution, Output, Build, Content, Scripts sections
- mkosi.extra/ overlay tree (files, templates, systemd unit files)
- mkosi.skeleton/ pre-package-manager tree
- scripts/ for postinst (user creation, service enablement, debloat masking),
  finalize (debloat path removal), and user-defined phase commands
"""

from __future__ import annotations

import hashlib
import shlex
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

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

# Map Python arch names to mkosi architecture values
ARCH_TO_MKOSI: dict[str, str] = {
    "x86_64": "x86-64",
    "aarch64": "arm64",
}

# Custom init script matching nethermind-tdx base/mkosi.skeleton/init
DEFAULT_TDX_INIT_SCRIPT = textwrap.dedent("""\
    #!/bin/sh

    # Mount essential filesystems
    mkdir -p /dev /proc /sys /run
    mount -t proc none /proc
    mount -t sysfs none /sys
    mount -t devtmpfs none /dev
    mount -t tmpfs none /run
    mount -t configfs none /sys/kernel/config

    # Workaround to make pivot_root work
    # https://aconz2.github.io/2024/07/29/container-from-initramfs.html
    exec unshare --mount sh -c '
        mkdir /@
        mount --rbind / /@
        cd /@ && mount --move . /
        exec chroot . /lib/systemd/systemd systemd.unit=minimal.target'
""")

# Minimal systemd target matching nethermind-tdx base/mkosi.skeleton
MINIMAL_TARGET_UNIT = (
    "[Unit]\n"
    "Description=Minimal System\n"
    "Requires=basic.target\n"
    "Conflicts=rescue.service rescue.target\n"
    "After=basic.target rescue.service rescue.target\n"
    "AllowIsolate=yes\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target"
)

# GCP postoutput: creates ESP image, GPT disk, wraps in deterministic tar.gz
GCP_POSTOUTPUT_SCRIPT = textwrap.dedent("""\
    #!/bin/bash
    set -euxo pipefail

    EFI="${OUTPUTDIR}/${IMAGE_ID}_${IMAGE_VERSION}.efi"
    TAR="${OUTPUTDIR}/${IMAGE_ID}_${IMAGE_VERSION}.tar.gz"
    TMP="${OUTPUTDIR}/gcp-tmp"

    [ ! -f "$EFI" ] && echo "Error: $EFI not found" && exit 1

    mkdir -p "$TMP"

    # Fixed GUIDs and IDs
    DISK_GUID="12345678-1234-5678-1234-567812345678"
    PARTITION_GUID="87654321-4321-8765-4321-876543218765"
    FAT_SERIAL="12345678"

    # Create 500MB ESP
    dd if=/dev/zero of="$TMP/esp.img" bs=1M count=500

    # Format with fixed volume serial number and label
    mformat -i "$TMP/esp.img" -F -v "ESP" -N "$FAT_SERIAL" ::

    # Create directory structure
    mmd -i "$TMP/esp.img" ::EFI ::EFI/BOOT

    # Copy files with deterministic timestamps
    # -D o sets file times to 1980-01-01 (DOS epoch)
    mcopy -D o -i "$TMP/esp.img" "$EFI" ::EFI/BOOT/BOOTX64.EFI

    # Create 1GB disk with GPT
    dd if=/dev/zero of="$TMP/disk.raw" bs=1M count=1024
    sgdisk --disk-guid="$DISK_GUID" "$TMP/disk.raw"

    # Create ESP partition
    # -n creates partition (number:start:end)
    # -t sets type (1:ef00 for ESP)
    # -u sets partition GUID
    # -c sets partition name
    sgdisk -n 1:2048:1026047 \\
            -t 1:ef00 \\
            -u 1:"$PARTITION_GUID" \\
            -c 1:"ESP" \\
            -A 1:set:0 \\
            "$TMP/disk.raw"

    # Write ESP image to partition area
    dd if="$TMP/esp.img" of="$TMP/disk.raw" bs=512 seek=2048 conv=notrunc
    touch -d "2024-01-01 00:00:00 UTC" "$TMP/disk.raw" 2>/dev/null || true

    # Create GCP tar.gz
    tar --format=oldgnu -Sczf "$TAR" -C "$TMP" disk.raw

    rm -rf "$TMP"
""")

# Azure postoutput: converts EFI to VHD with ESP partition
AZURE_POSTOUTPUT_SCRIPT = textwrap.dedent("""\
    #!/bin/bash
    set -euxo pipefail

    EFI_FILE="${OUTPUTDIR}/${IMAGE_ID}_${IMAGE_VERSION}.efi"
    VHD_FILE="${OUTPUTDIR}/${IMAGE_ID}_${IMAGE_VERSION}.vhd"
    WORK_DIR="${OUTPUTDIR}/azure-tmp"

    if [ ! -f "$EFI_FILE" ]; then
        echo "Error: EFI file not found at $EFI_FILE"
        exit 1
    fi

    echo "Converting $EFI_FILE to VHD format..."

    # Create working directory
    mkdir -p "$WORK_DIR"

    # Create ESP filesystem image (500MB should be plenty)
    ESP_SIZE_MB=500
    ESP_IMAGE="$WORK_DIR/esp.img"

    # Create empty ESP image and format it as FAT32
    dd if=/dev/zero of="$ESP_IMAGE" bs=1M count=$ESP_SIZE_MB
    mformat -i "$ESP_IMAGE" -F -v "ESP" ::

    # Create EFI directory structure and copy the UKI file using mtools
    mmd -i "$ESP_IMAGE" ::EFI
    mmd -i "$ESP_IMAGE" ::EFI/BOOT
    mcopy -i "$ESP_IMAGE" "$EFI_FILE" ::EFI/BOOT/BOOTX64.EFI

    # Create the final disk image with GPT
    DISK_SIZE_MB=$((ESP_SIZE_MB + 2))  # ESP + 1MB for GPT headers
    DISK_IMAGE="$WORK_DIR/azure_image.raw"

    # Create empty disk
    dd if=/dev/zero of="$DISK_IMAGE" bs=1M count=$DISK_SIZE_MB

    # Create GPT partition table and ESP partition
    # Use sector size of 512 bytes, so 1MB = 2048 sectors
    parted "$DISK_IMAGE" --script -- \\
      mklabel gpt \\
      mkpart ESP fat32 2048s $(($ESP_SIZE_MB * 2048 + 2047))s \\
      set 1 boot on

    # Copy the ESP filesystem into the partition
    # Skip first 1MB (2048 sectors) to account for GPT header
    dd if="$ESP_IMAGE" of="$DISK_IMAGE" bs=512 seek=2048 conv=notrunc

    # Convert to VHD
    truncate -s %1MiB "$DISK_IMAGE"
    qemu-img convert -O vpc -o subformat=fixed,force_size "$DISK_IMAGE" "$VHD_FILE"

    # Clean up
    rm -rf "$WORK_DIR"

    echo "Successfully created VHD: $VHD_FILE"
""")

# mkosi.version: git-based version script
MKOSI_VERSION_SCRIPT = textwrap.dedent("""\
    #!/bin/bash
    set -euo pipefail

    # Add current directory to git safe directories if not already present
    if ! git config --global --get-all safe.directory | grep -Fxq "$PWD"; then
        git config --global --add safe.directory "$PWD"
    fi

    commit_date=$(TZ=UTC0 git show -s --date=format:'%Y-%m-%d' --format='%ad')
    commit_hash=$(git rev-parse --short=6 HEAD)
    dirty_suffix=""
    if [ -n "$(git status --porcelain)" ]; then
        dirty_suffix="-dirty"
    fi

    # example value: 2025-06-26.a1b2c3d-dirty
    echo "${commit_date}.${commit_hash}${dirty_suffix}"
""")


@dataclass(frozen=True, slots=True)
class EmitConfig:
    """Configuration passed from Image to the emitter."""

    base: str
    arch: Arch = "x86_64"
    reproducible: bool = True
    kernel: Kernel | None = None
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    output_format: str = "uki"
    seed: str = DEFAULT_SEED
    compress_output: str | None = None
    output_directory: str | None = None
    with_network: bool = True
    clean_package_metadata: bool = True
    manifest_format: str = "json"
    sandbox_trees: tuple[str, ...] = ()
    package_cache_directory: str | None = None
    init_script: str | None = None
    generate_version_script: bool = False
    generate_cloud_postoutput: bool = True
    environment: dict[str, str] | None = None
    environment_passthrough: tuple[str, ...] | None = None
    emit_mode: Literal["per_directory", "native_profiles"] = "per_directory"


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
        lines.extend(
            [
                "ProtectSystem=strict",
                "ProtectHome=yes",
                "PrivateTmp=yes",
                "NoNewPrivileges=yes",
                "ProtectKernelModules=yes",
                "ProtectKernelTunables=yes",
                "ProtectControlGroups=yes",
                "RestrictSUIDSGID=yes",
                "MemoryDenyWriteExecute=yes",
            ]
        )

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
    """Generate a useradd shell command from a UserSpec."""
    parts: list[str] = ["mkosi-chroot useradd"]
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


def _render_kernel_build_script(kernel: Kernel) -> str:
    """Render a build script that clones, configures, and compiles the Linux kernel."""
    version = kernel.version or "unknown"
    config_hash = hashlib.sha256(str(kernel.config_file).encode()).hexdigest()[:12]
    cache_key = f"kernel-{version}-{config_hash}"
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        KERNEL_CACHE="${{BUILDDIR}}/{cache_key}"
        KERNEL_VERSION="{version}"

        if [ -d "$KERNEL_CACHE/done" ]; then
            echo "Using cached kernel build: {cache_key}"
        else
            rm -rf "$KERNEL_CACHE"
            mkdir -p "$KERNEL_CACHE"

            git clone --depth 1 --branch "v${{KERNEL_VERSION}}" \\
                {kernel.source_repo} "$KERNEL_CACHE/src"

            cp kernel/kernel.config "$KERNEL_CACHE/src/.config"
            cd "$KERNEL_CACHE/src"

            # Reproducibility environment
            export KBUILD_BUILD_TIMESTAMP="1970-01-01"
            export KBUILD_BUILD_USER="tdxvm"
            export KBUILD_BUILD_HOST="tdxvm"

            make olddefconfig
            make -j"$(nproc)" bzImage ARCH=x86_64

            mkdir -p "$KERNEL_CACHE/done"
        fi

        # Install kernel to destination
        INSTALL_DIR="${{DESTDIR}}/usr/lib/modules/${{KERNEL_VERSION}}"
        mkdir -p "$INSTALL_DIR"
        cp "$KERNEL_CACHE/src/arch/x86/boot/bzImage" "$INSTALL_DIR/vmlinuz"

        # Export for downstream phases
        export KERNEL_IMAGE="$INSTALL_DIR/vmlinuz"
        export KERNEL_VERSION="{version}"
    """)


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

        if config.emit_mode == "native_profiles":
            return self._emit_native_profiles(
                recipe=recipe,
                destination=destination,
                profile_names=profile_names,
                config=config,
            )

        return self._emit_per_directory(
            recipe=recipe,
            destination=destination,
            profile_names=profile_names,
            config=config,
        )

    def _emit_per_directory(
        self,
        *,
        recipe: RecipeState,
        destination: Path,
        profile_names: tuple[str, ...],
        config: EmitConfig,
    ) -> MkosiEmission:
        destination.mkdir(parents=True, exist_ok=True)
        profile_paths: dict[str, Path] = {}
        script_paths: dict[str, dict[Phase, Path]] = {}

        # Emit mkosi.version at emission root when enabled
        if config.generate_version_script:
            version_path = destination / "mkosi.version"
            version_path.write_text(MKOSI_VERSION_SCRIPT, encoding="utf-8")
            version_path.chmod(0o755)

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
            self._emit_skeleton_tree(profile_dir, profile, config)

            # Copy kernel config file if kernel has one
            self._emit_kernel_config(profile_dir, config)

            # Generate phase scripts + synthetic postinst/finalize
            phase_scripts = self._emit_all_scripts(
                profile_name=profile_name,
                profile_dir=profile_dir,
                profile=profile,
                recipe=recipe,
                config=config,
            )

            # Generate mkosi.conf
            conf_content = self._render_conf(
                profile_name=profile_name,
                config=config,
                packages=sorted(profile.packages),
                build_packages=sorted(profile.build_packages),
                build_sources=profile.build_sources or None,
                repositories=profile.repositories,
                phase_scripts=phase_scripts,
            )

            conf_path = profile_dir / "mkosi.conf"
            conf_path.write_text(conf_content, encoding="utf-8")

            # Emit cloud postoutput scripts based on output_targets
            if config.generate_cloud_postoutput:
                self._emit_cloud_postoutput(profile_dir, profile)

            profile_paths[profile_name] = conf_path
            script_paths[profile_name] = phase_scripts

        return MkosiEmission(
            root=destination,
            profile_paths=profile_paths,
            script_paths=script_paths,
        )

    def _emit_native_profiles(
        self,
        *,
        recipe: RecipeState,
        destination: Path,
        profile_names: tuple[str, ...],
        config: EmitConfig,
    ) -> MkosiEmission:
        """Emit a single root mkosi.conf with mkosi.profiles/<name>/ overrides."""
        destination.mkdir(parents=True, exist_ok=True)
        profile_paths: dict[str, Path] = {}
        script_paths: dict[str, dict[Phase, Path]] = {}

        # Emit mkosi.version at emission root when enabled
        if config.generate_version_script:
            version_path = destination / "mkosi.version"
            version_path.write_text(MKOSI_VERSION_SCRIPT, encoding="utf-8")
            version_path.chmod(0o755)

        # Root mkosi.conf with shared configuration (use first profile as base)
        first_profile_name = sorted(profile_names)[0]
        first_profile = recipe.profiles.get(first_profile_name)
        if first_profile is None:
            raise ValidationError(
                "Profile does not exist for mkosi emission.",
                hint="Create the profile before calling emit_mkosi().",
                context={"profile": first_profile_name, "operation": "emit_mkosi"},
            )

        # Shared skeleton and extra at root level
        self._emit_skeleton_tree(destination, first_profile, config)
        self._emit_extra_tree(destination, first_profile)

        # Root mkosi.conf with shared settings (no profile-specific packages)
        root_conf_content = self._render_conf(
            profile_name=first_profile_name,
            config=config,
            packages=[],
            build_packages=[],
            repositories=[],
            phase_scripts={},
        )
        root_conf_path = destination / "mkosi.conf"
        root_conf_path.write_text(root_conf_content, encoding="utf-8")

        # Per-profile overrides under mkosi.profiles/<name>/
        profiles_dir = destination / "mkosi.profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        for profile_name in sorted(profile_names):
            profile = recipe.profiles.get(profile_name)
            if profile is None:
                raise ValidationError(
                    "Profile does not exist for mkosi emission.",
                    hint="Create the profile before calling emit_mkosi().",
                    context={"profile": profile_name, "operation": "emit_mkosi"},
                )
            self._validate_profile_phases(profile_name=profile_name, recipe=recipe)

            profile_dir = profiles_dir / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)

            # Profile-specific extra tree
            self._emit_extra_tree(profile_dir, profile)

            # Copy kernel config file if kernel has one
            self._emit_kernel_config(profile_dir, config)

            # Generate phase scripts
            phase_scripts = self._emit_all_scripts(
                profile_name=profile_name,
                profile_dir=profile_dir,
                profile=profile,
                recipe=recipe,
                config=config,
            )

            # Profile-specific mkosi.conf override
            conf_content = self._render_conf(
                profile_name=profile_name,
                config=config,
                packages=sorted(profile.packages),
                build_packages=sorted(profile.build_packages),
                build_sources=profile.build_sources or None,
                repositories=profile.repositories,
                phase_scripts=phase_scripts,
            )
            conf_path = profile_dir / "mkosi.conf"
            conf_path.write_text(conf_content, encoding="utf-8")

            # Emit cloud postoutput scripts
            if config.generate_cloud_postoutput:
                self._emit_cloud_postoutput(profile_dir, profile)

            profile_paths[profile_name] = conf_path
            script_paths[profile_name] = phase_scripts

        return MkosiEmission(
            root=destination,
            profile_paths=profile_paths,
            script_paths=script_paths,
        )

    def _emit_cloud_postoutput(self, profile_dir: Path, profile: ProfileState) -> None:
        """Emit cloud-specific postoutput scripts based on output_targets."""
        targets = profile.output_targets
        if "gcp" in targets:
            gcp_script = profile_dir / "scripts" / "gcp-postoutput.sh"
            gcp_script.parent.mkdir(parents=True, exist_ok=True)
            gcp_script.write_text(GCP_POSTOUTPUT_SCRIPT, encoding="utf-8")
            gcp_script.chmod(0o755)
        if "azure" in targets:
            azure_script = profile_dir / "scripts" / "azure-postoutput.sh"
            azure_script.parent.mkdir(parents=True, exist_ok=True)
            azure_script.write_text(AZURE_POSTOUTPUT_SCRIPT, encoding="utf-8")
            azure_script.chmod(0o755)

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
            # Skip enablement-only registrations (no exec = no unit file to generate)
            if not svc.exec:
                continue
            unit_name = svc.name if svc.name.endswith(".service") else f"{svc.name}.service"
            # Skip non-service targets (like secrets-ready.target)
            if svc.name.endswith(".target"):
                continue
            dest = extra_dir / "usr" / "lib" / "systemd" / "system" / unit_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_systemd_unit_content(svc), encoding="utf-8")

    def _emit_skeleton_tree(
        self, profile_dir: Path, profile: ProfileState, config: EmitConfig
    ) -> None:
        """Generate mkosi.skeleton/ with pre-package-manager files."""
        skeleton_dir = profile_dir / "mkosi.skeleton"
        skeleton_dir.mkdir(parents=True, exist_ok=True)

        # Write custom init script if configured
        if config.init_script:
            init_path = skeleton_dir / "init"
            init_path.write_text(config.init_script, encoding="utf-8")
            init_path.chmod(0o755)

        # Write skeleton files from img.skeleton()
        for entry in profile.skeleton_files:
            dest = skeleton_dir / entry.path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(entry.content, encoding="utf-8")

        # Auto-emit minimal.target when systemd debloat sets it as default
        if profile.debloat.enabled and profile.debloat.systemd_minimize:
            target_path = skeleton_dir / "etc" / "systemd" / "system" / "minimal.target"
            if not target_path.exists():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(MINIMAL_TARGET_UNIT, encoding="utf-8")

    def _emit_kernel_config(self, profile_dir: Path, config: EmitConfig) -> None:
        """Copy kernel config file into the output tree if present."""
        if config.kernel and config.kernel.config_file:
            kernel_dir = profile_dir / "kernel"
            kernel_dir.mkdir(parents=True, exist_ok=True)
            config_src = Path(config.kernel.config_file)
            config_dest = kernel_dir / "kernel.config"
            if config_src.exists():
                config_dest.write_text(config_src.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                # Write a placeholder referencing the expected config file
                config_dest.write_text(
                    f"# Kernel config: {config.kernel.config_file}\n"
                    f"# Place the actual config file at this path before building.\n",
                    encoding="utf-8",
                )

    def _emit_all_scripts(
        self,
        *,
        profile_name: str,
        profile_dir: Path,
        profile: ProfileState,
        recipe: RecipeState,
        config: EmitConfig | None = None,
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
                needs_debloat = profile.debloat.enabled and profile.debloat.systemd_minimize
                if all_commands or needs_debloat:
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

            # For build: prepend kernel build script if kernel has config_file
            if phase == "build" and config and config.kernel and config.kernel.config_file:
                kernel_script = _render_kernel_build_script(config.kernel)
                if commands:
                    # Combine kernel build + user-defined build commands
                    user_script = self._render_script(commands)
                    # Remove the shebang from user script to avoid duplicate
                    user_lines = user_script.split("\n")
                    user_body = "\n".join(
                        line
                        for line in user_lines
                        if not line.startswith("#!") and line != "set -euo pipefail"
                    ).strip()
                    combined = kernel_script.rstrip() + "\n\n" + user_body + "\n"
                else:
                    combined = kernel_script
                script_name = f"{index:02d}-{phase}.sh"
                script_path = scripts_dir / script_name
                script_path.write_text(combined, encoding="utf-8")
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

        # User creation via mkosi-chroot
        for user in profile.users:
            commands.append(CommandSpec(argv=(_useradd_command(user),)))

        # Service enablement via mkosi-chroot systemctl enable + minimal.target.wants
        enabled_units: list[str] = []
        for svc in profile.services:
            if svc.enabled:
                unit_name = svc.name if "." in svc.name else f"{svc.name}.service"
                commands.append(CommandSpec(argv=(f"mkosi-chroot systemctl enable {unit_name}",)))
                enabled_units.append(unit_name)

        if enabled_units:
            commands.append(
                CommandSpec(argv=('mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"',))
            )
            for unit_name in enabled_units:
                commands.append(
                    CommandSpec(
                        argv=(
                            f'ln -sf "/etc/systemd/system/{unit_name}" '
                            f'"$BUILDROOT/etc/systemd/system/minimal.target.wants/"',
                        )
                    )
                )

        return commands

    def _synthetic_finalize_lines(self, profile: ProfileState) -> list[str]:
        """Generate debloat shell lines for the finalize script."""
        config = profile.debloat
        if not config.enabled:
            return []

        lines: list[str] = []

        # Clean files in var directories (preserve directory structure)
        if config.clean_var_dirs:
            lines.append("# Debloat: clean files in var directories")
            for var_dir in sorted(config.clean_var_dirs):
                lines.append(f'find "$BUILDROOT{var_dir}" -type f -delete')

        # Path removal (remains in finalize — runs on host with $BUILDROOT)
        paths = config.effective_paths_remove
        if paths:
            lines.append("")
            lines.append("# Debloat: remove unnecessary paths")
            for path in sorted(paths):
                lines.append(f'rm -rf "$BUILDROOT{path}"')

        # Profile-conditional path removal: paths removed only when profile is NOT active
        conditional = config.profile_conditional_paths
        if conditional:
            lines.append("")
            lines.append("# Debloat: profile-conditional path removal")
            for profile_name in sorted(conditional):
                for path in conditional[profile_name]:
                    lines.append(f'if [[ ! "${{PROFILES:-}}" == *"{profile_name}"* ]]; then')
                    lines.append(f'    rm -rf "$BUILDROOT{path}"')
                    lines.append("fi")

        return lines

    def _render_postinst_script(self, commands: list[CommandSpec], profile: ProfileState) -> str:
        """Render postinst script with user creation, service enablement, and debloat."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]

        # Render all commands (synthetic + user-defined)
        for command in commands:
            lines.append(self._render_command_line(command))

        # Systemd debloat via dpkg-query (matching nethermind-tdx debloat-systemd.sh)
        config = profile.debloat
        if config.enabled and config.systemd_minimize:
            bins_keep = sorted(config.systemd_bins_keep)
            units_keep = sorted(config.effective_units_keep)

            # Binary cleanup via mkosi-chroot dpkg-query
            lines.append("")
            lines.append("# Debloat: remove unwanted systemd binaries")
            bins_keep_list = " ".join(f'"{b}"' for b in bins_keep)
            lines.append(f"systemd_bin_whitelist=({bins_keep_list})")
            lines.append(
                "mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/' | "
                "while read -r bin_path; do"
            )
            lines.append('    bin_name=$(basename "$bin_path")')
            lines.append(
                "    if ! printf '%s\\n' \"${systemd_bin_whitelist[@]}\" | "
                'grep -qx "$bin_name"; then'
            )
            lines.append('        rm -f "$BUILDROOT$bin_path"')
            lines.append("    fi")
            lines.append("done")

            # Unit masking via mkosi-chroot dpkg-query
            lines.append("")
            lines.append("# Debloat: mask unwanted systemd units")
            keep_list = " ".join(f'"{u}"' for u in units_keep)
            lines.append(f"systemd_svc_whitelist=({keep_list})")
            lines.append('SYSTEMD_DIR="$BUILDROOT/etc/systemd/system"')
            lines.append('mkdir -p "$SYSTEMD_DIR"')
            lines.append(
                "mkosi-chroot dpkg-query -L systemd | "
                "grep -E '\\.service$|\\.socket$|\\.timer$|\\.target$|\\.mount$' | "
                "sed 's|.*/||' | while read -r unit; do"
            )
            lines.append(
                '    if ! printf \'%s\\n\' "${systemd_svc_whitelist[@]}" | grep -qx "$unit"; then'
            )
            lines.append('        ln -sf /dev/null "$SYSTEMD_DIR/$unit"')
            lines.append("    fi")
            lines.append("done")

            # Set default target
            lines.append("")
            lines.append("# Set default systemd target")
            lines.append('ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"')

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
        build_sources: list[tuple[str, str]] | None = None,
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
        mkosi_arch = ARCH_TO_MKOSI.get(config.arch)
        if mkosi_arch:
            lines.append(f"Architecture={mkosi_arch}")
        if config.mirror:
            lines.append(f"Mirror={config.mirror}")
        lines.append("")

        # [Output]
        lines.append("[Output]")
        lines.append(f"Format={config.output_format}")
        lines.append(f"ImageId={profile_name}")
        lines.append(f"ManifestFormat={config.manifest_format}")
        if config.compress_output:
            lines.append(f"CompressOutput={config.compress_output}")
        if config.output_directory:
            lines.append(f"OutputDirectory={config.output_directory}")
        if config.reproducible:
            lines.append(f"Seed={config.seed}")
        lines.append("")

        # [Build] - reproducibility + network + sandbox settings
        build_lines: list[str] = []
        env_vars: dict[str, str] = dict(config.environment) if config.environment else {}
        if config.reproducible:
            env_vars.setdefault("SOURCE_DATE_EPOCH", "0")
        for key in sorted(env_vars):
            build_lines.append(f"Environment={key}={env_vars[key]}")
        # Collect environment passthrough keys
        passthrough_keys = list(config.environment_passthrough or ())
        # Auto-add kernel env vars when kernel has config_file
        if config.kernel and config.kernel.config_file:
            for kvar in ("KERNEL_IMAGE", "KERNEL_VERSION"):
                if kvar not in passthrough_keys:
                    passthrough_keys.append(kvar)
        for key in passthrough_keys:
            build_lines.append(f"Environment={key}")
        if config.tools_tree_mirror:
            build_lines.append(f"ToolsTreeMirror={config.tools_tree_mirror}")
        build_lines.append(f"WithNetwork={'true' if config.with_network else 'false'}")
        if config.sandbox_trees:
            for tree in config.sandbox_trees:
                build_lines.append(f"SandboxTrees={tree}")
        if config.package_cache_directory:
            build_lines.append(f"PackageCacheDirectory={config.package_cache_directory}")
        if build_lines:
            lines.append("[Build]")
            lines.extend(build_lines)
            lines.append("")

        # [Content]
        lines.append("[Content]")
        if config.reproducible:
            lines.append("SourceDateEpoch=0")
        lines.append(f"CleanPackageMetadata={'true' if config.clean_package_metadata else 'false'}")
        if packages:
            pkg_lines = "\n".join(f"    {p}" for p in packages)
            lines.append(f"Packages=\n{pkg_lines}")
        if build_packages:
            bpkg_lines = "\n".join(f"    {p}" for p in build_packages)
            lines.append(f"BuildPackages=\n{bpkg_lines}")
        if build_sources:
            for host_path, target in build_sources:
                entry = f"{host_path}:{target}" if target else host_path
                lines.append(f"BuildSources={entry}")

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
        rendered = command.argv[0]
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(command.env.items())
        )
        if env_prefix:
            rendered = f"{env_prefix} {rendered}"
        if command.cwd is not None:
            rendered = f"(cd {shlex.quote(command.cwd)} && {rendered})"
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
