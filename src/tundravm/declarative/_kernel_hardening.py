"""Tundra's kernel hardening: the fragment ``Kernel.build(hardening=True)`` merges first.

Each row below is one ``.config`` line of the fragment, in this order. The build merges it
onto the base config, then the kernel's own fragments and each component's, runs
``olddefconfig`` and checks every value survived. A fragment that sets one of these options
itself leaves its row out, so a subsystem an image needs stays one explicit line in the
image file. With ``modules=True`` the lockdown rows are left out too: lockdown refuses
unsigned modules, and a reproducible build cannot sign them with a key it then throws away.

The off rows started as flashbots-images' deny list (``shared/kernel/config.d/01-sane-defaults``,
``02-disable-unneeded`` and ``modules/flashbox/common/kernel/config.d/10-bob``), each checked
against the Kconfig help of 6.12.113 and 6.13.12 and proven to survive ``olddefconfig`` on
Debian's ``cloud-amd64`` config and on the yocto config of ``tests/fixtures/kernel``. Their
``NET_DEVMEM`` line is not here: it has no prompt (``def_bool y``), so a fragment cannot turn
it off. Their ``IPV6=n`` is not either: the image's network and firewall speak IPv6.

| Option | Value | Why |
|---|---|---|
| CONFIG_BTRFS_FS | n | no image root or volume is btrfs |
| CONFIG_CEPH_FS | n | no network file system: the image mounts only its own disks |
| CONFIG_CIFS | n | no SMB client |
| CONFIG_SMB_SERVER | n | no in-kernel SMB server |
| CONFIG_NFS_FS | n | no NFS client |
| CONFIG_NFSD | n | no in-kernel NFS server |
| CONFIG_NTFS3_FS | n | no NTFS parser for a disk the host can attach |
| CONFIG_ISO9660_FS | n | no CD-ROM file system parser |
| CONFIG_UDF_FS | n | no UDF parser |
| CONFIG_ZONEFS_FS | n | no zoned-device file system |
| CONFIG_FUSE_FS | n | no file systems served from userspace |
| CONFIG_FS_DAX | n | no direct access to host-provided persistent memory |
| CONFIG_FS_ENCRYPTION | n | volumes are LUKS2, not fscrypt |
| CONFIG_FS_VERITY | n | the root is dm-verity, not fs-verity |
| CONFIG_BLK_DEV_RBD | n | no Ceph block devices |
| CONFIG_BLK_DEV_DRBD | n | no replicated block devices |
| CONFIG_BLK_DEV_NBD | n | no network block devices |
| CONFIG_BLK_DEV_NULL_BLK | n | no null block test device |
| CONFIG_BLK_DEV_UBLK | n | no block devices served from userspace |
| CONFIG_ZRAM | n | no compressed RAM swap |
| CONFIG_BLK_DEV_PCIESSD_MTIP32XX | n | no target offers the device, so only a hostile host could |
| CONFIG_DM_AUDIT | n | device-mapper audit events no one reads |
| CONFIG_DM_CACHE | n | dm targets beyond crypt and verity: no image uses them |
| CONFIG_DM_CLONE | n | dm target no image uses |
| CONFIG_DM_DELAY | n | dm test target |
| CONFIG_DM_ERA | n | dm target no image uses |
| CONFIG_DM_FLAKEY | n | dm test target |
| CONFIG_DM_INIT | n | no dm-mod.create=: mapped devices come from the measured initrd |
| CONFIG_DM_INTEGRITY | n | volumes are LUKS2 without dm-integrity |
| CONFIG_DM_LOG_WRITES | n | dm test target |
| CONFIG_DM_MIRROR | n | no LVM mirrors |
| CONFIG_DM_MULTIPATH | n | no multipath storage |
| CONFIG_DM_RAID | n | no software RAID |
| CONFIG_DM_SNAPSHOT | n | no LVM snapshots |
| CONFIG_DM_SWITCH | n | dm target no image uses |
| CONFIG_DM_THIN_PROVISIONING | n | no thin pools |
| CONFIG_DM_UEVENT | n | multipath path events no one reads |
| CONFIG_DM_UNSTRIPED | n | dm target no image uses |
| CONFIG_DM_VDO | n | no deduplicating block devices |
| CONFIG_DM_ZERO | n | dm test target |
| CONFIG_DM_ZONED | n | no zoned block devices |
| CONFIG_AF_KCM | n | no kernel connection multiplexor sockets |
| CONFIG_BONDING | n | one NIC: no bonding |
| CONFIG_DUMMY | n | no dummy network devices |
| CONFIG_EQUALIZER | n | no serial line load balancing |
| CONFIG_INFINIBAND | n | no RDMA fabric in a confidential VM |
| CONFIG_RDS | n | no RDS sockets |
| CONFIG_TIPC | n | no TIPC cluster sockets |
| CONFIG_IP_SCTP | n | no SCTP |
| CONFIG_MPTCP | n | no multipath TCP: one path, and its own protocol code |
| CONFIG_L2TP | n | no L2TP tunnels |
| CONFIG_NET_FOU | n | no foo-over-UDP tunnels |
| CONFIG_NET_IPGRE_DEMUX | n | no GRE tunnels |
| CONFIG_NET_IPIP | n | no IP-in-IP tunnels |
| CONFIG_NET_IPVTI | n | no IPsec tunnel interfaces |
| CONFIG_IPV6_SIT | n | no IPv6-in-IPv4 tunnels |
| CONFIG_IPV6_TUNNEL | n | no IPv6 tunnels |
| CONFIG_IPV6_VTI | n | no IPv6 IPsec tunnel interfaces |
| CONFIG_IPV6_ILA | n | no identifier-locator addressing |
| CONFIG_IPV6_MIP6 | n | no mobile IPv6 |
| CONFIG_IPV6_SEG6_LWTUNNEL | n | no segment routing |
| CONFIG_IPV6_SEG6_HMAC | n | no segment routing |
| CONFIG_IP_ADVANCED_ROUTER | n | the image is a host, not a router |
| CONFIG_IP_MROUTE | n | no multicast routing |
| CONFIG_IPV6_MROUTE | n | no IPv6 multicast routing |
| CONFIG_IPV6_MULTIPLE_TABLES | n | no policy routing |
| CONFIG_NET_KEY | n | no IPsec key sockets |
| CONFIG_XFRM | n | no IPsec: traffic is TLS end to end |
| CONFIG_XFRM_ALGO | n | no IPsec algorithms |
| CONFIG_XFRM_USER | n | no IPsec netlink interface |
| CONFIG_INET_AH | n | no IPsec AH |
| CONFIG_INET_ESP | n | no IPsec ESP |
| CONFIG_INET_IPCOMP | n | no IPsec compression |
| CONFIG_INET6_AH | n | no IPv6 IPsec AH |
| CONFIG_INET6_ESP | n | no IPv6 IPsec ESP |
| CONFIG_INET6_IPCOMP | n | no IPv6 IPsec compression |
| CONFIG_CRYPTO_AUTHENCESN | n | IPsec's ESN template, the algorithm of CVE-2026-31431 |
| CONFIG_CRYPTO_USER_API_AEAD | n | AF_ALG AEAD: cryptsetup needs only skcipher and hash |
| CONFIG_CRYPTO_USER_API_RNG | n | AF_ALG RNG: userspace reads getrandom |
| CONFIG_NETLABEL | n | no CIPSO or CALIPSO packet labels |
| CONFIG_NET_SCH_TEQL | n | no true link equalizer qdisc |
| CONFIG_TCP_MD5SIG | n | no BGP-style TCP MD5 signatures |
| CONFIG_XDP_SOCKETS | n | no AF_XDP sockets |
| CONFIG_TCP_CONG_BIC | n | unused congestion control |
| CONFIG_TCP_CONG_CDG | n | unused congestion control |
| CONFIG_TCP_CONG_DCTCP | n | unused congestion control |
| CONFIG_TCP_CONG_HSTCP | n | unused congestion control |
| CONFIG_TCP_CONG_HTCP | n | unused congestion control |
| CONFIG_TCP_CONG_HYBLA | n | unused congestion control |
| CONFIG_TCP_CONG_ILLINOIS | n | unused congestion control |
| CONFIG_TCP_CONG_LP | n | unused congestion control |
| CONFIG_TCP_CONG_NV | n | unused congestion control |
| CONFIG_TCP_CONG_SCALABLE | n | unused congestion control |
| CONFIG_TCP_CONG_VEGAS | n | unused congestion control |
| CONFIG_TCP_CONG_VENO | n | unused congestion control |
| CONFIG_TCP_CONG_WESTWOOD | n | unused congestion control |
| CONFIG_TCP_CONG_YEAH | n | unused congestion control |
| CONFIG_XEN | n | a TDX guest is not a Xen guest |
| CONFIG_KVM_XEN | n | no Xen emulation for nested guests |
| CONFIG_VHOST | n | the guest hosts no VMs: no vhost backends |
| CONFIG_VHOST_NET | n | no vhost network backend |
| CONFIG_VHOST_SCSI | n | no vhost SCSI backend |
| CONFIG_VHOST_VSOCK | n | no vhost vsock backend |
| CONFIG_VHOST_VDPA | n | no vhost vDPA backend |
| CONFIG_VDPA | n | no vDPA devices |
| CONFIG_HIBERNATION | n | no hibernation image of guest memory on a host disk |
| CONFIG_SUSPEND | n | a cloud VM does not suspend |
| CONFIG_X86_ACPI_CPUFREQ_CPB | n | host CPU frequency controls |
| CONFIG_X86_P4_CLOCKMOD | n | Pentium 4 clock modulation |
| CONFIG_X86_POWERNOW_K8 | n | AMD K8 frequency scaling |
| CONFIG_X86_SPEEDSTEP_CENTRINO | n | Centrino frequency scaling |
| CONFIG_SURFACE_PLATFORMS | n | Microsoft Surface laptop drivers |
| CONFIG_IA32_EMULATION | n | no 32-bit syscall entry: half the syscall surface |
| CONFIG_IO_URING | n | io_uring, a frequent kernel exploit vector |
| CONFIG_IO_URING_ZCRX | n | io_uring zero-copy receive |
| CONFIG_USERFAULTFD | n | userfaultfd, the usual way to win a kernel race |
| CONFIG_KSM | n | no page merging: shared pages are a timing side channel |
| CONFIG_PROFILING | n | no /proc/profile or profile= sampling of kernel addresses |
| CONFIG_KALLSYMS_ALL | n | kallsyms keeps function names only, no data symbol addresses |
| CONFIG_RD_GZIP | n | the UKI's initrd is zstd: no other decompressor runs at boot |
| CONFIG_RD_BZIP2 | n | unused initrd decompressor |
| CONFIG_RD_LZMA | n | unused initrd decompressor |
| CONFIG_RD_XZ | n | unused initrd decompressor |
| CONFIG_RD_LZO | n | unused initrd decompressor |
| CONFIG_RD_LZ4 | n | unused initrd decompressor |
| CONFIG_DEBUG_INFO_NONE | y | no DWARF: hours of build and gigabytes no one ships |
| CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT | n | the other DWARF choices, off |
| CONFIG_DEBUG_INFO_DWARF4 | n | the other DWARF choices, off |
| CONFIG_DEBUG_INFO_DWARF5 | n | the other DWARF choices, off |
| CONFIG_DEBUG_INFO_BTF | n | no BTF: no BPF program reads kernel types |
| CONFIG_EXPERT | y | the prompts of IO_URING and MODIFY_LDT_SYSCALL sit behind it |
| CONFIG_SECURITY | y | the LSM framework lockdown runs in |
| CONFIG_SECURITY_LOCKDOWN_LSM | y | lockdown: root cannot read or patch kernel memory |
| CONFIG_SECURITY_LOCKDOWN_LSM_EARLY | y | lockdown is active before userspace runs |
| CONFIG_LOCK_DOWN_KERNEL_FORCE_CONFIDENTIALITY | y | lockdown in its strictest mode |
| CONFIG_INIT_ON_ALLOC_DEFAULT_ON | y | zero pages and slabs on allocation |
| CONFIG_INIT_ON_FREE_DEFAULT_ON | y | zero memory on free: no stale secrets |
| CONFIG_RANDOMIZE_KSTACK_OFFSET | y | allow a per-syscall kernel stack offset |
| CONFIG_RANDOMIZE_KSTACK_OFFSET_DEFAULT | y | randomize it on every syscall |
| CONFIG_DEVMEM | n | no /dev/mem: physical memory is not a file |
| CONFIG_PROC_KCORE | n | no /proc/kcore: no live image of kernel memory |
| CONFIG_KEXEC | n | no kexec: the measured kernel cannot be swapped at runtime |
| CONFIG_KEXEC_FILE | n | no kexec_file_load either |
| CONFIG_BPF_UNPRIV_DEFAULT_OFF | y | unprivileged BPF is off |
| CONFIG_BPF_JIT_ALWAYS_ON | y | no BPF interpreter to abuse as a Spectre gadget |
| CONFIG_LEGACY_TIOCSTI | n | no TIOCSTI keystroke injection into terminals |
| CONFIG_LEGACY_VSYSCALL_NONE | y | no fixed-address vsyscall page |
| CONFIG_MODIFY_LDT_SYSCALL | n | no modify_ldt: 16-bit segment attack surface |
| CONFIG_X86_IOPL_IOPERM | n | no userspace port I/O |
| CONFIG_MAGIC_SYSRQ | n | no SysRq: a console cannot dump state or reboot |
| CONFIG_COMPAT_BRK | n | randomize the heap start |
| CONFIG_HARDENED_USERCOPY | y | bounds-check copies to and from userspace |
| CONFIG_FORTIFY_SOURCE | y | compile-time and runtime buffer overflow checks |
| CONFIG_STACKPROTECTOR | y | stack canaries, the base of the strong variant |
| CONFIG_STACKPROTECTOR_STRONG | y | canaries in every function with arrays |
| CONFIG_SLAB_FREELIST_RANDOM | y | randomize slab freelists |
| CONFIG_SLAB_FREELIST_HARDENED | y | detect slab freelist corruption |
| CONFIG_SHUFFLE_PAGE_ALLOCATOR | y | randomize the page allocator's free lists |
| CONFIG_IKCONFIG | y | the final config is embedded, so it can be audited |
| CONFIG_IKCONFIG_PROC | y | and read at /proc/config.gz |
"""

