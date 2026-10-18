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
import re
import shlex
import shutil
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol, get_args
from urllib.parse import urlparse

from tundravm._source import FETCH_MARKER, SOURCES_MOUNT, HttpSource
from tundravm._units import v1
from tundravm._units.render import service_unit
from tundravm.errors import ValidationError
from tundravm.kernel_modules import (
    MODULE_SETS_VERSION,
    NO_MODULES,
    ModuleSelection,
    debloat_paths_remove,
    module_selection,
)
from tundravm.models import (
    Arch,
    CommandSpec,
    DebloatConfig,
    FileEntry,
    GroupSpec,
    Kernel,
    Phase,
    ProfileState,
    RecipeState,
    RepositorySpec,
    ServiceSpec,
    UnitAction,
    UserSpec,
    debloat_units_keep,
    declared_paths,
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

DIR_MODE = 0o755
"""Every directory the emitter creates, whatever the umask."""
FILE_MODE = 0o644
"""Every file written without a declared mode."""
EXEC_MODE = 0o755
"""Every script the emitter generates."""

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
MINIMAL_TARGET_UNIT = v1.MINIMAL_TARGET

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

# Finalize, current dialect: account databases in a fixed order, without the "-" backups.
ACCOUNTS_FINALIZE = textwrap.dedent("""\
    # Deterministic accounts (Debian #963788): passwd and group by numeric id, then name;
    # shadow and gshadow in that order, as pwck --sort and grpck --sort leave them; subuid
    # and subgid by first id. Then drop the "-" backups useradd and groupadd leave behind.
    tundravm_sort_by_field() {
        local file="$BUILDROOT/etc/$1"
        [ -f "$file" ] || return 0
        LC_ALL=C sort -t: -k"$2,$2n" -k1,1 -o "$file" "$file"
    }
    tundravm_sort_like() {
        local order="$BUILDROOT/etc/$1" file="$BUILDROOT/etc/$2"
        [ -f "$order" ] && [ -f "$file" ] || return 0
        LC_ALL=C awk -F: 'NR == FNR { rank[$1] = FNR; next }
            { print (($1 in rank) ? rank[$1] : 1000000) ":" $0 }' "$order" "$file" |
            LC_ALL=C sort -t: -k1,1n -k2,2 | cut -d: -f2- >"$file.tundravm"
        cat "$file.tundravm" >"$file"
        rm -f "$file.tundravm"
    }
    tundravm_sort_by_field passwd 3
    tundravm_sort_by_field group 3
    tundravm_sort_like passwd shadow
    tundravm_sort_like group gshadow
    tundravm_sort_by_field subuid 2
    tundravm_sort_by_field subgid 2
    find "$BUILDROOT/etc" -maxdepth 1 -type f -name '*-' -delete
""")
"""The finalize step every reproducible current-dialect profile ends with (its own block).

``cat >`` rewrites in place, so ``shadow`` keeps its mode and group."""


# Current dialect: the ESP both cloud scripts build, byte-reproducible from the UKI alone.
_DETERMINISTIC_ESP = """\
# Deterministic ESP (current dialect): sized to the UKI plus a fixed 32 MiB (64 MiB at
# least, so FAT32 fits); a volume serial from the UKI's sha256; one entry per directory,
# copied with mcopy -m from a tree dated SOURCE_DATE_EPOCH, under TZ=UTC (FAT keeps
# local times); mkfs.vfat --invariant fixes the rest of the boot sector.
export TZ=UTC SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
UKI_SHA=$(sha256sum "$EFI" | cut -c1-64)
ESP_MB=$(( ($(stat -c %s "$EFI") + 33 * 1024 * 1024 - 1) / (1024 * 1024) ))
[ "$ESP_MB" -ge 64 ] || ESP_MB=64
rm -rf "$TMP/esp" "$TMP/esp.img"
mkdir -p "$TMP/esp/EFI/BOOT"
cp "$EFI" "$TMP/esp/EFI/BOOT/BOOTX64.EFI"
find "$TMP/esp" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
truncate -s "${ESP_MB}M" "$TMP/esp.img"
mkfs.vfat -F 32 -n ESP -i "${UKI_SHA:0:8}" --invariant "$TMP/esp.img"
mcopy -b -s -p -Q -m -i "$TMP/esp.img" "$TMP/esp/EFI" ::
"""

# The disk around it: GPT with fixed GUIDs, the ESP at 1 MiB.
_DETERMINISTIC_DISK = """\
DISK_GUID="12345678-1234-5678-1234-567812345678"
PARTITION_GUID="87654321-4321-8765-4321-876543218765"
truncate -s "${DISK_MB}M" "$DISK"
sgdisk --disk-guid="$DISK_GUID" \\
    -n 1:2048:$(( 2048 + ESP_MB * 2048 - 1 )) -t 1:ef00 -u 1:"$PARTITION_GUID" -c 1:ESP \\
    "$DISK"
dd if="$TMP/esp.img" of="$DISK" bs=1M seek=1 conv=notrunc,sparse status=none
"""

AZURE_POSTOUTPUT_SCRIPT_CURRENT = (
    """\
#!/bin/bash
set -euxo pipefail

EFI="${OUTPUTDIR}/${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}.efi"
VHD="${OUTPUTDIR}/${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}.vhd"
TMP="${OUTPUTDIR}/azure-tmp"

[ -f "$EFI" ] || { echo "Error: EFI file not found at $EFI"; exit 1; }
mkdir -p "$TMP"

"""
    + _DETERMINISTIC_ESP
    + """
# GPT disk: the ESP plus 1 MiB in front and 1 MiB for the backup GPT (Azure wants whole MiB).
DISK="$TMP/azure_image.raw"
DISK_MB=$(( ESP_MB + 2 ))
"""
    + _DETERMINISTIC_DISK
    + """
qemu-img convert -O vpc -o subformat=fixed,force_size "$DISK" "$VHD"

# qemu-img stamps the VHD footer with the wall clock and a random unique id: pin the
# timestamp to SOURCE_DATE_EPOCH, derive the id from the disk, recompute the checksum.
python3 - "$VHD" "$SOURCE_DATE_EPOCH" "$(sha256sum "$DISK" | cut -c1-32)" <<'PY'
import struct, sys
path, epoch, unique = sys.argv[1], int(sys.argv[2]), bytes.fromhex(sys.argv[3])
with open(path, "r+b") as vhd:
    vhd.seek(-512, 2)
    footer = bytearray(vhd.read(512))
    assert footer[:8] == b"conectix", "not a fixed VHD footer"
    footer[24:28] = struct.pack(">I", max(epoch - 946684800, 0))  # seconds since 2000
    footer[68:84] = unique
    footer[64:68] = bytes(4)
    footer[64:68] = struct.pack(">I", ~sum(footer) & 0xFFFFFFFF)
    vhd.seek(-512, 2)
    vhd.write(footer)
PY

rm -rf "$TMP"
echo "Successfully created VHD: $VHD"
"""
)
"""The ``azure`` postoutput script outside ``nethermind-v1``: a reproducible VHD."""

GCP_POSTOUTPUT_SCRIPT_CURRENT = (
    """\
#!/bin/bash
set -euxo pipefail

EFI="${OUTPUTDIR}/${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}.efi"
TAR="${OUTPUTDIR}/${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}.tar.gz"
TMP="${OUTPUTDIR}/gcp-tmp"

[ -f "$EFI" ] || { echo "Error: $EFI not found"; exit 1; }
mkdir -p "$TMP"

"""
    + _DETERMINISTIC_ESP
    + """
# GPT disk: GCP wants a whole number of GiB, the ESP plus 1 MiB in front and the backup GPT.
DISK="$TMP/disk.raw"
DISK_MB=$(( (ESP_MB + 2 + 1023) / 1024 * 1024 ))
"""
    + _DETERMINISTIC_DISK
    + """
# GCP's tar.gz: oldgnu with a sparse disk.raw, holes found by reading (not by the build
# filesystem's extents), fixed owner and date, and gzip without a name or timestamp.
touch -d "@$SOURCE_DATE_EPOCH" "$DISK"
tar --format=oldgnu --sparse --hole-detection=raw --sort=name \\
    --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \\
    -cf - -C "$TMP" disk.raw | gzip -n >"$TAR"

rm -rf "$TMP"
"""
)
"""The ``gcp`` postoutput script outside ``nethermind-v1``: a reproducible ``tar.gz``."""

_CURRENT_CLOUD_SCRIPTS = {
    AZURE_POSTOUTPUT_SCRIPT: AZURE_POSTOUTPUT_SCRIPT_CURRENT,
    GCP_POSTOUTPUT_SCRIPT: GCP_POSTOUTPUT_SCRIPT_CURRENT,
}

HISTORICAL_OUTPUT_NAME = "${IMAGE_ID}_${IMAGE_VERSION}"
"""The output name the cloud postoutput scripts spell under the ``nethermind-v1`` dialect."""
OUTPUT_NAME = "${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}"
"""The name mkosi gives its output: ``IMAGE_ID``, plus ``_IMAGE_VERSION`` when a version is set."""


def cloud_postoutput_script(script: str, dialect: str = "current") -> str:
    """*script* (a cloud postoutput script) as *dialect* emits it.

    ``nethermind-v1`` keeps the historical scripts verbatim. The ``current`` dialect
    emits the reproducible :data:`AZURE_POSTOUTPUT_SCRIPT_CURRENT` and
    :data:`GCP_POSTOUTPUT_SCRIPT_CURRENT`, which read mkosi's output name whether or
    not the image has a version (any other script: that name substituted).
    """
    if dialect == "nethermind-v1":
        return script
    current = _CURRENT_CLOUD_SCRIPTS.get(script)
    return current if current is not None else script.replace(HISTORICAL_OUTPUT_NAME, OUTPUT_NAME)


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
    """Configuration a lowered recipe passes to the emitter."""

    base: str
    arch: Arch = "x86_64"
    reproducible: bool = True
    kernel: Kernel | None = None
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    snapshot: str | None = None
    """mkosi's ``Snapshot=``: the distribution snapshot the build's packages come from."""
    output_format: str = "uki"
    seed: str = DEFAULT_SEED
    compress_output: str | None = None
    output_directory: str | None = None
    with_network: bool = True
    clean_package_metadata: bool = True
    kernel_modules: tuple[str, ...] | Literal["all"] = ()
    """``Debian(kernel_modules=...)``: see :func:`tundravm.kernel_modules.module_selection`."""
    manifest_format: str = "json"
    sandbox_trees: tuple[str, ...] = ()
    sandbox_files: tuple[tuple[str, str], ...] = ()
    """``(path, content)`` written under ``mkosi.sandbox/`` (the package manager's sandbox)."""
    package_cache_directory: str | None = None
    init_script: str | None = None
    generate_version_script: bool = False
    generate_cloud_postoutput: bool = True
    environment: dict[str, str] | None = None
    environment_passthrough: tuple[str, ...] | None = None
    emit_mode: Literal["per_directory", "native_profiles"] = "per_directory"
    settings: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """Verbatim ``[section] key=value`` lines the compiler does not write itself."""
    bootable: bool = True
    """``False`` writes ``Bootable=no`` and ``Format=disk``: mkosi needs a kernel for a UKI."""
    dialect: Literal["current", "nethermind-v1"] = "current"
    """``nethermind-v1`` keeps the historical cloud postoutput scripts."""
    kernel_pin: str | None = None
    """The kernel source's pin (commit or sha256); ``None`` while unpinned."""
    kernel_checkout: str | None = None
    """Outside ``nethermind-v1``: the kernel's host-fetched checkout ``<name>-<pin[:12]>``,
    which the build script copies from ``$SRCDIR/tundravm-sources``."""
    kernel_distribution: str | None = None
    """Outside ``nethermind-v1``: the fingerprint of what the pinned kernel compiles
    against (``Lowered.build_distribution``), which its cache key ends with."""
    kernel_files: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """A composed kernel's downloads: ``(url, mounted path, lines that check it)``; an
    unpinned one has no path and lines that fail (``KernelFile.mounted``)."""
    profiles: Mapping[str, EmitConfig] = field(default_factory=dict)
    """Profiles with their own settings or kernel; the rest use this configuration."""

    def for_profile(self, name: str) -> EmitConfig:
        """The configuration profile *name* is emitted with."""
        return self.profiles.get(name, self)


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


def _parse_mode(mode: str) -> int:
    """Parse an octal file mode string."""
    try:
        return int(mode, 8)
    except ValueError as exc:
        raise ValidationError(
            "Invalid file mode value.",
            hint="Use a valid octal string such as 0644 or 0755.",
            context={"mode": mode},
        ) from exc


def _make_dir(path: Path) -> None:
    """Create *path* and its missing parents as ``DIR_MODE``, whatever the umask."""
    missing: list[Path] = []
    while not path.is_dir():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir()
        directory.chmod(DIR_MODE)


def _write(dest: Path, content: str | bytes, mode: int) -> None:
    """Write *content* to *dest* with exactly *mode*, whatever the umask."""
    _make_dir(dest.parent)
    if isinstance(content, bytes):
        dest.write_bytes(content)
    else:
        dest.write_text(content, encoding="utf-8")
    dest.chmod(mode)


def _write_file_entry(dest: Path, entry: FileEntry) -> None:
    """Write *entry* at *dest*: its bytes, a symlink to its target or a directory."""
    if entry.kind == "symlink":
        _make_dir(dest.parent)
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        target = entry.content.decode() if isinstance(entry.content, bytes) else entry.content
        dest.symlink_to(target)
    elif entry.kind == "directory":
        _make_dir(dest)
        dest.chmod(_parse_mode(entry.mode))
    else:
        _write(dest, entry.content, _parse_mode(entry.mode))


def _write_repository(
    root: Path, repo: RepositorySpec, safe_name: str, suite: str, *, types: str
) -> None:
    """Write *repo* as ``<root>/etc/apt/sources.list.d/<safe_name>.sources`` and its pin."""
    source_lines = [
        f"Types: {types}",
        f"URIs: {repo.url}",
        f"Suites: {suite}",
        f"Components: {' '.join(repo.components or ('main',))}",
        "Enabled: yes",
    ]
    if repo.keyring:
        source_lines.append(f"Signed-By: {repo.keyring}")
    source_path = root / "etc" / "apt" / "sources.list.d" / f"{safe_name}.sources"
    _write(source_path, "\n".join(source_lines) + "\n", FILE_MODE)

    if repo.priority != 100:
        host = urlparse(repo.url).netloc or repo.url
        pref_lines = [
            "Package: *",
            f'Pin: origin "{host}"',
            f"Pin-Priority: {repo.priority}",
        ]
        pref_path = root / "etc" / "apt" / "preferences.d" / f"{safe_name}.pref"
        _write(pref_path, "\n".join(pref_lines) + "\n", FILE_MODE)


def _systemd_unit_content(svc: ServiceSpec) -> str:
    """Generate a real systemd .service unit file from a ServiceSpec (``_units.render``)."""
    return service_unit(svc)


def _groupadd_command(group: GroupSpec) -> str:
    """Generate a groupadd shell command from a GroupSpec."""
    parts: list[str] = ["mkosi-chroot groupadd"]
    if group.system:
        parts.append("--system")
    if group.gid is not None:
        parts.extend(["--gid", str(group.gid)])
    parts.append(group.name)
    return " ".join(parts)


def _unit_state_commands(profile: ProfileState) -> list[CommandSpec]:
    """``systemctl disable`` then ``systemctl mask``, one line each, units in declaration order."""
    commands: list[CommandSpec] = []
    for action in get_args(UnitAction):
        units = dict.fromkeys(s.unit for s in profile.unit_states if s.action == action)
        if units:
            line = f"mkosi-chroot systemctl {action} {' '.join(units)}"
            commands.append(CommandSpec(argv=(line,)))
    return commands


ACCOUNT_TOOLS_PACKAGE = "passwd"
"""The package with ``useradd``/``groupadd``, which the postinst lines run in the image."""


def _image_packages(profile: ProfileState) -> list[str]:
    """*profile*'s packages, plus the account tools when it declares users or groups."""
    packages = set(profile.packages)
    if profile.users or profile.groups:
        packages.add(ACCOUNT_TOOLS_PACKAGE)
    return sorted(packages)


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


_COMMIT = re.compile(r"[0-9a-f]{40}")
"""A full commit hash: fetched by hash, since ``git clone --branch`` takes names only."""


def _tagged_kernel(kernel: Kernel) -> bool:
    """Whether *kernel* is tag ``v<version>`` of a repository, the historical clone."""
    ref = kernel.source_ref
    return (
        kernel.source_archive is None
        and (ref is None or ref == f"v{kernel.version or 'unknown'}")
        and not (kernel.source_subdir or kernel.source_submodules)
    )


def _kernel_fetch(kernel: Kernel) -> list[str]:
    """Shell lines that put the kernel tree at ``$KERNEL_CACHE/src``."""
    version = kernel.version or "unknown"
    if kernel.source_archive is not None:
        archive = '"$KERNEL_CACHE/linux.tar"'
        lines = [f"curl -fsSL {shlex.quote(kernel.source_archive)} -o {archive}"]
        if kernel.source_sha256 is not None:
            lines.append(f'echo "{kernel.source_sha256}  $KERNEL_CACHE/linux.tar" | sha256sum -c -')
        lines.extend(
            (
                'mkdir -p "$KERNEL_CACHE/src"',
                f'tar -xf {archive} -C "$KERNEL_CACHE/src" --strip-components=1',
            )
        )
        return lines
    if _tagged_kernel(kernel):
        return [
            'git clone --depth 1 --branch "v${KERNEL_VERSION}" \\',
            f'    {kernel.source_repo} "$KERNEL_CACHE/src"',
        ]
    ref = kernel.source_ref or f"v{version}"
    repo = shlex.quote(kernel.source_repo)
    dest = '"$KERNEL_CACHE/repo"' if kernel.source_subdir else '"$KERNEL_CACHE/src"'
    if _COMMIT.fullmatch(ref):
        lines = [
            f"git init -q {dest}",
            f"git -C {dest} fetch --depth 1 {repo} {ref}",
            f"git -C {dest} checkout -q FETCH_HEAD",
        ]
        if kernel.source_submodules:
            lines.append(f"git -C {dest} submodule update --init --recursive --depth 1")
    else:
        submodules = (
            " --recurse-submodules --shallow-submodules" if kernel.source_submodules else ""
        )
        lines = [
            f"git clone --depth 1{submodules} --branch {shlex.quote(ref)} \\",
            f"    {repo} {dest}",
        ]
    if kernel.source_subdir:
        lines.append(f'ln -s {shlex.quote("repo/" + kernel.source_subdir)} "$KERNEL_CACHE/src"')
    return lines


def _kernel_copy(kernel: Kernel, checkout: str | None) -> list[str]:
    """Shell lines that copy the host-fetched kernel *checkout* to ``$KERNEL_CACHE/src``.

    The copy leaves out ``.git`` (the release string is the version, whatever
    ref was fetched), the fetch marker and a downloaded archive. Without
    *checkout* the kernel is unpinned and the lines only fail.
    """
    if checkout is None:
        version = kernel.version or "unknown"
        message = (
            f"tundravm: kernel {version} is not pinned: run tundravm lock, then tundravm fetch"
        )
        return [f"echo {shlex.quote(message)} >&2", "exit 1"]
    source = f'"$SRCDIR/{SOURCES_MOUNT}/{checkout}"'
    missing = f"tundravm: {checkout} is not fetched: run tundravm fetch RECIPE"
    dest = '"$KERNEL_CACHE/repo"' if kernel.source_subdir else '"$KERNEL_CACHE/src"'
    skip = [".git", FETCH_MARKER]
    if kernel.source_archive is not None:
        skip.append(HttpSource(kernel.source_archive).filename)
    names = " ".join(f"! -name {shlex.quote(name)}" for name in skip)
    lines = [
        f"[ -f {source}/{FETCH_MARKER} ] || {{ echo {shlex.quote(missing)} >&2; exit 1; }}",
        f"mkdir -p {dest}",
        f"find {source} -mindepth 1 -maxdepth 1 {names} \\",
        f"    -exec cp -a --no-preserve=ownership -t {dest} {{}} +",
    ]
    if kernel.source_subdir:
        lines.append(f'ln -s {shlex.quote("repo/" + kernel.source_subdir)} "$KERNEL_CACHE/src"')
    return lines


def _kernel_source_identity(kernel: Kernel) -> str:
    """What the cache key adds for a source other than tag ``v<version>`` of a repository."""
    if _tagged_kernel(kernel):
        return ""
    if kernel.source_archive is not None:
        return f"archive:{kernel.source_archive}:{kernel.source_sha256}"
    return ":".join(
        (
            "git",
            kernel.source_repo,
            kernel.source_ref or "",
            kernel.source_subdir or "",
            str(kernel.source_submodules),
        )
    )


_LEGACY_CONFIGURE = """\
cp kernel/kernel.config "$KERNEL_CACHE/src/.config"
cd "$KERNEL_CACHE/src"

# Reproducibility environment
export KBUILD_BUILD_TIMESTAMP="1970-01-01"
export KBUILD_BUILD_USER="tundravm"
export KBUILD_BUILD_HOST="tundravm"

make olddefconfig
make -j"$(nproc)" bzImage ARCH=x86_64"""

_MODULES_INSTALL = """\
RELEASE=$(make -s -C "$KERNEL_CACHE/src" kernelrelease)
if [ "$RELEASE" != "$KERNEL_VERSION" ]; then
    echo "tundravm: the kernel builds release $RELEASE, not $KERNEL_VERSION" >&2
    exit 1
fi
make -C "$KERNEL_CACHE/src" modules_install INSTALL_MOD_PATH="${DESTDIR}/usr" \\
    INSTALL_MOD_STRIP=1 ARCH=x86_64"""

_KCONFIG_CHECK = """\
# Kconfig drops a requested option whose dependencies are unmet: check each survived
kconfig_check() {
    local final
    final=$(sed -n "s/^$1=//p" .config)
    if [ "${final:-n}" != "$2" ]; then
        echo "tundravm: kernel-option-dropped: $1 requested $2, final ${final:-n}" >&2
        return 1
    fi
}
dropped=0"""


def kernel_patch_file(index: int, name: str) -> str:
    """Where a local kernel patch lands in the tree, under ``kernel/patches``."""
    return f"{index:02d}-{name}"


def kernel_fragment(kernel: Kernel) -> str:
    """The requested options as a ``.config`` fragment (``kernel/fragment.config``)."""
    return "".join(
        f"# {key} is not set\n" if value == "n" else f"{key}={value}\n"
        for key, value in kernel.options
    )


def _kernel_file(
    files: Sequence[tuple[str, str, tuple[str, ...]]], url: str
) -> tuple[str, list[str]]:
    """The mounted *url* and its check lines; lines that fail when it is not mounted."""
    for found, path, lines in files:
        if found == url:
            return path, list(lines)
    message = f"tundravm: {url} is not pinned: run tundravm lock, then tundravm fetch"
    return "", [f"echo {shlex.quote(message)} >&2", "exit 1"]


def _composed_configure(kernel: Kernel, files: Sequence[tuple[str, str, tuple[str, ...]]]) -> str:
    """Base config, patches in order, the fragment, ``olddefconfig``, the check, the build."""
    lines = ['KERNEL_TREE="$PWD/kernel"']
    if kernel.base_deb is not None:
        deb, check = _kernel_file(files, kernel.base_deb)
        member = shlex.quote(kernel.base_member or "")
        lines += check
        lines += [
            f"dpkg-deb --fsys-tarfile {deb} | tar -xO {member} \\",
            '    | xz -dc > "$KERNEL_CACHE/src/.config"',
        ]
    else:
        lines.append('cp kernel/base.config "$KERNEL_CACHE/src/.config"')
    for index, patch in enumerate(kernel.patches, 1):
        if patch.url is not None:
            path, check = _kernel_file(files, patch.url)
            lines += check
        else:
            path = f'"$KERNEL_TREE/patches/{kernel_patch_file(index, patch.name)}"'
        failed = shlex.quote(f"tundravm: kernel patch {patch.name} does not apply")
        lines += [
            f'patch -d "$KERNEL_CACHE/src" -p1 --batch --forward < {path} \\',
            f"    || {{ echo {failed} >&2; exit 1; }}",
        ]
    lines += [
        'cd "$KERNEL_CACHE/src"',
        "",
        "# Reproducibility environment",
        'export KBUILD_BUILD_TIMESTAMP="1970-01-01"',
        'export KBUILD_BUILD_USER="tundravm"',
        'export KBUILD_BUILD_HOST="tundravm"',
        "",
        './scripts/kconfig/merge_config.sh -m .config "$KERNEL_TREE/fragment.config"',
        "make olddefconfig",
        _KCONFIG_CHECK,
        *(
            f"kconfig_check {key} {shlex.quote(value)} || dropped=1"
            for key, value in kernel.options
        ),
        '[ "$dropped" -eq 0 ] || exit 1',
        f'make -j"$(nproc)" bzImage{" modules" if kernel.modules else ""} ARCH=x86_64',
    ]
    return "\n".join(lines)


def _composed_identity(kernel: Kernel, files: Sequence[tuple[str, str, tuple[str, ...]]]) -> str:
    """What a composed kernel's cache key covers: base, fragment, patches and module mode."""

    def digest(path: str | Path | None) -> str:
        found = Path(path or "")
        return hashlib.sha256(found.read_bytes()).hexdigest() if found.is_file() else str(path)

    pins = {url: "\n".join(lines) for url, _, lines in files}
    parts = [
        f"base:{kernel.base_deb}:{pins.get(kernel.base_deb or '', '')}"
        if kernel.base_deb is not None
        else f"base:{digest(kernel.base_file)}",
        "fragment:" + kernel_fragment(kernel),
        *(
            f"patch:{p.name}:{p.url}:{pins.get(p.url, '')}"
            if p.url is not None
            else f"patch:{p.name}:{digest(p.path)}"
            for p in kernel.patches
        ),
        f"modules:{kernel.modules}",
    ]
    return "\n".join(parts)


def _emit_composed_kernel(directory: Path, kernel: Kernel) -> None:
    """Write a composed kernel's fragment, local base and local patches under *directory*."""
    _write(directory / "fragment.config", kernel_fragment(kernel), FILE_MODE)
    local = [(kernel.base_file, directory / "base.config")] if kernel.base_file else []
    for index, patch in enumerate(kernel.patches, 1):
        if patch.path is not None:
            local.append((patch.path, directory / "patches" / kernel_patch_file(index, patch.name)))
    for source, dest in local:
        path = Path(source or "")
        if not path.is_file():
            raise ValidationError(
                f"Kernel {kernel.version}: {path} does not exist.",
                hint="Pass the path of an existing file (assets.file(...)).",
                context={"path": str(path)},
            )
        _write(dest, path.read_text(encoding="utf-8"), FILE_MODE)


def _render_kernel_build_script(
    kernel: Kernel,
    dialect: str = "current",
    *,
    pin: str | None = None,
    checkout: str | None = None,
    distribution: str | None = None,
    files: Sequence[tuple[str, str, tuple[str, ...]]] = (),
) -> str:
    """Render a build script that fetches, configures, and compiles the Linux kernel.

    Under ``nethermind-v1`` the script clones (or downloads) the kernel in the
    sandbox and caches it in ``$BUILDDIR``. Every other dialect copies the
    host-fetched *checkout* of *pin* (see :func:`_kernel_copy`), keys the cache
    by the pin and the *distribution* fingerprint, so another snapshot, mirror
    or toolchain rebuilds, and falls back to the build overlay for it, since
    mkosi sets ``$BUILDDIR`` only with ``BuildDirectory=``. A composed kernel
    (``base``) starts from its base config, applies its patches in order, merges
    ``kernel/fragment.config``, runs ``olddefconfig`` and checks every requested
    option survived (*files*: its mounted downloads, ``EmitConfig.kernel_files``).
    """
    historical = dialect == "nethermind-v1"
    cache_root = "${BUILDDIR}" if historical else "${BUILDDIR:-$BUILDROOT/build}"
    version = kernel.version or "unknown"
    config_hash_source = str(kernel.config_file)
    if kernel.config_file:
        config_path = Path(kernel.config_file)
        if config_path.exists():
            config_hash_source = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if kernel.composed:
        config_hash_source = _composed_identity(kernel, files)
    source_identity = _kernel_source_identity(kernel)
    if source_identity:
        config_hash_source += "\n" + source_identity
    config_hash = hashlib.sha256(config_hash_source.encode()).hexdigest()[:12]
    cache_key = f"kernel-{version}-{config_hash}"
    if historical:
        fetch_lines = _kernel_fetch(kernel)
    else:
        fetch_lines = _kernel_copy(kernel, checkout if pin is not None else None)
        if pin is not None:
            cache_key += f"-{pin[:12]}"
            if distribution is not None:
                cache_key += f"-{distribution}"
    fetch = ("\n" + " " * 12).join(fetch_lines)
    configure_text = _composed_configure(kernel, files) if kernel.composed else _LEGACY_CONFIGURE
    configure = ("\n" + " " * 12).join(configure_text.split("\n"))
    modules_install = ""
    if kernel.composed and kernel.modules:
        modules_install = ("\n" + " " * 8).join(("", *_MODULES_INSTALL.split("\n")))
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        KERNEL_CACHE="{cache_root}/{cache_key}"
        KERNEL_VERSION="{version}"

        if [ -d "$KERNEL_CACHE/done" ]; then
            echo "Using cached kernel build: {cache_key}"
        else
            rm -rf "$KERNEL_CACHE"
            mkdir -p "$KERNEL_CACHE"

            {fetch}

            {configure}

            mkdir -p "$KERNEL_CACHE/done"
        fi

        # Install kernel to destination
        INSTALL_DIR="${{DESTDIR}}/usr/lib/modules/${{KERNEL_VERSION}}"
        mkdir -p "$INSTALL_DIR"
        cp "$KERNEL_CACHE/src/arch/x86/boot/bzImage" "$INSTALL_DIR/vmlinuz"{modules_install}

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
            _write(destination / "mkosi.version", MKOSI_VERSION_SCRIPT, EXEC_MODE)

        for profile_name in sorted(profile_names):
            _require_profile(recipe, profile_name)
            # Each profile directory is a complete image: the profile merged over the
            # profile it extends.
            profile = recipe.effective_profile(profile_name)
            self._validate_profile_phases(profile_name=profile_name, profile=profile)

            profile_dir = destination / profile_name
            _reset_dir(profile_dir)
            profile_config = config.for_profile(profile_name)
            modules = _module_selection(profile, profile_config)

            # Generate mkosi.extra/ overlay tree (files, templates, service units)
            self._emit_extra_tree(profile_dir, profile)

            # Generate mkosi.skeleton/ tree
            self._emit_skeleton_tree(profile_dir, profile, profile_config)
            self._emit_sandbox_tree(profile_dir, profile_config)

            # Copy kernel config file if kernel has one
            self._emit_kernel_config(profile_dir, profile_config)

            # Generate phase scripts + synthetic postinst/finalize
            phase_scripts = self._emit_all_scripts(
                profile_name=profile_name,
                profile_dir=profile_dir,
                profile=profile,
                recipe=recipe,
                config=profile_config,
                modules=modules,
            )

            # Emit cloud postoutput scripts based on output_targets
            cloud_scripts: tuple[Path, ...] = ()
            if config.generate_cloud_postoutput:
                cloud_scripts = self._emit_cloud_postoutput(profile_dir, profile, profile_config)

            # Generate mkosi.conf
            conf_content = self._render_conf(
                profile_name=profile_name,
                config=profile_config,
                packages=_image_packages(profile),
                build_packages=sorted(profile.build_packages),
                build_sources=profile.build_sources or None,
                repositories=profile.repositories,
                phase_scripts=phase_scripts,
                cloud_postoutput_scripts=cloud_scripts,
                modules=modules,
            )

            conf_path = profile_dir / "mkosi.conf"
            _write(conf_path, conf_content, FILE_MODE)

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
        """Emit the default profile as the root mkosi.conf, others as mkosi.profiles/<name>/.

        mkosi layers ``mkosi.profiles/<name>/`` over the root config, so each overlay
        holds only what that profile adds to the default profile it extends.
        """
        destination.mkdir(parents=True, exist_ok=True)
        profile_paths: dict[str, Path] = {}
        script_paths: dict[str, dict[Phase, Path]] = {}

        # Emit mkosi.version at emission root when enabled
        if config.generate_version_script:
            _write(destination / "mkosi.version", MKOSI_VERSION_SCRIPT, EXEC_MODE)

        for profile_name in profile_names:
            _require_profile(recipe, profile_name)
        default_name = recipe.default_profile
        own_config = sorted(name for name in config.profiles if name != default_name)
        if own_config:
            raise ValidationError(
                "native_profiles mode cannot give a profile its own settings or kernel.",
                hint=(
                    "mkosi applies the root mkosi.conf (the default profile) to every profile; "
                    "use emit_mode='per_directory'."
                ),
                context={"profiles": ", ".join(own_config), "operation": "emit_mkosi"},
            )
        default = recipe.effective_profile(default_name)
        self._validate_profile_phases(profile_name=default_name, profile=default)
        # The root tree boots every profile: it keeps the modules all of them select.
        modules = _module_selection(default, config)
        for profile_name in profile_names:
            effective = recipe.effective_profile(profile_name)
            modules = modules.union(_module_selection(effective, config))

        # Root: the full default profile
        self._emit_skeleton_tree(destination, default, config)
        self._emit_sandbox_tree(destination, config)
        self._emit_extra_tree(destination, default)
        self._emit_kernel_config(destination, config)
        root_scripts = self._emit_all_scripts(
            profile_name=default_name,
            profile_dir=destination,
            profile=default,
            recipe=recipe,
            config=config,
            modules=modules,
        )
        root_cloud: tuple[Path, ...] = ()
        if config.generate_cloud_postoutput:
            root_cloud = self._emit_cloud_postoutput(destination, default, config)
        _write(
            destination / "mkosi.conf",
            self._render_conf(
                profile_name=default_name,
                config=config,
                packages=_image_packages(default),
                build_packages=sorted(default.build_packages),
                build_sources=default.build_sources or None,
                repositories=default.repositories,
                phase_scripts=root_scripts,
                cloud_postoutput_scripts=root_cloud,
                modules=modules,
            ),
            FILE_MODE,
        )

        # Per-profile overlays under mkosi.profiles/<name>/; the root already built
        # the kernel and wrote the init script.
        profiles_dir = destination / "mkosi.profiles"
        _make_dir(profiles_dir)
        overlay_config = replace(config, kernel=None, init_script=None)

        for profile_name in sorted(profile_names):
            overlay = _native_overlay(recipe, profile_name)
            self._validate_profile_phases(profile_name=profile_name, profile=overlay)

            profile_dir = profiles_dir / profile_name
            _reset_dir(profile_dir)
            self._emit_skeleton_tree(profile_dir, overlay, overlay_config)
            self._emit_extra_tree(profile_dir, overlay)
            phase_scripts = self._emit_all_scripts(
                profile_name=profile_name,
                profile_dir=profile_dir,
                profile=overlay,
                recipe=recipe,
                config=overlay_config,
            )
            cloud_scripts: tuple[Path, ...] = ()
            if config.generate_cloud_postoutput:
                cloud_scripts = self._emit_cloud_postoutput(profile_dir, overlay, config)

            conf_content = self._render_conf(
                profile_name=profile_name,
                config=config,
                packages=_image_packages(overlay),
                build_packages=sorted(overlay.build_packages),
                build_sources=overlay.build_sources or None,
                repositories=overlay.repositories,
                phase_scripts=phase_scripts,
                cloud_postoutput_scripts=cloud_scripts,
            )
            conf_path = profile_dir / "mkosi.conf"
            _write(conf_path, conf_content, FILE_MODE)

            profile_paths[profile_name] = conf_path
            script_paths[profile_name] = (
                root_scripts if profile_name == default_name else phase_scripts
            )

        return MkosiEmission(
            root=destination,
            profile_paths=profile_paths,
            script_paths=script_paths,
        )

    def _emit_cloud_postoutput(
        self, profile_dir: Path, profile: ProfileState, config: EmitConfig
    ) -> tuple[Path, ...]:
        """Emit cloud-specific postoutput scripts based on output_targets."""
        emitted: list[Path] = []
        targets = profile.output_targets
        if "gcp" in targets:
            gcp_script = profile_dir / "scripts" / "gcp-postoutput.sh"
            _write(
                gcp_script,
                cloud_postoutput_script(GCP_POSTOUTPUT_SCRIPT, config.dialect),
                EXEC_MODE,
            )
            emitted.append(gcp_script)
        if "azure" in targets:
            azure_script = profile_dir / "scripts" / "azure-postoutput.sh"
            _write(
                azure_script,
                cloud_postoutput_script(AZURE_POSTOUTPUT_SCRIPT, config.dialect),
                EXEC_MODE,
            )
            emitted.append(azure_script)
        return tuple(emitted)

    def _emit_sandbox_tree(self, profile_dir: Path, config: EmitConfig) -> None:
        """Write ``config.sandbox_files`` under ``mkosi.sandbox/``.

        mkosi picks the directory up as a sandbox tree by itself and copies it over
        the sandbox its package manager runs in; nothing of it reaches the image.
        """
        for path, content in config.sandbox_files:
            _write(profile_dir / "mkosi.sandbox" / path.lstrip("/"), content, FILE_MODE)

    def _validate_profile_phases(self, *, profile_name: str, profile: ProfileState) -> None:
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
        _make_dir(extra_dir)

        # Files from img.file()
        for entry in profile.files:
            _write_file_entry(extra_dir / entry.path.lstrip("/"), entry)

        # Rendered templates from img.template()
        for tmpl in profile.templates:
            _write(extra_dir / tmpl.path.lstrip("/"), tmpl.rendered, _parse_mode(tmpl.mode))

        # Systemd service unit files from img.service()
        for svc in profile.services:
            # Skip enablement-only registrations (no command = no unit file to generate)
            if not svc.command:
                continue
            unit_name = svc.name if svc.name.endswith(".service") else f"{svc.name}.service"
            # Skip non-service targets (like secrets-ready.target)
            if svc.name.endswith(".target"):
                continue
            dest = extra_dir / "usr" / "lib" / "systemd" / "system" / unit_name
            _write(dest, _systemd_unit_content(svc), FILE_MODE)

    def _emit_skeleton_tree(
        self, profile_dir: Path, profile: ProfileState, config: EmitConfig
    ) -> None:
        """Generate mkosi.skeleton/ with pre-package-manager files.

        Outside ``nethermind-v1``, repositories also go under ``mkosi.sandbox/``
        (where the build's apt reads them) with any keyring the profile declares.
        """
        skeleton_dir = profile_dir / "mkosi.skeleton"
        _make_dir(skeleton_dir)

        # Write custom init script if configured
        if config.init_script:
            _write(skeleton_dir / "init", config.init_script, EXEC_MODE)

        # Write skeleton files from img.skeleton()
        for entry in profile.skeleton_files:
            _write_file_entry(skeleton_dir / entry.path.lstrip("/"), entry)

        # Additional apt repositories from image.repository(...)
        distribution, release = _parse_base(config.base)
        historical = config.dialect == "nethermind-v1"
        declared = {entry.path: entry for entry in (*profile.skeleton_files, *profile.files)}
        sandbox_dir = profile_dir / "mkosi.sandbox"
        for repo in profile.repositories:
            safe_name = repo.name.lower().replace("/", "-").replace(" ", "-")
            suite = repo.suite or release or distribution
            if historical or repo.in_image:
                # nethermind-v1 lists every repository in the image only, as it always has.
                _write_repository(skeleton_dir, repo, safe_name, suite, types="deb deb-src")
            if historical:
                continue
            # mkosi's apt reads only the sandbox's /etc/apt during the build.
            _write_repository(sandbox_dir, repo, safe_name, suite, types="deb")
            keyring = declared.get(repo.keyring or "")
            if keyring is not None:
                _write_file_entry(sandbox_dir / keyring.path.lstrip("/"), keyring)

        # Auto-emit minimal.target when systemd debloat sets it as default
        if profile.debloat.enabled and profile.debloat.systemd_minimize:
            target_path = skeleton_dir / "etc" / "systemd" / "system" / "minimal.target"
            if not target_path.exists():
                _write(target_path, MINIMAL_TARGET_UNIT, FILE_MODE)

    def _emit_kernel_config(self, profile_dir: Path, config: EmitConfig) -> None:
        """Copy kernel config file into the output tree if present.

        A composed kernel writes ``kernel/fragment.config``, its local base as
        ``kernel/base.config`` and its local patches under ``kernel/patches``.
        """
        if config.kernel and config.kernel.composed:
            _emit_composed_kernel(profile_dir / "kernel", config.kernel)
            return
        if config.kernel and config.kernel.config_file:
            config_src = Path(config.kernel.config_file)
            if not config_src.exists():
                raise ValidationError(
                    "Kernel config file does not exist.",
                    hint="Provide a valid kernel config file before compiling.",
                    context={"path": str(config.kernel.config_file)},
                )
            _write(
                profile_dir / "kernel" / "kernel.config",
                config_src.read_text(encoding="utf-8"),
                FILE_MODE,
            )

    def _emit_all_scripts(
        self,
        *,
        profile_name: str,
        profile_dir: Path,
        profile: ProfileState,
        recipe: RecipeState,
        config: EmitConfig | None = None,
        modules: ModuleSelection = NO_MODULES,
    ) -> dict[Phase, Path]:
        """Emit phase scripts + synthetic postinst/finalize."""
        scripts_dir = profile_dir / "scripts"
        _make_dir(scripts_dir)
        phase_scripts: dict[Phase, Path] = {}

        # Emit user-defined phase scripts
        for index, phase in enumerate(PHASE_ORDER, start=1):
            commands = profile.phases.get(phase, [])

            # For postinst: prepend user creation, service enablement, debloat masking
            if phase == "postinst":
                synthetic = self._synthetic_postinst_commands(profile)
                all_commands = synthetic + list(commands) + _unit_state_commands(profile)
                needs_debloat = profile.debloat.enabled and profile.debloat.systemd_minimize
                if all_commands or needs_debloat:
                    script_name = f"{index:02d}-{phase}.sh"
                    script_path = scripts_dir / script_name
                    _write(
                        script_path,
                        self._render_postinst_script(all_commands, profile),
                        EXEC_MODE,
                    )
                    phase_scripts[phase] = script_path
                continue

            # For finalize: prepend debloat path removal
            if phase == "finalize":
                synthetic_lines = self._synthetic_finalize_lines(profile, modules)
                accounts = _sorts_accounts(config)
                if synthetic_lines or commands or accounts:
                    script_name = f"{index:02d}-{phase}.sh"
                    script_path = scripts_dir / script_name
                    _write(
                        script_path,
                        self._render_finalize_script(synthetic_lines, commands, accounts=accounts),
                        EXEC_MODE,
                    )
                    phase_scripts[phase] = script_path
                continue

            # For build: prepend kernel build script if kernel has config_file
            if phase == "build" and config and config.kernel and config.kernel.built:
                kernel_script = _render_kernel_build_script(
                    config.kernel,
                    config.dialect,
                    pin=config.kernel_pin,
                    checkout=config.kernel_checkout,
                    distribution=config.kernel_distribution,
                    files=config.kernel_files,
                )
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
                _write(script_path, combined, EXEC_MODE)
                phase_scripts[phase] = script_path
                continue

            if not commands:
                continue
            script_name = f"{index:02d}-{phase}.sh"
            script_path = scripts_dir / script_name
            _write(script_path, self._render_script(commands), EXEC_MODE)
            phase_scripts[phase] = script_path

        return phase_scripts

    def _synthetic_postinst_commands(self, profile: ProfileState) -> list[CommandSpec]:
        """Create synthetic commands for user creation and service enablement."""
        commands: list[CommandSpec] = []

        # Group creation, before any user that joins them
        for group in profile.groups:
            commands.append(CommandSpec(argv=(_groupadd_command(group),)))

        # User creation via mkosi-chroot
        for user in profile.users:
            commands.append(CommandSpec(argv=(_useradd_command(user),)))

        # Service enablement via mkosi-chroot systemctl enable + minimal.target.wants
        enabled_units: list[str] = []
        for svc in profile.services:
            if svc.enabled:
                unit_name = svc.name if "." in svc.name else f"{svc.name}.service"
                commands.append(
                    CommandSpec(argv=(f"mkosi-chroot systemctl enable {shlex.quote(unit_name)}",))
                )
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

    def _synthetic_finalize_lines(
        self, profile: ProfileState, modules: ModuleSelection = NO_MODULES
    ) -> list[str]:
        """Generate the kernel module check and debloat shell lines for the finalize script."""
        config = profile.debloat
        lines = _kernel_module_check(modules.modules)
        if not config.enabled:
            return lines
        if lines:
            lines.append("")

        # Clean files in var directories (preserve directory structure)
        if config.clean_var_dirs:
            lines.append("# Debloat: clean files in var directories")
            for var_dir in sorted(config.clean_var_dirs):
                lines.append(f'find "$BUILDROOT{var_dir}" -type f -delete')

        # Path removal (remains in finalize — runs on host with $BUILDROOT)
        paths = debloat_paths_remove(config, modules, declared_paths(profile))
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
            units_keep = debloat_units_keep(profile)

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
        self, synthetic_lines: list[str], commands: list[CommandSpec], *, accounts: bool = False
    ) -> str:
        """Render finalize script with debloat path removal + user commands, then
        (*accounts*) :data:`ACCOUNTS_FINALIZE`, after anything that adds an account."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        lines.extend(synthetic_lines)

        if commands:
            lines.append("")
            lines.append("# User-defined finalize commands")
            for command in commands:
                lines.append(self._render_command_line(command))

        lines.append("")
        text = "\n".join(lines)
        if accounts:
            text += ("\n" if synthetic_lines or commands else "") + ACCOUNTS_FINALIZE
        return text

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
        cloud_postoutput_scripts: tuple[Path, ...] = (),
        modules: ModuleSelection = NO_MODULES,
    ) -> str:
        distribution, release = _parse_base(config.base)
        lines: list[str] = []
        extra: dict[str, list[str]] = {}
        for section, key, values in config.settings:
            extra.setdefault(section, []).extend(f"{key}={value}" for value in values)

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
        if config.snapshot:
            lines.append(f"Snapshot={config.snapshot}")
        lines.extend(extra.pop("Distribution", ()))
        lines.append("")

        # [Output]
        lines.append("[Output]")
        output_format = config.output_format
        if not config.bootable and output_format == "uki":
            output_format = "disk"
        lines.append(f"Format={output_format}")
        lines.append(f"ImageId={profile_name}")
        lines.append(f"ManifestFormat={config.manifest_format}")
        if config.compress_output:
            lines.append(f"CompressOutput={config.compress_output}")
        if config.output_directory:
            lines.append(f"OutputDirectory={config.output_directory}")
        if config.reproducible:
            lines.append(f"Seed={config.seed}")
        lines.extend(extra.pop("Output", ()))
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
        if config.kernel and config.kernel.built:
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
        build_lines.extend(extra.pop("Build", ()))
        if build_lines:
            lines.append("[Build]")
            lines.extend(build_lines)
            lines.append("")

        # [Content]
        lines.append("[Content]")
        if config.reproducible:
            lines.append(f"SourceDateEpoch={env_vars['SOURCE_DATE_EPOCH']}")
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

        if modules.modules:
            module_lines = "\n".join(f"    {m}" for m in modules.modules)
            lines.append(f"KernelModules=\n{module_lines}")

        # Kernel configuration
        if not config.bootable:
            lines.append("Bootable=no")
        elif config.kernel and config.output_format == "uki":
            lines.append("Bootable=yes")
        if config.kernel:
            if config.kernel.cmdline:
                lines.append(f"KernelCommandLine={config.kernel.cmdline}")
            if config.kernel.version:
                lines.append(f"# KernelVersion={config.kernel.version}")
            if config.kernel.tdx:
                lines.append("# TDX-enabled kernel required")

        lines.extend(extra.pop("Content", ()))

        # Extra trees and skeleton
        lines.append("ExtraTrees=mkosi.extra")
        lines.append("SkeletonTrees=mkosi.skeleton")

        # Script references (part of [Content] section)
        if phase_scripts:
            lines.append("")
            for phase in PHASE_ORDER:
                script_path = phase_scripts.get(phase)
                if script_path is None:
                    continue
                lines.append(f"{PHASE_TO_MKOSI_KEY[phase]}=scripts/{script_path.name}")

        # Additional cloud conversion postoutput scripts.
        if cloud_postoutput_scripts:
            if not phase_scripts:
                lines.append("")
            for script_path in cloud_postoutput_scripts:
                lines.append(f"PostOutputScripts=scripts/{script_path.name}")

        for section, entries in extra.items():
            lines.extend(("", f"[{section}]", *entries))

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


def _sorts_accounts(config: EmitConfig | None) -> bool:
    """Whether finalize ends with :data:`ACCOUNTS_FINALIZE`: reproducible, current dialect."""
    return config is not None and config.reproducible and config.dialect != "nethermind-v1"


def _require_profile(recipe: RecipeState, name: str) -> None:
    if name not in recipe.profiles:
        raise ValidationError(
            "Profile does not exist for mkosi emission.",
            hint="Create the profile before calling compile().",
            context={"profile": name, "operation": "emit_mkosi"},
        )


def _module_selection(profile: ProfileState, config: EmitConfig) -> ModuleSelection:
    """*profile*'s kernel modules under *config* (a built kernel selects none)."""
    return module_selection(profile, config.kernel_modules, built_kernel=config.kernel is not None)


