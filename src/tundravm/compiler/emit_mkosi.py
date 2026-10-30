"""The mkosi-native tree: the payload subimage, the QEMU disk around it and the cloud wrappers.

:class:`DeterministicMkosiEmitter` writes one mkosi 27.1 project that uses mkosi's own
composition (``mkosi.images/``, ``mkosi.conf.d/``, ``mkosi.profiles/``), readable by any
mkosi user:

- ``mkosi.conf``: the settings every image shares (distribution, snapshot, tools tree,
  reproducibility) and the main image, the QEMU raw disk: the ESP around the payload's
  UKI (``ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI``);
- ``mkosi.images/payload/``: the OS, which includes ``payload/`` (subimages inherit none of
  the main image's content settings);
- ``mkosi.images/<cloud>/``: the same disk per cloud target, converted at postoutput
  (fixed VHD for Azure, oldgnu sparse ``tar.gz`` for GCP);
- ``payload/``: ``mkosi.conf``, one ``mkosi.conf.d/`` file per concern, the trees, the
  phase scripts in ``mkosi.<phase>.d/`` and one ``mkosi.profiles/<variant>/`` overlay per
  variant that adds content.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import textwrap
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlparse

from tundravm._options import RootLayout
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

MKOSI_VERSION = "27.1"
"""The mkosi release the tree is written for (``MinimumVersion=``): tundravm's pinned mkosi."""
PAYLOAD = "payload"
"""The OS subimage, its configuration directory and its output name (``payload.efi``)."""
MAIN_OUTPUT = "qemu"
"""The main image's output: the QEMU raw disk, ``qemu.raw``."""
ESP_TARGET = "/efi/EFI/BOOT/BOOTX64.EFI"
"""Where a disk image's root carries the UKI, which its ESP partition copies from ``/efi``."""
DEFAULT_OUTPUT_DIRECTORY = "mkosi.output"
"""``OutputDirectory=`` when the recipe sets none: ``%O`` must name a directory when parsed."""
MACHINE_ID = "/etc/machine-id"
"""Shipped ``uninitialized``: every boot of the payload generates its own machine id."""
CLOUD_GLUE = "cloud-glue"
"""The concern of the packages the cloud targets' glue installs (``mkosi.conf.d`` last)."""
OWNED_PATHS = (
    "mkosi.conf",
    "mkosi.version",
    "mkosi.repart",
    "mkosi.sandbox",
    "mkosi.profiles",
    "mkosi.images",
    PAYLOAD,
)
"""What the emitter owns in a tree directory, removed before it writes; mkosi's state
(``mkosi.tools``, ``mkosi.output``, caches) is left alone."""

PHASE_DIRECTORIES: dict[Phase, str] = {
    "sync": "mkosi.sync.d",
    "prepare": "mkosi.prepare.d",
    "build": "mkosi.build.d",
    "postinst": "mkosi.postinst.d",
    "finalize": "mkosi.finalize.d",
    "postoutput": "mkosi.postoutput.d",
    "clean": "mkosi.clean.d",
}
"""The script directories mkosi picks up by name, per phase, in the order mkosi runs them."""
PHASE_ORDER: tuple[Phase, ...] = tuple(PHASE_DIRECTORIES)
PHASE_TO_MKOSI_KEY: dict[Phase, str] = {
    "sync": "SyncScripts",
    "prepare": "PrepareScripts",
    "build": "BuildScripts",
    "postinst": "PostInstallationScripts",
    "finalize": "FinalizeScripts",
    "postoutput": "PostOutputScripts",
    "clean": "CleanScripts",
}
"""The mkosi setting of each phase's scripts (the tree uses the directories instead)."""

MAIN_IMAGE_SETTINGS = frozenset(
    {
        "Architecture", "BindUser", "BuildDirectory", "BuildSources", "BuildSourcesEphemeral",
        "CPUs", "CXL", "CacheDirectory", "Console", "Credentials", "DelegateRanges", "DiskType",
        "Distribution", "Drives", "Ephemeral", "ExtraSearchPaths", "Firmware",
        "FirmwareVariables", "ForeignUIDRange", "ForwardJournal", "History", "Incremental",
        "InitrdPackages", "InitrdProfiles", "InitrdVolatilePackages", "KVM",
        "KernelCommandLineExtra", "Linux", "LocalMirror", "Machine", "MakeScriptsExecutable",
        "MaxMem", "MinimumVersion", "Mirror", "NSpawnSettings", "OutputDirectory", "OutputMode",
        "PackageCacheDirectory", "PackageDirectories", "PassEnvironment",
        "ProxyClientCertificate", "ProxyClientKey", "ProxyExclude", "ProxyPeerCertificate",
        "ProxyUrl", "QemuArgs", "RAM", "Register", "Release", "Removable", "RepartOffline",
        "Repositories", "RepositoryKeyCheck", "RepositoryKeyFetch", "RuntimeBuildSources",
        "RuntimeNetwork", "RuntimeSize", "RuntimeTrees", "SandboxTrees", "SecureBootCertificate",
        "SecureBootCertificateSource", "SecureBootKey", "SecureBootKeySource", "Snapshot",
        "SourceDateEpoch", "SshCertificate", "SshKey", "StorageTargetMode", "SysupdateDirectory",
        "TPM", "ToolsTree", "ToolsTreeCertificates", "ToolsTreeDistribution", "ToolsTreeMirror",
        "ToolsTreePackageDirectories", "ToolsTreePackages", "ToolsTreeProfiles",
        "ToolsTreeRelease", "ToolsTreeRepositories", "ToolsTreeSandboxTrees",
        "ToolsTreeSnapshot", "UseSubvolumes", "VSock", "VSockCID", "VerityCertificate",
        "VerityCertificateSource", "VerityKey", "VerityKeySource", "VirtualMachineMonitor",
        "VolatilePackageDirectories", "WithNetwork", "WithTests", "WorkspaceDirectory",
    }
)  # fmt: skip
"""mkosi 27.1's settings of scope ``universal``, ``multiversal``, ``main``, ``tools`` and
``initrd``: a subimage may not set them, so a verbatim one goes to the root ``mkosi.conf``
(``tests/compiler/test_native_tree.py`` checks this list against mkosi's ``SETTINGS``)."""

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