from __future__ import annotations

import re

from .model import KernelConfig

__all__ = ["HARDENING", "LOCKDOWN", "hardening_fragment"]

_ROW = re.compile(r"^\| (CONFIG_[A-Z0-9_]+) \| ([yn]) \| (.+) \|$", re.MULTILINE)

HARDENING: tuple[tuple[str, str, str], ...] = tuple(
    (option, value, reason) for option, value, reason in _ROW.findall(__doc__ or "")
)
"""The module docstring's table, row by row: ``(option, value, reason)``."""

LOCKDOWN: frozenset[str] = frozenset(
    option for option, _, _ in HARDENING if "LOCKDOWN" in option or "LOCK_DOWN" in option
)
"""The rows a kernel with loadable modules leaves out."""


def hardening_fragment(*, modules: bool, kept: frozenset[str] = frozenset()) -> KernelConfig:
    """:data:`HARDENING` as one fragment, ``kernel-hardening``: without :data:`LOCKDOWN` when
    the kernel loads *modules*, and without the rows of *kept*, the options another fragment
    sets itself."""
    left_out = kept | (LOCKDOWN if modules else frozenset())
    return KernelConfig(
        {option: value for option, value, _ in HARDENING if option not in left_out},
        name="kernel-hardening",
        reason="Kernel.build(hardening=True)",
    )
