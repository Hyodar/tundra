"""``KernelHardening()``: a reviewed set of kernel options for a composed ``Kernel``.

Re-exported from :mod:`tundravm.declarative.utils`.
"""

from __future__ import annotations

from collections.abc import Sequence

from tundravm.errors import ValidationError

from ._component import Composite, component
from .model import KernelConfig

ATTACK_SURFACE: tuple[str, ...] = tuple(
    f"CONFIG_{name}"
    for name in (
        "AF_KCM BLK_DEV_RBD BONDING BTRFS_FS CEPH_FS CIFS DUMMY EQUALIZER FS_DAX "
        "FS_ENCRYPTION FS_VERITY FUSE_FS HIBERNATION IA32_EMULATION INET6_AH INET6_ESP "
        "INET6_IPCOMP INET_AH INET_ESP INET_IPCOMP INFINIBAND IO_URING IO_URING_ZCRX "
        "IPV6_ILA IPV6_MIP6 IPV6_MROUTE IPV6_MULTIPLE_TABLES IPV6_SEG6_HMAC "
        "IPV6_SEG6_LWTUNNEL IPV6_SIT IPV6_TUNNEL IPV6_VTI IP_ADVANCED_ROUTER IP_MROUTE "
        "IP_SCTP ISO9660_FS KVM_XEN L2TP NET_DEVMEM NET_FOU NET_IPGRE_DEMUX NET_IPIP "
        "NET_IPVTI NET_KEY NFSD NFS_FS NTFS3_FS RDS SMB_SERVER SURFACE_PLATFORMS SUSPEND "
        "TCP_CONG_BIC TCP_CONG_CDG TCP_CONG_DCTCP TCP_CONG_HSTCP TCP_CONG_HTCP "
        "TCP_CONG_HYBLA TCP_CONG_ILLINOIS TCP_CONG_LP TCP_CONG_NV TCP_CONG_SCALABLE "
        "TCP_CONG_VEGAS TCP_CONG_VENO TCP_CONG_WESTWOOD TCP_CONG_YEAH TCP_MD5SIG TIPC "
        "UDF_FS USERFAULTFD VDPA VHOST VHOST_NET VHOST_SCSI VHOST_VDPA VHOST_VSOCK "
        "X86_ACPI_CPUFREQ_CPB X86_P4_CLOCKMOD X86_POWERNOW_K8 X86_SPEEDSTEP_CENTRINO "
        "XDP_SOCKETS XEN XFRM XFRM_ALGO XFRM_USER ZONEFS_FS "
        # flashbots-images modules/flashbox/common/kernel/config.d/10-bob: AF_ALG
        # beyond what cryptsetup needs, authencesn (CVE-2026-31431) and unused block devices
        "CRYPTO_USER_API_RNG CRYPTO_USER_API_AEAD CRYPTO_AUTHENCESN BLK_DEV_DRBD "
        "BLK_DEV_NBD BLK_DEV_NULL_BLK BLK_DEV_UBLK NET_SCH_TEQL ZRAM"
    ).split()
)
"""Subsystems a confidential VM does not need, turned off: flashbots-images'
deny list (``shared/kernel/config.d/01-sane-defaults:135-219``, ``02-disable-unneeded``
and ``modules/flashbox/common/kernel/config.d/10-bob:39-65``)."""

