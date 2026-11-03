"""Curated kernel module sets: the drivers a TDX guest's modular distribution kernel keeps.

A tundravm image is a UKI whose initrd is the whole root file system, so every module under
``/usr/lib/modules`` is memory and attack surface the host can reach through a device it
offers. ``Debian(kernel_modules=...)`` therefore keeps only the drivers its outputs boot on:
mkosi's ``KernelModules=`` keeps the listed modules and the modules and firmware they depend
on (mkosi follows modinfo ``depends`` and ``softdep``) and deletes the rest before the
finalize scripts run, which then check that every listed module is there.

The sets are checked against Debian trixie's ``linux-image-amd64`` (6.12: its config and
``modinfo``) and mirror the drivers flashbots-images builds into its module-less kernel
(``shared/kernel/config.d/01-sane-defaults``, ``mkosi.profiles/gcp/kernel/config.d/20-gcp``).
Built-in drivers (``virtio_console``, the TPM's ``tpm_tis``/``tpm_crb``) need no entry.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from tundravm.errors import ValidationError
from tundravm.models import MODULES_PATH, DebloatConfig, ProfileState

MODULE_SETS_VERSION = 4
"""Bumped whenever a curated set changes; the generated finalize check names it."""

TDX_GUEST_MODULES: tuple[str, ...] = (
    # /dev/tdx_guest and the configfs-tsm quote interface Tdxs reads (pulls tsm, configfs).
    "tdx_guest",
    # QEMU/KVM's NIC, and GCP's on machine series without gVNIC (pulls net_failover).
    "virtio_net",
    # QEMU's virtio disks (/dev/vd*).
    "virtio_blk",
    # QEMU's virtio-scsi disks, GCP's SCSI persistent disks (pulls scsi_mod).
    "virtio_scsi",
    # SCSI disks (/dev/sd*), which Disk(device=None) picks; Debian builds it as a module.
    "sd_mod",
    # NVMe disks: QEMU's nvme device, GCP C3 (its TDX series), Azure's v6 sizes.
    "nvme",
    # /sys/firmware/efi/efivars: systemd reads the UKI stub's loader variables there.
    "efivarfs",
    # /sys/kernel/config, which systemd mounts at boot (sys-kernel-config.mount, in the
    # verity initrd too) and configfs-tsm lives in; tdx_guest holds it only inside a TD.
    "configfs",
    # The ACPI power button a cloud stop and QEMU's system_powerdown press (acpid's events).
    "button",
)
"""Every TDX guest, whatever it boots on (the ``qemu`` output's whole set)."""

AZURE_MODULES: tuple[str, ...] = (
    # VMBus: every Hyper-V synthetic device hangs off it.
    "hv_vmbus",
    # The synthetic NIC.
    "hv_netvsc",
    # Synthetic SCSI: the OS and data disks (/dev/sd*).
    "hv_storvsc",
    # KVP, heartbeat, time sync and the host's graceful shutdown request.
    "hv_utils",
    # PCI pass-through: accelerated-networking VFs and NVMe disks reach the guest through it.
    "pci_hyperv",
    # Microsoft Azure Network Adapter, the accelerated NIC; without it traffic stays on netvsc.
    "mana",
)
"""Azure (Hyper-V) confidential VMs, on top of :data:`TDX_GUEST_MODULES`."""

GCP_MODULES: tuple[str, ...] = (
    # gVNIC: Confidential VMs with TDX (C3) require it as their NIC.
    "gve",
    # C3 persistent and local SSD disks are NVMe.
    "nvme",
    # SCSI persistent disks on older machine series.
    "virtio_scsi",
)
"""Google Cloud confidential VMs, on top of :data:`TDX_GUEST_MODULES`."""

DISK_MODULES: tuple[str, ...] = (
    # The LUKS mapping of Disk(key=...) (pulls dm_mod).
    "dm_crypt",
    # aes-xts-plain64, cryptsetup's LUKS2 default: requested by name, no modinfo dependency.
    "xts",
    # The AES-NI implementation of that cipher.
    "aesni_intel",
    # cryptsetup decrypts LUKS keyslots through the kernel crypto API (AF_ALG).
    "algif_skcipher",
    # The file system the disk tools format (pulls jbd2, mbcache).
    "ext4",
    # ext4's metadata checksums: its softdep names the alias crypto-crc32c, which mkosi skips.
    "crc32c_intel",
)
"""A variant that declares a ``Disk``, on top of its outputs' sets."""

FIREWALL_MODULES: tuple[str, ...] = (
    # nftables (pulls nfnetlink); the inet family, filter chains and sets are built into it.
    "nf_tables",
    # The firewall's "ct state established,related" (pulls nf_conntrack, nf_defrag_ipv4/6).
    "nft_ct",
    # Firewall(default="reject"): TCP resets and ICMP unreachables (pulls nf_reject_ipv4/6).
    "nft_reject_inet",
    # nf_tables and nf_conntrack pull libcrc32c, whose init allocates the "crc32c" cipher
    # and fails without an implementation: its softdep names the alias crc32c, which mkosi
    # skips (the first boots' nftables.service failed on it). The generic one runs anywhere.
    "crc32c_generic",
)
"""A variant that installs ``nftables`` (a ``Firewall``), on top of its outputs' sets."""

VERITY_MODULES: tuple[str, ...] = (
    # The initrd opens the root through dm-verity (pulls dm_mod, dm_bufio).
    "dm_verity",
    # The root file system.
    "erofs",
    # systemd.volatile=overlay: the initrd stacks a tmpfs over the read-only root.
    "overlay",
    # erofs pulls libcrc32c, whose init fails without a crc32c implementation: its softdep
    # names the alias, which mkosi skips (as for nftables in the firewall set).
    "crc32c_generic",
)
"""What the verity layout's initrd mounts the root with: Debian builds each as a module, and
mkosi's kernel-modules initrd holds only the ``KernelModules=`` the payload keeps."""

CONTAINER_MODULES: tuple[str, ...] = (
    # A container's root image is a file the unit's RootImage= attaches to a loop device.
    "loop",
    # Its root partition (pulls libcrc32c, hence the generic crc32c as for the verity root).
    "erofs",
    "crc32c_generic",
)
"""A variant that ships a ``Container``, on top of its outputs' sets; a built kernel needs
each built in."""

CONTAINER_NETWORK_MODULES: tuple[str, ...] = (
    # A container's veth, and the one the probe sends a datagram into it through.
    "veth",
    # The firewall's prerouting and postrouting nat chains (pull nf_nat).
    "nft_chain_nat",
    # Their dnat to the container's address and masquerade of what it sends.
    "nft_nat",
    "nft_masq",
)
"""A variant whose ``Container`` has a veth (``expose=``, ``egress=``, ``serves=``), on top of
:data:`CONTAINER_MODULES`; ``Container.kernel()`` asks a built kernel for each."""

CONTAINER_NETWORK_PATH = "/etc/systemd/network/10-tundravm-ve-"
"""The prefix of the networkd file a container's veth ships, which selects
:data:`CONTAINER_NETWORK_MODULES`."""

TARGET_MODULES: dict[str, tuple[str, ...]] = {
    "qemu": (),
    "azure": AZURE_MODULES,
    "gcp": GCP_MODULES,
}
"""Output target -> what it adds to :data:`TDX_GUEST_MODULES`."""

MODULE_OPTIONS: dict[str, str] = {
    "tdx_guest": "CONFIG_TDX_GUEST_DRIVER",
    "virtio_net": "CONFIG_VIRTIO_NET",
    "virtio_blk": "CONFIG_VIRTIO_BLK",
    "virtio_scsi": "CONFIG_SCSI_VIRTIO",
    "sd_mod": "CONFIG_BLK_DEV_SD",
    "nvme": "CONFIG_BLK_DEV_NVME",
    "efivarfs": "CONFIG_EFIVAR_FS",
    "configfs": "CONFIG_CONFIGFS_FS",
    "button": "CONFIG_ACPI_BUTTON",
    "hv_vmbus": "CONFIG_HYPERV",
    "hv_netvsc": "CONFIG_HYPERV_NET",
    "hv_storvsc": "CONFIG_HYPERV_STORAGE",
    "hv_utils": "CONFIG_HYPERV_UTILS",
    "pci_hyperv": "CONFIG_PCI_HYPERV",
    "mana": "CONFIG_MICROSOFT_MANA",
    "gve": "CONFIG_GVE",
    "dm_crypt": "CONFIG_DM_CRYPT",
    "xts": "CONFIG_CRYPTO_XTS",
    "aesni_intel": "CONFIG_CRYPTO_AES_NI_INTEL",
    "algif_skcipher": "CONFIG_CRYPTO_USER_API_SKCIPHER",
    "ext4": "CONFIG_EXT4_FS",
    "crc32c_intel": "CONFIG_CRYPTO_CRC32C_INTEL",
    "nf_tables": "CONFIG_NF_TABLES",
    "nft_ct": "CONFIG_NFT_CT",
    "nft_reject_inet": "CONFIG_NFT_REJECT_INET",
    "crc32c_generic": "CONFIG_CRYPTO_CRC32C",
    "dm_verity": "CONFIG_DM_VERITY",
    "erofs": "CONFIG_EROFS_FS",
    "overlay": "CONFIG_OVERLAY_FS",
    "loop": "CONFIG_BLK_DEV_LOOP",
    "veth": "CONFIG_VETH",
    "nft_chain_nat": "CONFIG_NFT_NAT",
    "nft_nat": "CONFIG_NFT_NAT",
    "nft_masq": "CONFIG_NFT_MASQ",
}
"""Each curated module and the ``CONFIG_*`` option that builds it: a built kernel loads no
module, so it needs every one ``=y``.

The source of truth is Debian trixie's ``linux`` 6.12 source, the ``obj-$(CONFIG_...) +=
<module>.o`` line of the Makefile that builds each module, checked against
``config.amd64_none_cloud-amd64`` of ``linux-config-6.12`` (6.12.57-1), which sets every one
(``=m``, or ``=y`` for ``nvme``, ``ext4`` and ``crc32c_generic``)."""

GUEST_OPTIONS: tuple[str, ...] = (
    # The TDX guest support every module of the set stands on.
    "CONFIG_INTEL_TDX_GUEST",
    # configfs-tsm, the quote interface (the tsm module tdx_guest pulls).
    "CONFIG_TSM_REPORTS",
    # The virtio PCI transport the virtio drivers bind through (built in by Debian).
    "CONFIG_VIRTIO_PCI",
)
"""Options of every TDX guest that are no curated module: Debian builds them in, or a
module of :data:`TDX_GUEST_MODULES` pulls them."""

DENIED_DRIVERS: tuple[str, ...] = (
    # A PCIe SSD driver no target offers, reachable by a device the host emulates.
    "CONFIG_BLK_DEV_PCIESSD_MTIP32XX",
)
"""Drivers a built kernel must not carry: no target offers the device, so only a hostile
host could, and ``Kernel.build(hardening=True)`` turns them off too."""

_FEATURE_MODULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("an encrypted Volume", DISK_MODULES),
    ("the Firewall", FIREWALL_MODULES),
    ("RootFS.verity()", VERITY_MODULES),
    ("a Container", CONTAINER_MODULES),
)


