#!/usr/bin/env bash
set -euo pipefail

# Kernel modules: mkosi kept KernelModules= and their dependencies (tundravm module sets
# v2); each must be in the image
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
    for module in crc32c_generic dm_verity efivarfs erofs gve hv_netvsc hv_storvsc hv_utils hv_vmbus mana nf_tables nft_ct nft_reject_inet nvme overlay pci_hyperv sd_mod tdx_guest virtio_blk virtio_net virtio_scsi; do
        if ! grep -qxF "${module//_/-}" <<<"$present"; then
            echo "tundravm: kernel module $module is not in kernel $(basename "$kdir"):" \
                "drop it from Debian(kernel_modules=...) or pick a kernel that ships it" >&2
            exit 1
        fi
    done
    # modules.dep is still the kernel package's (mkosi runs no depmod when it only keeps
    # modules): its line for a listed module names every module that loads first.
    files=$(find "$kdir" -name '*.ko*' -printf '%P\n')
    for module in crc32c_generic dm_verity efivarfs erofs gve hv_netvsc hv_storvsc hv_utils hv_vmbus mana nf_tables nft_ct nft_reject_inet nvme overlay pci_hyperv sd_mod tdx_guest virtio_blk virtio_net virtio_scsi; do
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

# Debloat: clean files in var directories
find "$BUILDROOT/var/cache" -type f -delete
find "$BUILDROOT/var/log" -type f -delete

# Debloat: remove unnecessary paths
rm -rf "$BUILDROOT/etc/credstore"
rm -rf "$BUILDROOT/etc/machine-id"
rm -rf "$BUILDROOT/etc/ssh/ssh_host_"*"_key"*
rm -rf "$BUILDROOT/usr/lib/pcrlock.d"
rm -rf "$BUILDROOT/usr/lib/systemd/catalog"
rm -rf "$BUILDROOT/usr/lib/systemd/network"
rm -rf "$BUILDROOT/usr/lib/systemd/user"
rm -rf "$BUILDROOT/usr/lib/systemd/user-generators"
rm -rf "$BUILDROOT/usr/lib/tmpfiles.d"
rm -rf "$BUILDROOT/usr/lib/udev/hwdb.bin"
rm -rf "$BUILDROOT/usr/lib/udev/hwdb.d"
rm -rf "$BUILDROOT/usr/share/bash-completion"
rm -rf "$BUILDROOT/usr/share/bug"
rm -rf "$BUILDROOT/usr/share/debconf"
rm -rf "$BUILDROOT/usr/share/doc"
rm -rf "$BUILDROOT/usr/share/gcc"
rm -rf "$BUILDROOT/usr/share/gdb"
rm -rf "$BUILDROOT/usr/share/info"
rm -rf "$BUILDROOT/usr/share/initramfs-tools"
rm -rf "$BUILDROOT/usr/share/lintian"
rm -rf "$BUILDROOT/usr/share/locale"
rm -rf "$BUILDROOT/usr/share/man"
rm -rf "$BUILDROOT/usr/share/menu"
rm -rf "$BUILDROOT/usr/share/mime"
rm -rf "$BUILDROOT/usr/share/perl5/debconf"
rm -rf "$BUILDROOT/usr/share/polkit-1"
rm -rf "$BUILDROOT/usr/share/systemd"
rm -rf "$BUILDROOT/usr/share/zsh"

# User-defined finalize commands
sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release" 
rm -rf "$BUILDROOT/usr/lib/udev/hwdb.bin" "$BUILDROOT/usr/lib/udev/hwdb.d" "$BUILDROOT/usr/share/doc" "$BUILDROOT/usr/share/fonts" "$BUILDROOT/usr/share/info" "$BUILDROOT/usr/share/javascript" "$BUILDROOT/usr/share/locale" "$BUILDROOT/usr/share/man"

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