HARDENING: tuple[tuple[str, str, str], ...] = (
    ("CONFIG_SECURITY_LOCKDOWN_LSM", "y", "lockdown: root cannot read or patch kernel memory"),
    ("CONFIG_SECURITY_LOCKDOWN_LSM_EARLY", "y", "lockdown is active before userspace runs"),
    ("CONFIG_LOCK_DOWN_KERNEL_FORCE_CONFIDENTIALITY", "y", "lockdown in its strictest mode"),
    ("CONFIG_INIT_ON_ALLOC_DEFAULT_ON", "y", "zero pages and slabs on allocation (no leaks)"),
    ("CONFIG_INIT_ON_FREE_DEFAULT_ON", "y", "zero memory on free (no stale secrets)"),
    ("CONFIG_RANDOMIZE_KSTACK_OFFSET", "y", "allow a per-syscall kernel stack offset"),
    ("CONFIG_RANDOMIZE_KSTACK_OFFSET_DEFAULT", "y", "randomize it on every syscall"),
    ("CONFIG_DEVMEM", "n", "no /dev/mem: physical memory is not a file"),
    ("CONFIG_PROC_KCORE", "n", "no /proc/kcore: no live image of kernel memory"),
    ("CONFIG_KEXEC", "n", "no kexec: the measured kernel cannot be swapped at runtime"),
    ("CONFIG_KEXEC_FILE", "n", "no kexec_file_load either"),
    ("CONFIG_BPF_UNPRIV_DEFAULT_OFF", "y", "unprivileged BPF is off"),
    ("CONFIG_BPF_JIT_ALWAYS_ON", "y", "no BPF interpreter to abuse as a Spectre gadget"),
    ("CONFIG_LEGACY_TIOCSTI", "n", "no TIOCSTI keystroke injection into terminals"),
    ("CONFIG_LEGACY_VSYSCALL_NONE", "y", "no fixed-address vsyscall page (ROP target)"),
    ("CONFIG_MODIFY_LDT_SYSCALL", "n", "no modify_ldt: 16-bit segment attack surface"),
    ("CONFIG_X86_IOPL_IOPERM", "n", "no userspace port I/O"),
    ("CONFIG_MAGIC_SYSRQ", "n", "no SysRq: a console cannot dump state or reboot"),
    ("CONFIG_COMPAT_BRK", "n", "randomize the heap start"),
    ("CONFIG_HARDENED_USERCOPY", "y", "bounds-check copies to and from userspace"),
    ("CONFIG_FORTIFY_SOURCE", "y", "compile-time and runtime buffer overflow checks"),
    ("CONFIG_STACKPROTECTOR_STRONG", "y", "stack canaries in every function with arrays"),
    ("CONFIG_SLAB_FREELIST_RANDOM", "y", "randomize slab freelists"),
    ("CONFIG_SLAB_FREELIST_HARDENED", "y", "detect slab freelist corruption"),
    ("CONFIG_SHUFFLE_PAGE_ALLOCATOR", "y", "randomize the page allocator's free lists"),
    ("CONFIG_IKCONFIG", "y", "the final config is embedded, so it can be audited"),
    ("CONFIG_IKCONFIG_PROC", "y", "and read at /proc/config.gz"),
)
"""Well-known hardening options (KSPP recommendations), each ``(option, value, reason)``."""


@component
class KernelHardening(Composite):
    """``items=(KernelHardening(),)``: :data:`ATTACK_SURFACE` off and :data:`HARDENING` on.

    Two :class:`KernelConfig` declarations, ``kernel-attack-surface`` and
    ``kernel-hardening``, for a composed ``Kernel(base=...)``; the build checks every
    value survived ``olddefconfig``. *allow* names options to leave out (a subsystem
    the image needs, or one the base's dependencies keep on). Fragment name:
    ``kernel-hardening``.
    """

    allow: Sequence[str] = ()

    def compose(self) -> tuple[KernelConfig, ...]:
        allowed = set(self.allow)
        known = {*ATTACK_SURFACE, *(option for option, _, _ in HARDENING)}
        unknown = sorted(allowed - known)
        if unknown:
            raise ValidationError(
                f"KernelHardening: allow names options it does not set: {', '.join(unknown)}.",
                hint="Name CONFIG_* options from ATTACK_SURFACE or HARDENING.",
            )
        off = {option: None for option in ATTACK_SURFACE if option not in allowed}
        on = {option: value for option, value, _ in HARDENING if option not in allowed}
        items = []
        if off:
            items.append(
                KernelConfig(off, name="kernel-attack-surface", reason="unused subsystems")
            )
        if on:
            items.append(KernelConfig(on, name="kernel-hardening", reason="hardening options"))
        return tuple(items)


__all__ = ["ATTACK_SURFACE", "HARDENING", "KernelHardening"]