@dataclass(frozen=True, slots=True)
class CloudDrivers:
    """The options a built kernel needs to boot on every one of *targets*, as a check.

    A built kernel loads no module, so every driver the curated sets keep for a modular
    kernel must be built in: :attr:`required` are ``(option, who needs it)``, the
    :data:`MODULE_OPTIONS` of :data:`TDX_GUEST_MODULES` and :data:`GUEST_OPTIONS` (every
    target), of each target's :data:`TARGET_MODULES` and of the features an image may
    switch on (:data:`DISK_MODULES`, :data:`FIREWALL_MODULES`, :data:`VERITY_MODULES`,
    :data:`CONTAINER_MODULES`).
    :attr:`denied` are :data:`DENIED_DRIVERS`. The kernel build asserts both after
    ``olddefconfig``: it requests none of them, so a miss names what the config must say.
    """

    targets: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(self.targets))
        unknown = [target for target in self.targets if target not in TARGET_MODULES]
        if unknown:
            raise ValidationError(
                f"CloudDrivers: {', '.join(unknown)} is not a target.",
                hint=f"Name targets among {', '.join(TARGET_MODULES)}.",
            )

    @property
    def required(self) -> tuple[tuple[str, str], ...]:
        """Each required option (sorted) and who needs it, comma-separated."""
        needs: dict[str, list[str]] = {}

        def add(options: Iterable[str], who: str) -> None:
            for option in options:
                found = needs.setdefault(option, [])
                if who not in found:
                    found.append(who)

        for target in self.targets:
            add(GUEST_OPTIONS, target)
            add((MODULE_OPTIONS[m] for m in TDX_GUEST_MODULES), target)
            add((MODULE_OPTIONS[m] for m in TARGET_MODULES[target]), target)
        for who, modules in _FEATURE_MODULES:
            add((MODULE_OPTIONS[m] for m in modules), who)
        return tuple((option, ", ".join(needs[option])) for option in sorted(needs))

    @property
    def denied(self) -> tuple[str, ...]:
        """The options that must stay unset."""
        return DENIED_DRIVERS


