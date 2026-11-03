#!/usr/bin/env bash
set -euo pipefail

KERNEL_CACHE="${BUILDDIR:-$BUILDROOT/build}/kernel-6.18.57-5ba8364233c4-2ff600d29ee4-fc2ec27163fab962"
KERNEL_CHROOT="${BUILDDIR:-/build}/kernel-6.18.57-5ba8364233c4-2ff600d29ee4-fc2ec27163fab962"
KERNEL_VERSION="6.18.57"
# make runs in the image through mkosi-chroot, where the build packages are, and the cache
# is $KERNEL_CHROOT; the build stamps are fixed, so two builds give the same bytes
kmake() {
    mkosi-chroot env KBUILD_BUILD_TIMESTAMP=1970-01-01 KBUILD_BUILD_USER=tundravm \
        KBUILD_BUILD_HOST=tundravm make -C "$KERNEL_CHROOT/src" ARCH=x86_64 "$@"
}

if [ -d "$KERNEL_CACHE/done" ]; then
    echo "Using cached kernel build: kernel-6.18.57-5ba8364233c4-2ff600d29ee4-fc2ec27163fab962"
else
    rm -rf "$KERNEL_CACHE"
    mkdir -p "$KERNEL_CACHE"

    [ -f "$SRCDIR/tundravm-sources/kernel-2ff600d29ee4-2c093006"/.tundravm-complete ] || { echo 'tundravm: kernel-2ff600d29ee4-2c093006 is not fetched: run tundravm build' >&2; exit 1; }
    mkdir -p "$KERNEL_CACHE/src"
    find "$SRCDIR/tundravm-sources/kernel-2ff600d29ee4-2c093006" -mindepth 1 -maxdepth 1 ! -name .git ! -name .tundravm-complete \
        -exec cp -a --no-preserve=ownership -t "$KERNEL_CACHE/src" {} +

    KERNEL_TREE="$PWD/payload/kernel"
    [ -f "$SRCDIR/tundravm-sources/kernel-config-7008d38386b9-ac4671b4/.tundravm-complete" ] || { echo 'tundravm: kernel-config-7008d38386b9-ac4671b4 is not fetched: run tundravm build' >&2; exit 1; }
    echo "7008d38386b9c9552fc347e5ea3a6f85e4e8ce9333a9e4c6f09ad3055348d959  $SRCDIR/tundravm-sources/kernel-config-7008d38386b9-ac4671b4/linux-config-6.18_6.18.15-1~bpo13+1_amd64.deb" | sha256sum -c --quiet -
    dpkg-deb --fsys-tarfile "$SRCDIR/tundravm-sources/kernel-config-7008d38386b9-ac4671b4/linux-config-6.18_6.18.15-1~bpo13+1_amd64.deb" | tar -xO ./usr/src/linux-config-6.18/config.amd64_none_cloud-amd64.xz \
        | xz -dc > "$KERNEL_CACHE/src/.config"
    cp "$KERNEL_CACHE/src/.config" "$KERNEL_CACHE/kernel-config.before"
    mkosi-chroot patch -d "$KERNEL_CHROOT/src" -p1 --batch --forward < "$KERNEL_TREE/patches/01-0001-acpi-gcp-block-aml-systemmemory-ram-access.patch" \
        || { echo 'tundravm: kernel patch 0001-acpi-gcp-block-aml-systemmemory-ram-access.patch does not apply' >&2; exit 1; }
    cd "$KERNEL_CACHE/src"
    ./scripts/kconfig/merge_config.sh -m .config "$KERNEL_TREE/fragment.config"
    kmake olddefconfig
    cp .config "$KERNEL_CACHE/kernel-config.after"
    # Kconfig drops a requested option whose dependencies are unmet: check each survived
    kconfig_check() {
        local final
        final=$(sed -n "s/^$1=//p" .config)
        if [ "${final:-n}" != "$2" ]; then
            echo "tundravm: kernel-option-dropped: $1 requested $2, final ${final:-n}" >&2
            return 1
        fi
    }
    dropped=0
    kconfig_check CONFIG_MODULES n || dropped=1
    kconfig_check CONFIG_BTRFS_FS n || dropped=1
    kconfig_check CONFIG_CEPH_FS n || dropped=1
    kconfig_check CONFIG_CIFS n || dropped=1
    kconfig_check CONFIG_SMB_SERVER n || dropped=1
    kconfig_check CONFIG_NFS_FS n || dropped=1
    kconfig_check CONFIG_NFSD n || dropped=1
    kconfig_check CONFIG_NTFS3_FS n || dropped=1
    kconfig_check CONFIG_ISO9660_FS n || dropped=1
    kconfig_check CONFIG_UDF_FS n || dropped=1
    kconfig_check CONFIG_ZONEFS_FS n || dropped=1
    kconfig_check CONFIG_FUSE_FS n || dropped=1
    kconfig_check CONFIG_FS_DAX n || dropped=1
    kconfig_check CONFIG_FS_ENCRYPTION n || dropped=1
    kconfig_check CONFIG_FS_VERITY n || dropped=1
    kconfig_check CONFIG_BLK_DEV_RBD n || dropped=1
    kconfig_check CONFIG_BLK_DEV_DRBD n || dropped=1
    kconfig_check CONFIG_BLK_DEV_NBD n || dropped=1
    kconfig_check CONFIG_BLK_DEV_NULL_BLK n || dropped=1
    kconfig_check CONFIG_BLK_DEV_UBLK n || dropped=1
    kconfig_check CONFIG_ZRAM n || dropped=1
    kconfig_check CONFIG_BLK_DEV_PCIESSD_MTIP32XX n || dropped=1
    kconfig_check CONFIG_DM_AUDIT n || dropped=1
    kconfig_check CONFIG_DM_CACHE n || dropped=1
    kconfig_check CONFIG_DM_CLONE n || dropped=1
    kconfig_check CONFIG_DM_DELAY n || dropped=1
    kconfig_check CONFIG_DM_ERA n || dropped=1
    kconfig_check CONFIG_DM_FLAKEY n || dropped=1
    kconfig_check CONFIG_DM_INIT n || dropped=1
    kconfig_check CONFIG_DM_INTEGRITY n || dropped=1
    kconfig_check CONFIG_DM_LOG_WRITES n || dropped=1
    kconfig_check CONFIG_DM_MIRROR n || dropped=1
    kconfig_check CONFIG_DM_MULTIPATH n || dropped=1
    kconfig_check CONFIG_DM_RAID n || dropped=1
    kconfig_check CONFIG_DM_SNAPSHOT n || dropped=1
    kconfig_check CONFIG_DM_SWITCH n || dropped=1
    kconfig_check CONFIG_DM_THIN_PROVISIONING n || dropped=1
    kconfig_check CONFIG_DM_UEVENT n || dropped=1
    kconfig_check CONFIG_DM_UNSTRIPED n || dropped=1
    kconfig_check CONFIG_DM_VDO n || dropped=1
    kconfig_check CONFIG_DM_ZERO n || dropped=1
    kconfig_check CONFIG_DM_ZONED n || dropped=1
    kconfig_check CONFIG_AF_KCM n || dropped=1
    kconfig_check CONFIG_BONDING n || dropped=1
    kconfig_check CONFIG_DUMMY n || dropped=1
    kconfig_check CONFIG_EQUALIZER n || dropped=1
    kconfig_check CONFIG_INFINIBAND n || dropped=1
    kconfig_check CONFIG_RDS n || dropped=1
    kconfig_check CONFIG_TIPC n || dropped=1
    kconfig_check CONFIG_IP_SCTP n || dropped=1
    kconfig_check CONFIG_MPTCP n || dropped=1
    kconfig_check CONFIG_L2TP n || dropped=1
    kconfig_check CONFIG_NET_FOU n || dropped=1
    kconfig_check CONFIG_NET_IPGRE_DEMUX n || dropped=1
    kconfig_check CONFIG_NET_IPIP n || dropped=1
    kconfig_check CONFIG_NET_IPVTI n || dropped=1
    kconfig_check CONFIG_IPV6_SIT n || dropped=1
    kconfig_check CONFIG_IPV6_TUNNEL n || dropped=1
    kconfig_check CONFIG_IPV6_VTI n || dropped=1
    kconfig_check CONFIG_IPV6_ILA n || dropped=1
    kconfig_check CONFIG_IPV6_MIP6 n || dropped=1
    kconfig_check CONFIG_IPV6_SEG6_LWTUNNEL n || dropped=1
    kconfig_check CONFIG_IPV6_SEG6_HMAC n || dropped=1
    kconfig_check CONFIG_IP_ADVANCED_ROUTER n || dropped=1
    kconfig_check CONFIG_IP_MROUTE n || dropped=1
    kconfig_check CONFIG_IPV6_MROUTE n || dropped=1
    kconfig_check CONFIG_IPV6_MULTIPLE_TABLES n || dropped=1
    kconfig_check CONFIG_NET_KEY n || dropped=1
    kconfig_check CONFIG_XFRM n || dropped=1
    kconfig_check CONFIG_XFRM_ALGO n || dropped=1
    kconfig_check CONFIG_XFRM_USER n || dropped=1
    kconfig_check CONFIG_INET_AH n || dropped=1
    kconfig_check CONFIG_INET_ESP n || dropped=1
    kconfig_check CONFIG_INET_IPCOMP n || dropped=1
    kconfig_check CONFIG_INET6_AH n || dropped=1
    kconfig_check CONFIG_INET6_ESP n || dropped=1
    kconfig_check CONFIG_INET6_IPCOMP n || dropped=1
    kconfig_check CONFIG_CRYPTO_AUTHENCESN n || dropped=1
    kconfig_check CONFIG_CRYPTO_USER_API_AEAD n || dropped=1
    kconfig_check CONFIG_CRYPTO_USER_API_RNG n || dropped=1
    kconfig_check CONFIG_NETLABEL n || dropped=1
    kconfig_check CONFIG_NET_SCH_TEQL n || dropped=1
    kconfig_check CONFIG_TCP_MD5SIG n || dropped=1
    kconfig_check CONFIG_XDP_SOCKETS n || dropped=1
    kconfig_check CONFIG_TCP_CONG_BIC n || dropped=1
    kconfig_check CONFIG_TCP_CONG_CDG n || dropped=1
    kconfig_check CONFIG_TCP_CONG_DCTCP n || dropped=1
    kconfig_check CONFIG_TCP_CONG_HSTCP n || dropped=1
    kconfig_check CONFIG_TCP_CONG_HTCP n || dropped=1
    kconfig_check CONFIG_TCP_CONG_HYBLA n || dropped=1
    kconfig_check CONFIG_TCP_CONG_ILLINOIS n || dropped=1
    kconfig_check CONFIG_TCP_CONG_LP n || dropped=1
    kconfig_check CONFIG_TCP_CONG_NV n || dropped=1
    kconfig_check CONFIG_TCP_CONG_SCALABLE n || dropped=1
    kconfig_check CONFIG_TCP_CONG_VEGAS n || dropped=1
    kconfig_check CONFIG_TCP_CONG_VENO n || dropped=1
    kconfig_check CONFIG_TCP_CONG_WESTWOOD n || dropped=1
    kconfig_check CONFIG_TCP_CONG_YEAH n || dropped=1
    kconfig_check CONFIG_XEN n || dropped=1
    kconfig_check CONFIG_KVM_XEN n || dropped=1
    kconfig_check CONFIG_VHOST n || dropped=1
    kconfig_check CONFIG_VHOST_NET n || dropped=1
    kconfig_check CONFIG_VHOST_SCSI n || dropped=1
    kconfig_check CONFIG_VHOST_VSOCK n || dropped=1
    kconfig_check CONFIG_VHOST_VDPA n || dropped=1
    kconfig_check CONFIG_VDPA n || dropped=1
    kconfig_check CONFIG_HIBERNATION n || dropped=1
    kconfig_check CONFIG_SUSPEND n || dropped=1
    kconfig_check CONFIG_X86_ACPI_CPUFREQ_CPB n || dropped=1
    kconfig_check CONFIG_X86_P4_CLOCKMOD n || dropped=1
    kconfig_check CONFIG_X86_POWERNOW_K8 n || dropped=1
    kconfig_check CONFIG_X86_SPEEDSTEP_CENTRINO n || dropped=1
    kconfig_check CONFIG_SURFACE_PLATFORMS n || dropped=1
    kconfig_check CONFIG_IA32_EMULATION n || dropped=1
    kconfig_check CONFIG_IO_URING n || dropped=1
    kconfig_check CONFIG_IO_URING_ZCRX n || dropped=1
    kconfig_check CONFIG_USERFAULTFD n || dropped=1
    kconfig_check CONFIG_KSM n || dropped=1
    kconfig_check CONFIG_PROFILING n || dropped=1
    kconfig_check CONFIG_KALLSYMS_ALL n || dropped=1
    kconfig_check CONFIG_RD_GZIP n || dropped=1
    kconfig_check CONFIG_RD_BZIP2 n || dropped=1
    kconfig_check CONFIG_RD_LZMA n || dropped=1
    kconfig_check CONFIG_RD_XZ n || dropped=1
    kconfig_check CONFIG_RD_LZO n || dropped=1
    kconfig_check CONFIG_RD_LZ4 n || dropped=1
    kconfig_check CONFIG_DEBUG_INFO_NONE y || dropped=1
    kconfig_check CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT n || dropped=1
    kconfig_check CONFIG_DEBUG_INFO_DWARF4 n || dropped=1
    kconfig_check CONFIG_DEBUG_INFO_DWARF5 n || dropped=1
    kconfig_check CONFIG_DEBUG_INFO_BTF n || dropped=1
    kconfig_check CONFIG_EXPERT y || dropped=1
    kconfig_check CONFIG_SECURITY y || dropped=1
    kconfig_check CONFIG_SECURITY_LOCKDOWN_LSM y || dropped=1
    kconfig_check CONFIG_SECURITY_LOCKDOWN_LSM_EARLY y || dropped=1
    kconfig_check CONFIG_LOCK_DOWN_KERNEL_FORCE_CONFIDENTIALITY y || dropped=1
    kconfig_check CONFIG_INIT_ON_ALLOC_DEFAULT_ON y || dropped=1
    kconfig_check CONFIG_INIT_ON_FREE_DEFAULT_ON y || dropped=1
    kconfig_check CONFIG_RANDOMIZE_KSTACK_OFFSET y || dropped=1
    kconfig_check CONFIG_RANDOMIZE_KSTACK_OFFSET_DEFAULT y || dropped=1
    kconfig_check CONFIG_DEVMEM n || dropped=1
    kconfig_check CONFIG_PROC_KCORE n || dropped=1
    kconfig_check CONFIG_KEXEC n || dropped=1
    kconfig_check CONFIG_KEXEC_FILE n || dropped=1
    kconfig_check CONFIG_BPF_UNPRIV_DEFAULT_OFF y || dropped=1
    kconfig_check CONFIG_BPF_JIT_ALWAYS_ON y || dropped=1
    kconfig_check CONFIG_LEGACY_TIOCSTI n || dropped=1
    kconfig_check CONFIG_LEGACY_VSYSCALL_NONE y || dropped=1
    kconfig_check CONFIG_MODIFY_LDT_SYSCALL n || dropped=1
    kconfig_check CONFIG_X86_IOPL_IOPERM n || dropped=1
    kconfig_check CONFIG_MAGIC_SYSRQ n || dropped=1
    kconfig_check CONFIG_COMPAT_BRK n || dropped=1
    kconfig_check CONFIG_HARDENED_USERCOPY y || dropped=1
    kconfig_check CONFIG_FORTIFY_SOURCE y || dropped=1
    kconfig_check CONFIG_STACKPROTECTOR y || dropped=1
    kconfig_check CONFIG_STACKPROTECTOR_STRONG y || dropped=1
    kconfig_check CONFIG_SLAB_FREELIST_RANDOM y || dropped=1
    kconfig_check CONFIG_SLAB_FREELIST_HARDENED y || dropped=1
    kconfig_check CONFIG_SHUFFLE_PAGE_ALLOCATOR y || dropped=1
    kconfig_check CONFIG_IKCONFIG y || dropped=1
    kconfig_check CONFIG_IKCONFIG_PROC y || dropped=1
    kconfig_check CONFIG_DEFAULT_FQ y || dropped=1
    kconfig_check CONFIG_TCP_CONG_BBR y || dropped=1
    kconfig_check CONFIG_DEFAULT_BBR y || dropped=1
    kconfig_check CONFIG_VETH y || dropped=1
    kconfig_check CONFIG_NF_NAT y || dropped=1
    kconfig_check CONFIG_NFT_NAT y || dropped=1
    kconfig_check CONFIG_NFT_MASQ y || dropped=1
    kconfig_check CONFIG_MD y || dropped=1
    kconfig_check CONFIG_BLK_DEV_DM y || dropped=1
    kconfig_check CONFIG_DM_VERITY y || dropped=1
    kconfig_check CONFIG_CRYPTO_SHA256 y || dropped=1
    kconfig_check CONFIG_EROFS_FS y || dropped=1
    kconfig_check CONFIG_EROFS_FS_XATTR y || dropped=1
    kconfig_check CONFIG_EROFS_FS_POSIX_ACL y || dropped=1
    kconfig_check CONFIG_OVERLAY_FS y || dropped=1
    kconfig_check CONFIG_BLK_DEV_LOOP y || dropped=1
    # A built kernel loads no module: every driver a target boots with must be built in
    drivers_report="$KERNEL_CACHE/kernel-drivers.txt"
    : > "$drivers_report"
    driver_required() {
        local final
        final=$(sed -n "s/^$1=//p" .config)
        echo "$1=${final:-n} required by $2" >> "$drivers_report"
        if [ "${final:-n}" != y ]; then
            echo "tundravm: kernel-driver-missing: $1 is ${final:-n}, $2 needs it built in:" \
                "add $1=y to the kernel's config (Kernel.build(config=...))" >&2
            return 1
        fi
    }
    driver_denied() {
        local final
        final=$(sed -n "s/^$1=//p" .config)
        echo "$1=${final:-n} denied" >> "$drivers_report"
        if [ -n "$final" ]; then
            echo "tundravm: kernel-driver-denied: $1 is $final, no target offers the device:" \
                "add '# $1 is not set' to the kernel's config (Kernel.build(config=...))" >&2
            return 1
        fi
    }
    missing=0
    driver_required CONFIG_ACPI_BUTTON 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_BLK_DEV_LOOP 'a Container' || missing=1
    driver_required CONFIG_BLK_DEV_NVME 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_BLK_DEV_SD 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_CONFIGFS_FS 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_CRYPTO_AES_NI_INTEL 'an encrypted Volume' || missing=1
    driver_required CONFIG_CRYPTO_CRC32C 'the Firewall, RootFS.verity(), a Container' || missing=1
    driver_required CONFIG_CRYPTO_CRC32C_INTEL 'an encrypted Volume' || missing=1
    driver_required CONFIG_CRYPTO_USER_API_SKCIPHER 'an encrypted Volume' || missing=1
    driver_required CONFIG_CRYPTO_XTS 'an encrypted Volume' || missing=1
    driver_required CONFIG_DM_CRYPT 'an encrypted Volume' || missing=1
    driver_required CONFIG_DM_VERITY 'RootFS.verity()' || missing=1
    driver_required CONFIG_EFIVAR_FS 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_EROFS_FS 'RootFS.verity(), a Container' || missing=1
    driver_required CONFIG_EXT4_FS 'an encrypted Volume' || missing=1
    driver_required CONFIG_GVE gcp || missing=1
    driver_required CONFIG_HYPERV azure || missing=1
    driver_required CONFIG_HYPERV_NET azure || missing=1
    driver_required CONFIG_HYPERV_STORAGE azure || missing=1
    driver_required CONFIG_HYPERV_UTILS azure || missing=1
    driver_required CONFIG_INTEL_TDX_GUEST 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_MICROSOFT_MANA azure || missing=1
    driver_required CONFIG_NFT_CT 'the Firewall' || missing=1
    driver_required CONFIG_NFT_REJECT_INET 'the Firewall' || missing=1
    driver_required CONFIG_NF_TABLES 'the Firewall' || missing=1
    driver_required CONFIG_OVERLAY_FS 'RootFS.verity()' || missing=1
    driver_required CONFIG_PCI_HYPERV azure || missing=1
    driver_required CONFIG_SCSI_VIRTIO 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_TDX_GUEST_DRIVER 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_TSM_REPORTS 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_VIRTIO_BLK 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_VIRTIO_NET 'qemu, azure, gcp' || missing=1
    driver_required CONFIG_VIRTIO_PCI 'qemu, azure, gcp' || missing=1
    driver_denied CONFIG_BLK_DEV_PCIESSD_MTIP32XX || missing=1
    [ "$dropped$missing" = 00 ] || exit 1
    kmake -j"$(nproc)" bzImage

    mkdir -p "$KERNEL_CACHE/done"
fi

# Install kernel to destination
INSTALL_DIR="${DESTDIR}/usr/lib/modules/${KERNEL_VERSION}"
mkdir -p "$INSTALL_DIR"
cp "$KERNEL_CACHE/src/arch/x86/boot/bzImage" "$INSTALL_DIR/vmlinuz"

# The records the postinst script hands to the output directory
mkdir -p "$ARTIFACTDIR/io.tundravm.kernel"
cp "$KERNEL_CACHE/kernel-config.before" "$ARTIFACTDIR/io.tundravm.kernel/"
cp "$KERNEL_CACHE/kernel-config.after" "$ARTIFACTDIR/io.tundravm.kernel/"
cp "$KERNEL_CACHE/kernel-drivers.txt" "$ARTIFACTDIR/io.tundravm.kernel/"

# Export for downstream phases
export KERNEL_IMAGE="$INSTALL_DIR/vmlinuz"
export KERNEL_VERSION="6.18.57"