# Minimal systemd target the systemd debloat makes the default target
MINIMAL_TARGET_UNIT = v1.MINIMAL_TARGET

ESP_PARTITION = """\
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
"""
"""The ESP of every disk around the payload: the UKI at ``/EFI/BOOT/BOOTX64.EFI``, sized to it."""
VERITY_DATA = "/payload/root.raw"
"""Where a disk image's root carries the verity payload's root partition (``CopyBlocks=``)."""
VERITY_HASH = "/payload/root-verity.raw"
"""Where a disk image's root carries the verity payload's hash partition."""
VERITY_ROOT_PARTITION = """\
[Partition]
Type=root
Format=erofs
CopyFiles=/
Verity=data
VerityMatchKey=root
SplitName=root
Minimize=best
"""
VERITY_HASH_PARTITION = """\
[Partition]
Type=root-verity
Verity=hash
VerityMatchKey=root
SplitName=root-verity
Minimize=best
"""

# Finalize: account databases in a fixed order, without the "-" backups.
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
"""The finalize step every reproducible payload ends with (its own block).

``cat >`` rewrites in place, so ``shadow`` keeps its mode and group."""

_GROW_GPT = """\
import struct, sys, zlib
# Grow a GPT disk to a multiple of UNIT bytes: move the backup table and header to the new
# end, update both headers and the protective MBR (repart sized the disk to its partitions).
path, unit, sector = sys.argv[1], int(sys.argv[2]), 512
with open(path, "r+b") as disk:
    size = disk.seek(0, 2)
    target = -(-size // unit) * unit
    disk.seek(sector)
    primary = bytearray(disk.read(sector))
    assert primary[:8] == b"EFI PART", "no GPT header"
    header_size = struct.unpack_from("<I", primary, 12)[0]
    entries_lba = struct.unpack_from("<Q", primary, 72)[0]
    count, entry_size = struct.unpack_from("<II", primary, 80)
    span = -(-(count * entry_size) // sector)
    disk.seek(entries_lba * sector)
    table = disk.read(span * sector)
    old_last = size // sector - 1
    disk.seek((old_last - span) * sector)
    disk.write(bytes((span + 1) * sector))
    disk.truncate(target)
    last = target // sector - 1

    def header(mine, other, entries):
        out = bytearray(primary[:header_size])
        struct.pack_into("<QQ", out, 24, mine, other)
        struct.pack_into("<Q", out, 48, last - span - 1)
        struct.pack_into("<Q", out, 72, entries)
        struct.pack_into("<I", out, 16, 0)
        struct.pack_into("<I", out, 16, zlib.crc32(out))
        return bytes(out)

    disk.seek(sector)
    disk.write(header(1, last, entries_lba))
    disk.seek((last - span) * sector)
    disk.write(table)
    disk.seek(last * sector)
    disk.write(header(last, 1, last - span))
    disk.seek(458)
    disk.write(struct.pack("<I", min(last, 0xFFFFFFFF)))
"""
"""Python run in the tools tree by the cloud converters (no ``sgdisk`` there)."""

AZURE_CONVERTER = f"""\
#!/bin/bash
# azure.raw as Azure takes it: a fixed VHD of a whole number of MiB, its footer's timestamp
# pinned to SOURCE_DATE_EPOCH and its unique id derived from the disk, so it reproduces.
# It converts a sparse copy: mkosi still reads azure.raw after the postoutput scripts.
set -euo pipefail
TMP="$OUTPUTDIR/azure-tmp"
DISK="$TMP/disk.raw"
VHD="$OUTPUTDIR/azure.vhd"
rm -rf "$TMP"
mkdir -p "$TMP"
cp --sparse=always --reflink=auto "$OUTPUTDIR/azure.raw" "$DISK"
python3 - "$DISK" 1048576 <<'PY'
{_GROW_GPT}PY
qemu-img convert -O vpc -o subformat=fixed,force_size "$DISK" "$VHD"
python3 - "$VHD" "${{SOURCE_DATE_EPOCH:-0}}" "$(sha256sum "$DISK" | cut -c1-32)" <<'PY'
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
"""
"""``mkosi.images/azure/mkosi.postoutput.d/vhd``: the reproducible fixed-VHD converter."""

GCP_CONVERTER = f"""\
#!/bin/bash
# gcp.raw as Compute Engine imports it: disk.raw of a whole number of GiB in an oldgnu tar.gz,
# holes found by reading, owner and dates fixed and gzip without name or timestamp.
# It archives a sparse copy: mkosi still reads gcp.raw after the postoutput scripts.
set -euo pipefail
TMP="$OUTPUTDIR/gcp-tmp"
rm -rf "$TMP"
mkdir -p "$TMP"
cp --sparse=always --reflink=auto "$OUTPUTDIR/gcp.raw" "$TMP/disk.raw"
python3 - "$TMP/disk.raw" 1073741824 <<'PY'
{_GROW_GPT}PY
EPOCH="${{SOURCE_DATE_EPOCH:-0}}"
touch -d "@$EPOCH" "$TMP/disk.raw"
tar --format=oldgnu --sparse --hole-detection=raw --sort=name \\
    --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner --mode=0644 \\
    -cf - -C "$TMP" disk.raw | gzip -n >"$OUTPUTDIR/gcp.tar.gz"
rm -rf "$TMP"
"""
"""``mkosi.images/gcp/mkosi.postoutput.d/tar``: the reproducible oldgnu sparse archive."""