_KERNEL_MODULE_CHECK = textwrap.dedent(r"""
    # Kernel modules: mkosi kept KernelModules= and their dependencies (tundravm module sets
    # v@VERSION@); each must be in the image
    kdirs=("$BUILDROOT"/usr/lib/modules/*/)
    if [ ! -d "${kdirs[0]}" ]; then
        echo "tundravm: no kernel modules under /usr/lib/modules" >&2
        exit 1
    fi
    for kdir in "${kdirs[@]}"; do
        present=$({
            find "$kdir" -name '*.ko*' -printf '%f\n'
            sed 's|.*/||' "${kdir}modules.builtin" 2>/dev/null || true
        } | sed 's/\.ko.*//' | tr _ -)
        for module in @MODULES@; do
            if ! grep -qxF "${module//_/-}" <<<"$present"; then
                echo "tundravm: kernel module $module is not in kernel $(basename "$kdir"):" \
                    "drop it from Debian(kernel_modules=...) or pick a kernel that ships it" >&2
                exit 1
            fi
        done
    done
""").strip("\n")
"""The finalize check of :func:`_kernel_module_check`."""


def _kernel_module_check(modules: tuple[str, ...]) -> list[str]:
    """Finalize lines failing the build when one of *modules* is not in the image.

    mkosi pruned ``/usr/lib/modules`` to ``KernelModules=`` before the finalize scripts run;
    a module that is neither a ``.ko`` file nor built in is missing from this kernel.
    """
    if not modules:
        return []
    text = _KERNEL_MODULE_CHECK.replace("@VERSION@", str(MODULE_SETS_VERSION))
    return text.replace("@MODULES@", " ".join(modules)).split("\n")