KernelModules = tuple[str, ...] | Literal["all"]
"""``Debian(kernel_modules=...)``: ``"all"`` keeps every module, a tuple adds to the sets."""

MODULE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
"""A module name as ``modprobe`` takes it (``-`` and ``_`` are the same character)."""

_KERNEL_METAPACKAGES = frozenset({"linux-generic", "linux-kvm", "linux-virtual"})
_DISK_STEP = "disks"
"""The runtime-init step a ``Disk`` declares."""


@dataclass(frozen=True, slots=True)
class ModuleSelection:
    """What a variant keeps of ``/usr/lib/modules``.

    *modular*: the variant boots a modular distribution kernel, so the implicit debloat keeps
    ``/usr/lib/modules``. *modules*: the ``KernelModules=`` list mkosi keeps (with their
    dependencies) and the finalize checks; empty keeps every module (``"all"``) or, without
    a modular kernel, selects nothing.
    """

    modular: bool = False
    modules: tuple[str, ...] = ()

    def union(self, other: ModuleSelection) -> ModuleSelection:
        """Both selections at once (one tree that boots both variants' outputs)."""
        if self.modular and other.modular and not (self.modules and other.modules):
            return ModuleSelection(modular=True)
        return ModuleSelection(
            modular=self.modular or other.modular,
            modules=tuple(sorted({*self.modules, *other.modules})),
        )