CLOUD_CONVERTERS: dict[str, tuple[str, str]] = {
    "azure": ("vhd", AZURE_CONVERTER),
    "gcp": ("tar", GCP_CONVERTER),
}
"""Each cloud wrapper's postoutput script: its file name and text."""

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
    image_id: str = "image"
    """``ImageId=`` of every image: the recipe name."""
    reproducible: bool = True
    epoch: int = 0
    """``SourceDateEpoch=``: ``Platform.epoch``, else 0; always written."""
    kernel: Kernel | None = None
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    snapshot: str | None = None
    """mkosi's ``Snapshot=`` and ``ToolsTreeSnapshot=``: the distribution snapshot the
    packages and the tools tree come from."""
    seed: str = DEFAULT_SEED
    compress_output: str | None = None
    output_directory: str | None = None
    with_network: bool = False
    clean_package_metadata: bool = True
    kernel_modules: tuple[str, ...] | Literal["all"] = ()
    """``Debian(kernel_modules=...)``: see :func:`tundravm.kernel_modules.module_selection`."""
    manifest_format: str = "json"
    sandbox_trees: tuple[str, ...] = ()
    sandbox_files: tuple[tuple[str, str], ...] = ()
    """``(path, content)`` written under ``mkosi.sandbox/`` (the package manager's sandbox)."""
    package_cache_directory: str | None = None
    generate_version_script: bool = False
    environment: Mapping[str, str] = field(default_factory=dict)
    settings: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """Verbatim ``[section] key=value`` lines the compiler does not write itself."""
    bootable: bool = True
    """``False`` writes ``Bootable=no`` and a disk payload, and no disk around it."""
    root: RootLayout = "initramfs"
    """The payload's root layout: the UKI's ``.initrd``, or erofs + dm-verity partitions."""
    kernel_pin: str | None = None
    """The kernel source's pin (commit or sha256); ``None`` while unpinned."""
    kernel_checkout: str | None = None
    """The kernel's host-fetched checkout ``<name>-<pin[:12]>``, which the build script
    copies from ``$SRCDIR/tundravm-sources``."""
    kernel_distribution: str | None = None
    """The fingerprint of what the pinned kernel compiles against
    (``Lowered.build_distribution``), which its cache key ends with."""
    kernel_files: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """A composed kernel's downloads: ``(url, mounted path, lines that check it)``; an
    unpinned one has no path and lines that fail (``KernelFile.mounted``)."""
    targets: tuple[str, ...] = ()
    """The cloud wrappers to emit under ``mkosi.images/`` (``azure``, ``gcp``)."""
    overlays: tuple[str, ...] = ()
    """The profiles emitted as ``payload/mkosi.profiles/<name>/``."""
    payload_overlays: tuple[str, ...] | None = None
    """Every profile overlay of the recipe, emitted or not: the payload keeps the modules and
    units they need and stands its debloat aside for theirs, so its bytes do not depend on
    which variants a compile selects (``None``: :attr:`overlays`)."""
    concerns: Mapping[tuple[str, str], str] = field(default_factory=dict)
    """The payload's packages by ``(name, role)``: the ``mkosi.conf.d`` concern listing each."""
    overlay_sandbox_files: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    """Per overlay, ``(path, content)`` only its build sandbox gets
    (``mkosi.profiles/<name>/mkosi.sandbox/`` at the root)."""


@dataclass(frozen=True, slots=True)
class MkosiEmission:
    root: Path
    profile_paths: dict[str, Path] = field(default_factory=dict)
    """Each emitted profile's ``mkosi.conf`` (the payload's for the default profile)."""
    script_paths: dict[str, dict[Phase, Path]] = field(default_factory=dict)
    """Each emitted profile's first script per phase."""


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


_CONFIG_FILE_CONFIGURE = """\
cp payload/kernel/kernel.config "$KERNEL_CACHE/src/.config"
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
    lines = ['KERNEL_TREE="$PWD/payload/kernel"']
    if kernel.base_deb is not None:
        deb, check = _kernel_file(files, kernel.base_deb)
        member = shlex.quote(kernel.base_member or "")
        lines += check
        lines += [
            f"dpkg-deb --fsys-tarfile {deb} | tar -xO {member} \\",
            '    | xz -dc > "$KERNEL_CACHE/src/.config"',
        ]
    else:
        lines.append('cp payload/kernel/base.config "$KERNEL_CACHE/src/.config"')
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
    *,
    pin: str | None = None,
    checkout: str | None = None,
    distribution: str | None = None,
    files: Sequence[tuple[str, str, tuple[str, ...]]] = (),
) -> str:
    """Render a build script that configures and compiles the Linux kernel.

    The script copies the host-fetched *checkout* of *pin* (see :func:`_kernel_copy`), keys
    the cache by the pin and the *distribution* fingerprint, so another snapshot, mirror or
    toolchain rebuilds, and falls back to the build overlay for it, since mkosi sets
    ``$BUILDDIR`` only with ``BuildDirectory=``. A composed kernel (``base``) starts from
    its base config, applies its patches in order, merges ``payload/kernel/fragment.config``,
    runs ``olddefconfig`` and checks every requested option survived (*files*: its mounted
    downloads, ``EmitConfig.kernel_files``).
    """
    cache_root = "${BUILDDIR:-$BUILDROOT/build}"
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
    fetch_lines = _kernel_copy(kernel, checkout if pin is not None else None)
    if pin is not None:
        cache_key += f"-{pin[:12]}"
        if distribution is not None:
            cache_key += f"-{distribution}"
    fetch = ("\n" + " " * 12).join(fetch_lines)
    configure_text = (
        _composed_configure(kernel, files) if kernel.composed else _CONFIG_FILE_CONFIGURE
    )
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


_GLOB = re.compile(r"[*?]|\[[^]/]*\]")
"""The glob characters of a debloat removal entry (``/etc/ssh/ssh_host_*_key*``)."""


def _globbed_path(path: str) -> str:
    """*path* under ``$BUILDROOT`` as a shell word whose literal parts are quoted and whose glob
    characters expand; a glob matching nothing stays literal, which ``rm -rf`` ignores."""
    word, start = '"$BUILDROOT', 0
    for match in _GLOB.finditer(path):
        word += path[start : match.start()] + '"' + match.group() + '"'
        start = match.end()
    word += path[start:] + '"'
    return word.replace('""', "")


_DEBLOAT_MASK = textwrap.dedent(r"""
    # Debloat: mask unwanted systemd units
    systemd_svc_whitelist=(@KEEP@)
    SYSTEMD_DIR="$BUILDROOT/etc/systemd/system"
    mkdir -p "$SYSTEMD_DIR"
    # Keep the targets the enabled units order against or pull in: masking one drops the
    # ordering (networkd starts after network-pre.target, which nftables.service precedes)
    for link in "$SYSTEMD_DIR"/*.wants/* "$SYSTEMD_DIR"/*.requires/*; do
        [ -L "$link" ] || continue
        unit=${link##*/}
        case $unit in *@?*.*) unit="${unit%%@*}@.${unit##*.}" ;; esac
        for file in "$BUILDROOT"{/usr/lib,/etc}/systemd/system/"$unit"{,.d/*.conf}; do
            [ -f "$file" ] || continue
            pattern='s/^(After|Before|Wants|Requires|Requisite|BindsTo)=//p'
            for target in $(sed -nE "$pattern" "$file"); do
                case $target in *.target) systemd_svc_whitelist+=("$target") ;; esac
            done
        done
    done
    mkosi-chroot dpkg-query -L systemd |
        grep -E '\.(service|socket|timer|target|mount|path|automount)$' |
        sed 's|.*/||' | while read -r unit; do
        if ! printf '%s\n' "${systemd_svc_whitelist[@]}" | grep -qx "$unit"; then
            ln -sf /dev/null "$SYSTEMD_DIR/$unit"
        fi
    done
    # A kept trigger whose unit is masked can only fail ("Refusing to start, unit ... to
    # trigger not loaded"): mask it too
    mkosi-chroot dpkg-query -L systemd |
        grep -E '/systemd/system/[^/]+\.(socket|timer|path|automount)$' | while read -r path; do
        trigger=${path##*/}
        [ "$(readlink "$SYSTEMD_DIR/$trigger" || true)" != /dev/null ] || continue
        unit=$(sed -nE 's/^(Unit|Service)=//p' "$BUILDROOT$path" | tail -n 1)
        if [ -z "$unit" ]; then
            case $trigger in
                *.automount) unit=${trigger%.*}.mount ;;
                *) unit=${trigger%.*}.service ;;
            esac
        fi
        if [ "$(readlink "$SYSTEMD_DIR/$unit" || true)" = /dev/null ]; then
            ln -sf /dev/null "$SYSTEMD_DIR/$trigger"
        fi
    done