def _native_overlay(recipe: RecipeState, name: str) -> ProfileState:
    """What ``mkosi.profiles/<name>/`` adds on top of the root (default profile) tree.

    Entries the default profile already ships, its output targets and its debloat
    are left out so nothing runs twice.
    """
    if name == recipe.default_profile:
        return ProfileState(name=name, output_targets=(), debloat=DebloatConfig(enabled=False))
    own = recipe.profiles[name]
    if own.extends is None:
        raise ValidationError(
            "native_profiles mode cannot emit a standalone profile.",
            hint=(
                "mkosi applies the root mkosi.conf (the default profile) to every profile; "
                "drop extends=None or use emit_mode='per_directory'."
            ),
            context={"profile": name, "operation": "emit_mkosi"},
        )
    default = recipe.effective_profile(recipe.default_profile)
    targets = own.output_targets if own.output_targets_explicit else ()
    return replace(
        own,
        files=[f for f in own.files if f not in default.files],
        services=[s for s in own.services if s not in default.services],
        groups=[g for g in own.groups if g not in default.groups],
        unit_states=[u for u in own.unit_states if u not in default.unit_states],
        output_targets=tuple(t for t in targets if t not in default.output_targets),
        debloat=own.debloat if own.debloat_explicit else DebloatConfig(enabled=False),
    )


def _reset_dir(path: Path) -> None:
    """Recreate *path* empty so files dropped from the recipe do not linger."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    _make_dir(path)


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