NO_MODULES = ModuleSelection()
"""No modular kernel: nothing selected, nothing kept."""


def installs_kernel_package(packages: Iterable[str]) -> bool:
    """Whether *packages* install a distribution kernel (``linux-image-*`` or a metapackage)."""
    for package in packages:
        name = re.split(r"[/=]", package, maxsplit=1)[0]
        if name == "linux-image" or name.startswith("linux-image-"):
            return True
        if name in _KERNEL_METAPACKAGES:
            return True
    return False


def curated_modules(
    targets: Iterable[str],
    *,
    disks: bool = False,
    firewall: bool = False,
    containers: bool = False,
    container_network: bool = False,
) -> tuple[str, ...]:
    """The curated set for an image booting on every one of *targets* (sorted, unique)."""
    selected = set(TDX_GUEST_MODULES)
    for target in targets:
        selected.update(TARGET_MODULES.get(target, ()))
    if disks:
        selected.update(DISK_MODULES)
    if firewall:
        selected.update(FIREWALL_MODULES)
    if containers:
        selected.update(CONTAINER_MODULES)
    if container_network:
        selected.update(CONTAINER_NETWORK_MODULES)
    return tuple(sorted(selected))


def module_selection(
    profile: ProfileState, spec: KernelModules, *, built_kernel: bool, loadable: bool = False
) -> ModuleSelection:
    """*profile*'s selection under ``Debian(kernel_modules=spec)``.

    A built ``Kernel`` (*built_kernel*) installs only its ``vmlinuz`` and a variant without a
    distribution kernel package has no modules: both select nothing. A built kernel that
    installs its own loadable modules (*loadable*: ``Kernel(modules=True)``) keeps all of
    ``/usr/lib/modules`` instead, so the implicit debloat leaves them.
    """
    if built_kernel:
        return ModuleSelection(modular=True) if loadable else NO_MODULES
    if not installs_kernel_package(profile.packages):
        return NO_MODULES
    if spec == "all":
        return ModuleSelection(modular=True)
    disks = any(entry.name == _DISK_STEP for entry in profile.init_scripts)
    firewall = any(re.split(r"[/=]", p, maxsplit=1)[0] == "nftables" for p in profile.packages)
    curated = curated_modules(
        profile.output_targets,
        disks=disks,
        firewall=firewall,
        containers=bool(profile.containers),
        container_network=any(f.path.startswith(CONTAINER_NETWORK_PATH) for f in profile.files),
    )
    return ModuleSelection(modular=True, modules=tuple(sorted({*curated, *spec})))


def removes_path(pattern: str, path: str) -> bool:
    """Whether debloat removal entry *pattern* (a path or a glob) deletes *path*."""
    return path == pattern or path.startswith(pattern + "/") or fnmatch.fnmatch(path, pattern)


def debloat_paths_remove(
    config: DebloatConfig, selection: ModuleSelection, declared: Iterable[str] = ()
) -> tuple[str, ...]:
    """The paths *config* removes. The implicit default keeps a modular kernel's modules
    (mkosi already pruned them to the selection) and every entry that would delete one of the
    *declared* file paths (``/etc/systemd/network`` under ``Network``). A declared ``Debloat``
    removes its list (lint: ``debloat-removes-declared-file``)."""
    paths = config.effective_paths_remove
    if config.declared:
        return paths
    placed = tuple(declared)
    return tuple(
        path
        for path in paths
        if not (selection.modular and path == MODULES_PATH)
        and not any(removes_path(path, file) for file in placed)
    )


__all__ = [
    "AZURE_MODULES",
    "DENIED_DRIVERS",
    "DISK_MODULES",
    "FIREWALL_MODULES",
    "GCP_MODULES",
    "GUEST_OPTIONS",
    "MODULE_NAME",
    "MODULE_OPTIONS",
    "MODULE_SETS_VERSION",
    "NO_MODULES",
    "TARGET_MODULES",
    "TDX_GUEST_MODULES",
    "VERITY_MODULES",
    "CONTAINER_MODULES",
    "CONTAINER_NETWORK_MODULES",
    "CONTAINER_NETWORK_PATH",
    "CloudDrivers",
    "KernelModules",
    "ModuleSelection",
    "curated_modules",
    "debloat_paths_remove",
    "installs_kernel_package",
    "module_selection",
    "removes_path",
]