""").strip("\n")
"""Unit masking: every unit of the ``systemd`` package but the keep list (``@KEEP@``) and
the targets the enabled units reference, then every kept trigger (socket, timer, path,
automount) whose unit is masked."""


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
        # modules.dep is still the kernel package's (mkosi runs no depmod when it only keeps
        # modules): its line for a listed module names every module that loads first.
        files=$(find "$kdir" -name '*.ko*' -printf '%P\n')
        for module in @MODULES@; do
            line=$(grep -E "(^|/)${module//[_-]/[_-]}\.ko[^:/]*:" "${kdir}modules.dep" || true)
            for dep in ${line#*:}; do
                if ! grep -qxF "$dep" <<<"$files"; then
                    echo "tundravm: kernel module $module needs $dep, which is not in the image" >&2
                    exit 1
                fi
            done
        done
        # A "pre:" softdep of a kept module names a module or an alias (libcrc32c's crc32c),
        # which mkosi does not follow: one module in the image must provide it.
        while read -r _ owner rest; do
            grep -qxF "${owner//_/-}" <<<"$present" || continue
            pre=${rest#*pre:}
            [ "$pre" != "$rest" ] || continue
            for wanted in ${pre%%post:*}; do
                found=
                for candidate in "$wanted" $(awk -v a="$wanted" '$2 == a {print $3}' \
                    "${kdir}modules.alias"); do
                    if grep -qxF "${candidate//_/-}" <<<"$present"; then found=1; fi
                done
                if [ -z "$found" ]; then
                    echo "tundravm: kernel module $owner loads $wanted first (softdep), and no" \
                        "module in the image provides it: add one to Debian(kernel_modules=...)" >&2
                    exit 1
                fi
            done
        done < <(grep '^softdep ' "${kdir}modules.softdep" 2>/dev/null || true)
        # Index only the kept modules, so modprobe and systemd do not chase pruned ones.
        if [ -x "$BUILDROOT/usr/sbin/depmod" ]; then
            mkosi-chroot depmod -a "$(basename "$kdir")"
        fi
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


class DeterministicMkosiEmitter:
    """Emit the mkosi-native tree: the payload subimage, the QEMU disk and the cloud wrappers.

    ::

        mkosi.conf                 the main image: the QEMU raw disk (the ESP around the UKI)
        mkosi.repart/              its partitions
        mkosi.sandbox/             apt sources and keys for the build, never in the image
        mkosi.images/payload/      Include=../../payload
        mkosi.images/<cloud>/      a disk wrapper per cloud target, converted at postoutput
        payload/                   the OS: mkosi.conf, mkosi.conf.d/ (one file per concern),
                                   trees, scripts and mkosi.profiles/<variant>/ overlays
    """

    def emit(self, *, recipe: RecipeState, destination: Path, config: EmitConfig) -> MkosiEmission:
        destination.mkdir(parents=True, exist_ok=True)
        for name in OWNED_PATHS:
            _remove(destination / name)
        for stale in sorted(destination.iterdir()):
            # a directory of its own mkosi.conf: an earlier per-variant tree, never mkosi state
            if stale.is_dir() and not stale.is_symlink() and (stale / "mkosi.conf").is_file():
                _remove(stale)
        default_name = recipe.default_profile
        payload_state = recipe.effective_profile(default_name)
        self._validate_profile_phases(profile_name=default_name, profile=payload_state)
        overlays: dict[str, ProfileState] = {}
        for name in config.overlays:
            _require_profile(recipe, name)
            overlays[name] = _overlay_state(recipe, name)
            self._validate_profile_phases(profile_name=name, profile=overlays[name])
        every = config.overlays if config.payload_overlays is None else config.payload_overlays
        every = tuple(name for name in every if name in recipe.profiles)
        # The payload boots every profile: it keeps the modules all of them select.
        modules = _module_selection(payload_state, config)
        for name in every:
            modules = modules.union(_module_selection(recipe.effective_profile(name), config))

        if config.generate_version_script:
            _write(destination / "mkosi.version", MKOSI_VERSION_SCRIPT, EXEC_MODE)
        _write(destination / "mkosi.conf", self._render_main(config, recipe), FILE_MODE)
        if config.bootable:
            self._emit_repart(destination / "mkosi.repart", config)
        self._emit_sandbox_tree(destination, payload_state, config, files=config.sandbox_files)
        for name, overlay in overlays.items():
            self._emit_sandbox_tree(
                destination / "mkosi.profiles" / name,
                overlay,
                config,
                files=config.overlay_sandbox_files.get(name, ()),
            )
        _write(
            destination / "mkosi.images" / PAYLOAD / "mkosi.conf",
            f"[Include]\nInclude=../../{PAYLOAD}\n",
            FILE_MODE,
        )
        if config.bootable:
            for target in config.targets:
                self._emit_wrapper(destination / "mkosi.images" / target, target, config)

        payload = destination / PAYLOAD
        own_debloat = tuple(name for name in every if recipe.profiles[name].debloat_explicit)
        # The payload's debloat runs in every profile: it keeps what any of them enables.
        keep_units = sorted(
            {
                unit
                for name in (default_name, *every)
                for unit in debloat_units_keep(recipe.effective_profile(name))
            }
        )
        self._emit_skeleton_tree(payload, payload_state, config)
        self._emit_extra_tree(payload, payload_state)
        if not any(e.path == MACHINE_ID for e in payload_state.files):
            _write(payload / "mkosi.extra" / MACHINE_ID.lstrip("/"), "uninitialized\n", 0o444)
        self._emit_kernel_config(payload, config)
        script_paths: dict[str, dict[Phase, Path]] = {
            default_name: self._emit_all_scripts(
                profile_dir=payload,
                profile=payload_state,
                config=config,
                modules=modules,
                kernel=True,
                accounts=config.reproducible,
                skip_debloat=own_debloat,
                keep_units=keep_units,
            )
        }
        _write(payload / "mkosi.conf", self._render_payload(config, payload_state, modules), 0o644)
        for name, packages in _concern_files(payload_state, config.concerns):
            _write(payload / "mkosi.conf.d" / f"{name}.conf", packages, FILE_MODE)
        if config.root == "verity" and config.bootable:
            self._emit_payload_repart(payload / "mkosi.repart")

        profile_paths = {default_name: payload / "mkosi.conf"}
        for name, overlay in overlays.items():
            profile_dir = payload / "mkosi.profiles" / name
            self._emit_skeleton_tree(profile_dir, overlay, config)
            self._emit_extra_tree(profile_dir, overlay)
            script_paths[name] = self._emit_all_scripts(
                profile_dir=profile_dir,
                profile=overlay,
                config=config,
            )
            conf = profile_dir / "mkosi.conf"
            _write(conf, self._render_overlay(name, overlay), FILE_MODE)
            profile_paths[name] = conf
        return MkosiEmission(
            root=destination, profile_paths=profile_paths, script_paths=script_paths
        )

    # ── the main image and the wrappers ─────────────────────────────────

    def _render_main(self, config: EmitConfig, recipe: RecipeState) -> str:
        """The root ``mkosi.conf``: the settings every image shares and the QEMU disk."""
        distribution, release = _parse_base(config.base)
        root, _ = _split_settings(config.settings)
        sections: dict[str, list[str]] = {
            "Config": [f"MinimumVersion={MKOSI_VERSION}", f"Dependencies={PAYLOAD}"],
            "Distribution": [
                f"Distribution={distribution}",
                *([f"Release={release}"] if release else []),
                f"Architecture={ARCH_TO_MKOSI[config.arch]}",
                *([f"Mirror={config.mirror}"] if config.mirror else []),
                *([f"Snapshot={config.snapshot}"] if config.snapshot else []),
            ],
            "Output": [
                f"Format={'disk' if config.bootable else 'none'}",
                f"Output={MAIN_OUTPUT}",
                f"ImageId={config.image_id}",
                f"OutputDirectory={config.output_directory or DEFAULT_OUTPUT_DIRECTORY}",
                f"Seed={config.seed}",
            ],
            "Build": [
                "ToolsTree=default",
                f"ToolsTreeDistribution={distribution}",
                *([f"ToolsTreeRelease={release}"] if release else []),
                *(
                    [f"ToolsTreeMirror={config.tools_tree_mirror}"]
                    if config.tools_tree_mirror
                    else []
                ),
                *([f"ToolsTreeSnapshot={config.snapshot}"] if config.snapshot else []),
                *(["ToolsTreePackages=qemu-utils"] if config.bootable else []),
                "History=no",
                f"WithNetwork={'yes' if config.with_network else 'no'}",
                *(
                    [f"PackageCacheDirectory={config.package_cache_directory}"]
                    if config.package_cache_directory
                    else []
                ),
                *(f"SandboxTrees={tree}" for tree in config.sandbox_trees),
                *_build_sources(recipe),
            ],
            "Content": [f"SourceDateEpoch={config.epoch}"],
            "Runtime": ["Firmware=uefi"],
        }
        if config.bootable:
            sections["Content"] += ["Bootable=no", *_payload_trees(config)]
        return _render_sections(sections, root)

    def _emit_repart(self, directory: Path, config: EmitConfig) -> None:
        """The partitions of a disk around the payload: the ESP, and with the verity layout
        the payload's root and hash partitions (copied block for block)."""
        _write(directory / "00-esp.conf", ESP_PARTITION, FILE_MODE)
        if config.root != "verity":
            return
        data, hashes = verity_uuids(config.seed)
        for name, kind, source, identifier in (
            ("10-root.conf", "root", VERITY_DATA, data),
            ("11-root-verity.conf", "root-verity", VERITY_HASH, hashes),
        ):
            text = f"[Partition]\nType={kind}\nCopyBlocks={source}\nUUID={identifier}\n"
            _write(directory / name, text, FILE_MODE)

    def _emit_wrapper(self, directory: Path, target: str, config: EmitConfig) -> None:
        """``mkosi.images/<target>/``: the payload in a disk, converted at postoutput."""
        sections = {
            "Config": [f"Dependencies={PAYLOAD}"],
            "Output": ["Format=disk", f"Output={target}", f"Seed={config.seed}"],
            "Content": ["Bootable=no", *_payload_trees(config)],
        }
        _write(directory / "mkosi.conf", _render_sections(sections), FILE_MODE)
        self._emit_repart(directory / "mkosi.repart", config)
        name, script = CLOUD_CONVERTERS[target]
        _write(directory / "mkosi.postoutput.d" / name, script, EXEC_MODE)

    # ── the payload ─────────────────────────────────────────────────────

    def _render_payload(
        self, config: EmitConfig, profile: ProfileState, modules: ModuleSelection
    ) -> str:
        """``payload/mkosi.conf``: the OS image and what no concern file lists."""
        _, local = _split_settings(config.settings)
        verity = config.root == "verity" and config.bootable
        output_format = "uki" if config.bootable and not verity else "disk"
        output = [
            f"Format={output_format}",
            f"Output={PAYLOAD}",
            f"Seed={config.seed}",
            f"ManifestFormat={config.manifest_format}",
        ]
        if config.compress_output:
            output.append(f"CompressOutput={config.compress_output}")
        if verity:
            output.append("SplitArtifacts=uki,partitions")
        content: list[str] = []
        if not config.bootable:
            content.append("Bootable=no")
        elif config.kernel or verity:
            content.append("Bootable=yes")
        if verity:
            content.append("Bootloader=uki")
        content.append(f"CleanPackageMetadata={'yes' if config.clean_package_metadata else 'no'}")
        cmdline = config.kernel.cmdline if config.kernel and config.kernel.cmdline else ""
        if verity:
            data, hashes = verity_uuids(config.seed)
            cmdline = " ".join(
                part
                for part in (
                    cmdline,
                    f"systemd.verity_root_data=/dev/disk/by-partuuid/{data}",
                    f"systemd.verity_root_hash=/dev/disk/by-partuuid/{hashes}",
                    "systemd.volatile=overlay",
                )
                if part
            )
        if cmdline:
            content.append(f"KernelCommandLine={cmdline}")
        content += _list_setting("KernelModules", modules.modules)
        own = [p for p in _image_packages(profile) if (p, "runtime") not in config.concerns]
        builds = [p for p in sorted(profile.build_packages) if (p, "build") not in config.concerns]
        content += _list_setting("Packages", own)
        content += _list_setting("BuildPackages", builds)
        sections: dict[str, list[str]] = {
            "Output": output,
            "Build": [
                f"Environment={k}={config.environment[k]}" for k in sorted(config.environment)
            ],
            "Content": content,
        }
        if verity:
            sections["Validation"] = ["Verity=hash"]
        return _render_sections(sections, local)

    def _emit_payload_repart(self, directory: Path) -> None:
        """The verity payload's root (erofs) and hash partitions, split out as artifacts."""
        _write(directory / "10-root.conf", VERITY_ROOT_PARTITION, FILE_MODE)
        _write(directory / "11-root-verity.conf", VERITY_HASH_PARTITION, FILE_MODE)

    def _render_overlay(self, name: str, overlay: ProfileState) -> str:
        """``payload/mkosi.profiles/<name>/mkosi.conf``: what the profile adds."""
        content = _list_setting("Packages", _image_packages(overlay))
        content += _list_setting("BuildPackages", sorted(overlay.build_packages))
        sections = {"Output": [f"ImageId=%i-{name}"], "Content": content}
        return _render_sections(sections)

    def _emit_sandbox_tree(
        self,
        directory: Path,
        profile: ProfileState,
        config: EmitConfig,
        *,
        files: Sequence[tuple[str, str]] = (),
    ) -> None:
        """``<directory>/mkosi.sandbox/``: *files* and *profile*'s repositories with their
        keyrings, where the build's apt reads them; nothing of it reaches the image."""
        sandbox = directory / "mkosi.sandbox"
        for path, content in files:
            _write(sandbox / path.lstrip("/"), content, FILE_MODE)
        distribution, release = _parse_base(config.base)
        declared = {entry.path: entry for entry in (*profile.skeleton_files, *profile.files)}
        for repo in profile.repositories:
            safe_name = repo.name.lower().replace("/", "-").replace(" ", "-")
            suite = repo.suite or release or distribution
            _write_repository(sandbox, repo, safe_name, suite, types="deb")
            keyring = declared.get(repo.keyring or "")
            if keyring is not None:
                _write_file_entry(sandbox / keyring.path.lstrip("/"), keyring)

    def _validate_profile_phases(self, *, profile_name: str, profile: ProfileState) -> None:
        for phase in profile.phases:
            if phase not in PHASE_DIRECTORIES:
                raise ValidationError(
                    f"mkosi runs no {phase} scripts.",
                    hint=f"Use one of the phases {', '.join(PHASE_DIRECTORIES)}.",
                    context={"phase": str(phase), "profile": profile_name},
                )

    def _emit_extra_tree(self, profile_dir: Path, profile: ProfileState) -> None:
        """Generate mkosi.extra/ with files, templates, and systemd units."""
        extra_dir = profile_dir / "mkosi.extra"
        for entry in profile.files:
            _write_file_entry(extra_dir / entry.path.lstrip("/"), entry)
        for tmpl in profile.templates:
            _write(extra_dir / tmpl.path.lstrip("/"), tmpl.rendered, _parse_mode(tmpl.mode))
        for svc in profile.services:
            # enablement-only registrations and targets ship no unit file
            if not svc.command or svc.name.endswith(".target"):
                continue
            unit_name = svc.name if svc.name.endswith(".service") else f"{svc.name}.service"
            dest = extra_dir / "usr" / "lib" / "systemd" / "system" / unit_name
            _write(dest, _systemd_unit_content(svc), FILE_MODE)

    def _emit_skeleton_tree(
        self, profile_dir: Path, profile: ProfileState, config: EmitConfig
    ) -> None:
        """Generate mkosi.skeleton/ with pre-package-manager files: declared skeleton files,
        the repositories the image keeps (``in_image``) and ``minimal.target`` for a
        systemd debloat. Every repository's build-time source is in a ``mkosi.sandbox/``
        (:meth:`_emit_sandbox_tree`)."""
        skeleton_dir = profile_dir / "mkosi.skeleton"
        for entry in profile.skeleton_files:
            _write_file_entry(skeleton_dir / entry.path.lstrip("/"), entry)
        distribution, release = _parse_base(config.base)
        for repo in profile.repositories:
            if repo.in_image:
                safe_name = repo.name.lower().replace("/", "-").replace(" ", "-")
                suite = repo.suite or release or distribution
                _write_repository(skeleton_dir, repo, safe_name, suite, types="deb deb-src")
        if profile.debloat.enabled and profile.debloat.systemd_minimize:
            target_path = skeleton_dir / "etc" / "systemd" / "system" / "minimal.target"
            if not target_path.exists():
                _write(target_path, MINIMAL_TARGET_UNIT, FILE_MODE)

    def _emit_kernel_config(self, profile_dir: Path, config: EmitConfig) -> None:
        """Write the kernel's config under ``kernel/``: a whole config as ``kernel.config``,
        a composed kernel's ``fragment.config``, local base and local patches."""
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
        profile_dir: Path,
        profile: ProfileState,
        config: EmitConfig,
        modules: ModuleSelection = NO_MODULES,
        kernel: bool = False,
        accounts: bool = False,
        skip_debloat: Sequence[str] = (),
        keep_units: Sequence[str] | None = None,
    ) -> dict[Phase, Path]:
        """Write *profile*'s scripts into ``mkosi.<phase>.d/`` (mkosi finds them there): the
        kernel build (*kernel*), the hooks, and the synthetic postinst and finalize.

        *accounts* ends finalize with :data:`ACCOUNTS_FINALIZE`; *skip_debloat* names the
        profiles that debloat on their own, for which this profile's debloat stands aside;
        *keep_units* replaces the units the systemd debloat keeps (default: the profile's).
        """
        found: dict[Phase, Path] = {}

        def write(phase: Phase, name: str, text: str) -> None:
            path = profile_dir / PHASE_DIRECTORIES[phase] / name
            _write(path, text, EXEC_MODE)
            found.setdefault(phase, path)

        if kernel and config.kernel and config.kernel.built:
            script = _render_kernel_build_script(
                config.kernel,
                pin=config.kernel_pin,
                checkout=config.kernel_checkout,
                distribution=config.kernel_distribution,
                files=config.kernel_files,
            )
            write("build", "10-kernel.sh", script)
        for phase in PHASE_DIRECTORIES:
            commands = list(profile.phases.get(phase, []))
            if phase == "postinst":
                synthetic = self._synthetic_postinst_commands(profile)
                all_commands = synthetic + commands + _unit_state_commands(profile)
                debloats = profile.debloat.enabled and profile.debloat.systemd_minimize
                if all_commands or debloats:
                    text = self._render_postinst_script(
                        all_commands, profile, skip_debloat=skip_debloat, keep_units=keep_units
                    )
                    write(phase, f"50-{phase}.sh", text)
                continue
            if phase == "finalize":
                lines = self._synthetic_finalize_lines(profile, modules, skip_debloat=skip_debloat)
                if lines or commands or accounts:
                    text = self._render_finalize_script(lines, commands, accounts=accounts)
                    write(phase, f"50-{phase}.sh", text)
                continue
            if commands:
                write(phase, f"50-{phase}.sh", self._render_script(commands))
        return {phase: found[phase] for phase in PHASE_ORDER if phase in found}

    def _synthetic_postinst_commands(self, profile: ProfileState) -> list[CommandSpec]:
        """Create synthetic commands for user creation and service enablement."""
        commands: list[CommandSpec] = []
        for group in profile.groups:
            commands.append(CommandSpec(argv=(_groupadd_command(group),)))
        for user in profile.users:
            commands.append(CommandSpec(argv=(_useradd_command(user),)))
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
        self,
        profile: ProfileState,
        modules: ModuleSelection = NO_MODULES,
        *,
        skip_debloat: Sequence[str] = (),
    ) -> list[str]:
        """The kernel module check and the debloat lines of the finalize script."""
        config = profile.debloat
        lines = _kernel_module_check(modules.modules)
        if not config.enabled:
            return lines
        debloat: list[str] = []
        if config.clean_var_dirs:
            debloat.append("# Debloat: clean files in var directories")
            for var_dir in sorted(config.clean_var_dirs):
                debloat.append(f'find "$BUILDROOT{var_dir}" -type f -delete')
        paths = debloat_paths_remove(config, modules, declared_paths(profile))
        if paths:
            debloat += ["", "# Debloat: remove unnecessary paths"]
            debloat += [f"rm -rf {_globbed_path(path)}" for path in sorted(paths)]
        conditional = config.profile_conditional_paths
        if conditional:
            debloat += ["", "# Debloat: profile-conditional path removal"]
            for profile_name in sorted(conditional):
                for path in conditional[profile_name]:
                    debloat.append(f'if [[ " ${{PROFILES:-}} " != *" {profile_name} "* ]]; then')
                    debloat.append(f"    rm -rf {_globbed_path(path)}")
                    debloat.append("fi")
        debloat = _unless_profiles(debloat, skip_debloat)
        if lines and debloat:
            lines.append("")
        return lines + debloat

    def _render_postinst_script(
        self,
        commands: list[CommandSpec],
        profile: ProfileState,
        *,
        skip_debloat: Sequence[str] = (),
        keep_units: Sequence[str] | None = None,
    ) -> str:
        """Render postinst script with user creation, service enablement, and debloat."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for command in commands:
            lines.append(self._render_command_line(command))
        config = profile.debloat
        if config.enabled and config.systemd_minimize:
            bins_keep = sorted(config.systemd_bins_keep)
            units_keep = debloat_units_keep(profile) if keep_units is None else keep_units
            debloat = ["# Debloat: remove unwanted systemd binaries"]
            bins_keep_list = " ".join(f'"{b}"' for b in bins_keep)
            debloat.append(f"systemd_bin_whitelist=({bins_keep_list})")
            debloat.append(
                "mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/' | "
                "while read -r bin_path; do"
            )
            debloat.append('    bin_name=$(basename "$bin_path")')
            debloat.append(
                "    if ! printf '%s\\n' \"${systemd_bin_whitelist[@]}\" | "
                'grep -qx "$bin_name"; then'
            )
            debloat.append('        rm -f "$BUILDROOT$bin_path"')
            debloat.append("    fi")
            debloat.append("done")
            debloat.append("")
            keep_list = " ".join(f'"{u}"' for u in units_keep)
            debloat.extend(_DEBLOAT_MASK.replace("@KEEP@", keep_list).split("\n"))
            debloat.append("")
            debloat.append("# Set default systemd target")
            debloat.append('ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"')
            lines.append("")
            lines.extend(_unless_profiles(debloat, skip_debloat))
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


def _unless_profiles(lines: list[str], profiles: Sequence[str]) -> list[str]:
    """*lines*, run only when none of *profiles* is selected (they debloat on their own)."""
    if not lines or not profiles:
        return lines
    test = " && ".join(f'[[ " ${{PROFILES:-}} " != *" {name} "* ]]' for name in profiles)
    body = [f"    {line}" if line else "" for line in lines]
    return [f"if {test}; then", *body, "fi"]


def _remove(path: Path) -> None:
    """Remove *path*, a file, symlink or directory, if it exists."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _list_setting(key: str, values: Sequence[str]) -> list[str]:
    """``key=`` followed by one indented value per line; nothing for no values."""
    if not values:
        return []
    return [f"{key}=", *(f"    {value}" for value in values)]


