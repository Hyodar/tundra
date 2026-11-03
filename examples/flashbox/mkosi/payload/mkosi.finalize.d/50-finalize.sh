#!/usr/bin/env bash
set -euo pipefail

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
