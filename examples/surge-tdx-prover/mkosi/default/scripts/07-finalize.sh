#!/usr/bin/env bash
set -euo pipefail

# Debloat: clean files in var directories
find "$BUILDROOT/var/cache" -type f -delete
find "$BUILDROOT/var/log" -type f -delete

# Debloat: remove unnecessary paths
rm -rf "$BUILDROOT/etc/credstore"
rm -rf "$BUILDROOT/etc/machine-id"
rm -rf "$BUILDROOT/etc/ssh/ssh_host_*_key*"
rm -rf "$BUILDROOT/etc/systemd/network"
rm -rf "$BUILDROOT/usr/lib/modules"
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
sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/etc/os-release"