def _render_sections(
    sections: Mapping[str, Sequence[str]],
    extra: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """INI text of *sections* in order, each followed by its *extra* verbatim lines, then the
    sections only *extra* has; empty sections are left out."""
    merged: dict[str, list[str]] = {name: list(lines) for name, lines in sections.items()}
    for name, lines in (extra or {}).items():
        merged.setdefault(name, []).extend(lines)
    blocks = ["\n".join([f"[{name}]", *lines]) for name, lines in merged.items() if lines]
    return "\n\n".join(blocks) + "\n"


def _split_settings(
    settings: Sequence[tuple[str, str, tuple[str, ...]]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Verbatim settings as ``{section: lines}``: those mkosi reads only in the main image
    (:data:`MAIN_IMAGE_SETTINGS`), and the payload's own."""
    root: dict[str, list[str]] = {}
    local: dict[str, list[str]] = {}
    for section, key, values in settings:
        target = root if key in MAIN_IMAGE_SETTINGS else local
        target.setdefault(section, []).extend(f"{key}={value}" for value in values)
    return root, local


def _payload_trees(config: EmitConfig) -> list[str]:
    """``ExtraTrees=`` of a disk around the payload: the UKI on the ESP path, and with the
    verity layout the root and hash partitions the repart definitions copy."""
    trees = [f"ExtraTrees=%O/{PAYLOAD}.efi:{ESP_TARGET}"]
    if config.root == "verity":
        trees += [
            f"ExtraTrees=%O/{PAYLOAD}.root.raw:{VERITY_DATA}",
            f"ExtraTrees=%O/{PAYLOAD}.root-verity.raw:{VERITY_HASH}",
        ]
    return trees


def _build_sources(recipe: RecipeState) -> list[str]:
    """``BuildSources=``: the tree itself (mkosi's default, which any entry replaces), then
    every profile's declared host directories (mounts are main-image settings)."""
    found: dict[str, None] = {}
    for name in sorted(recipe.profiles):
        for host, target in recipe.profiles[name].build_sources:
            found[f"{host}:{target}" if target else host] = None
    if not found:
        return []
    return ["BuildSources=.", *(f"BuildSources={entry}" for entry in found)]


def _concern_files(
    profile: ProfileState, concerns: Mapping[tuple[str, str], str]
) -> list[tuple[str, str]]:
    """``(file stem, text)`` of each ``payload/mkosi.conf.d`` file: the packages of one
    concern, the files numbered in the order the concerns first appear, the cloud glue last."""
    order: list[str] = []
    for concern in concerns.values():
        if concern not in order:
            order.append(concern)
    order.sort(key=lambda name: name == CLOUD_GLUE)
    runtime = set(_image_packages(profile))
    width = len(str(10 * len(order)))
    files: list[tuple[str, str]] = []
    for index, concern in enumerate(order, start=1):
        packages = sorted(
            n for (n, role), c in concerns.items() if c == concern and role == "runtime"
        )
        builds = sorted(n for (n, role), c in concerns.items() if c == concern and role == "build")
        packages = [p for p in packages if p in runtime]
        builds = [b for b in builds if b in profile.build_packages]
        content = _list_setting("Packages", packages) + _list_setting("BuildPackages", builds)
        if content:
            label = re.sub(r"[^A-Za-z0-9._-]+", "-", concern).strip("-") or "packages"
            stem = f"{10 * index:0{width}d}-{label}"
            files.append((stem, _render_sections({"Content": content})))
    return files


def verity_uuids(seed: str) -> tuple[str, str]:
    """The partition UUIDs of the verity root's data and hash partitions under *seed*: fixed,
    so every disk copying them and the UKI's ``systemd.verity_root_*=`` agree."""
    base = uuid.UUID(seed)
    return str(uuid.uuid5(base, "root")), str(uuid.uuid5(base, "root-verity"))


def _require_profile(recipe: RecipeState, name: str) -> None:
    if name not in recipe.profiles:
        raise ValidationError(
            "Profile does not exist for mkosi emission.",
            hint="Create the profile before calling compile().",
            context={"profile": name, "operation": "emit_mkosi"},
        )


def _module_selection(profile: ProfileState, config: EmitConfig) -> ModuleSelection:
    """*profile*'s kernel modules under *config* (a built kernel selects none, and keeps
    its own when it builds loadable ones)."""
    kernel = config.kernel
    return module_selection(
        profile,
        config.kernel_modules,
        built_kernel=kernel is not None,
        loadable=kernel is not None and kernel.composed and kernel.modules,
    )


def _overlay_state(recipe: RecipeState, name: str) -> ProfileState:
    """What ``payload/mkosi.profiles/<name>/`` adds on top of the payload.

    Entries the payload already ships, its output targets and its debloat are left out
    so nothing runs twice; a profile with its own debloat keeps it, and the payload's
    stands aside for it.
    """
    own = recipe.profiles[name]
    default = recipe.effective_profile(recipe.default_profile)
    return replace(
        own,
        files=[f for f in own.files if f not in default.files],
        services=[s for s in own.services if s not in default.services],
        groups=[g for g in own.groups if g not in default.groups],
        unit_states=[u for u in own.unit_states if u not in default.unit_states],
        output_targets=(),
        debloat=own.debloat if own.debloat_explicit else DebloatConfig(enabled=False),
    )


def emit_mkosi_tree(*, recipe: RecipeState, destination: Path, config: EmitConfig) -> MkosiEmission:
    """Emit *recipe* (the payload and :attr:`EmitConfig.overlays`) as the tree at *destination*."""
    return DeterministicMkosiEmitter().emit(recipe=recipe, destination=destination, config=config)
