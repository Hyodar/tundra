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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from tundravm.models import MODULES_PATH, DebloatConfig, ProfileState

MODULE_SETS_VERSION = 2
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

TARGET_MODULES: dict[str, tuple[str, ...]] = {
    "qemu": (),
    "azure": AZURE_MODULES,
    "gcp": GCP_MODULES,
}
"""Output target -> what it adds to :data:`TDX_GUEST_MODULES`."""

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
    targets: Iterable[str], *, disks: bool = False, firewall: bool = False
) -> tuple[str, ...]:
    """The curated set for an image booting on every one of *targets* (sorted, unique)."""
    selected = set(TDX_GUEST_MODULES)
    for target in targets:
        selected.update(TARGET_MODULES.get(target, ()))
    if disks:
        selected.update(DISK_MODULES)
    if firewall:
        selected.update(FIREWALL_MODULES)
    return tuple(sorted(selected))


def module_selection(
    profile: ProfileState, spec: KernelModules, *, built_kernel: bool
) -> ModuleSelection:
    """*profile*'s selection under ``Debian(kernel_modules=spec)``.

    A built ``Kernel`` (*built_kernel*) installs only its ``vmlinuz`` and a variant without a
    distribution kernel package has no modules: both select nothing.
    """
    if built_kernel or not installs_kernel_package(profile.packages):
        return NO_MODULES
    if spec == "all":
        return ModuleSelection(modular=True)
    disks = any(entry.name == _DISK_STEP for entry in profile.init_scripts)
    firewall = any(re.split(r"[/=]", p, maxsplit=1)[0] == "nftables" for p in profile.packages)
    curated = curated_modules(profile.output_targets, disks=disks, firewall=firewall)
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
    "DISK_MODULES",
    "FIREWALL_MODULES",
    "GCP_MODULES",
    "MODULE_NAME",
    "MODULE_SETS_VERSION",
    "NO_MODULES",
    "TARGET_MODULES",
    "TDX_GUEST_MODULES",
    "KernelModules",
    "ModuleSelection",
    "curated_modules",
    "debloat_paths_remove",
    "installs_kernel_package",
    "module_selection",
    "removes_path",
]
