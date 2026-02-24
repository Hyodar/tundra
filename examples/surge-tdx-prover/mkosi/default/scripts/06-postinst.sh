#!/usr/bin/env bash
set -euo pipefail

EFI_SNAPSHOT_URL="https://snapshot.debian.org/archive/debian/20251113T083151Z/"
EFI_PACKAGE_VERSION="255.4-1"
DEB_URL="${EFI_SNAPSHOT_URL}/pool/main/s/systemd/systemd-boot-efi_${EFI_PACKAGE_VERSION}_amd64.deb"
WORK_DIR=$(mktemp -d)
curl -sSfL -o "$WORK_DIR/systemd-boot-efi.deb" "$DEB_URL"
cp "$WORK_DIR/systemd-boot-efi.deb" "$BUILDROOT/tmp/"
mkosi-chroot dpkg -i /tmp/systemd-boot-efi.deb
cp "$BUILDROOT/usr/lib/systemd/boot/efi/systemd-bootx64.efi" "$BUILDROOT/usr/lib/systemd/boot/efi/linuxx64.efi.stub" 2>/dev/null || true
rm -rf "$WORK_DIR" "$BUILDROOT/tmp/systemd-boot-efi.deb"
mkosi-chroot groupadd --system tdx
mkosi-chroot useradd --system --home-dir /home/tdxs --shell /usr/sbin/nologin --gid tdx tdxs
mkosi-chroot systemctl enable tdxs.socket
mkosi-chroot groupadd -r eth
mkosi-chroot useradd --system --home-dir /home/raiko --shell /usr/sbin/nologin --gid tdx raiko
mkosi-chroot useradd --system --home-dir /home/taiko-client --shell /usr/sbin/nologin --groups eth taiko-client
mkosi-chroot useradd --system --home-dir /home/nethermind-surge --shell /usr/sbin/nologin --groups eth nethermind-surge
mkosi-chroot usermod -a -G tdx nethermind-surge
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
mkosi-chroot systemctl enable network-setup.service
ln -sf "/etc/systemd/system/network-setup.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable openntpd.service
ln -sf "/etc/systemd/system/openntpd.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable logrotate.service
ln -sf "/etc/systemd/system/logrotate.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable runtime-init.service
ln -sf "/etc/systemd/system/runtime-init.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable dropbear.service
ln -sf "/etc/systemd/system/dropbear.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable nethermind-surge.service
ln -sf "/etc/systemd/system/nethermind-surge.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable taiko-client.service
ln -sf "/etc/systemd/system/taiko-client.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable raiko.service
ln -sf "/etc/systemd/system/raiko.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable tdxs.service
ln -sf "/etc/systemd/system/tdxs.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl enable tdxs.socket
ln -sf "/etc/systemd/system/tdxs.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl disable ssh.service ssh.socket
mkosi-chroot systemctl mask ssh.service ssh.socket

# Debloat: remove unwanted systemd binaries
systemd_bin_whitelist=("journalctl" "systemctl" "systemd" "systemd-tty-ask-password-agent")
mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/' | while read -r bin_path; do
    bin_name=$(basename "$bin_path")
    if ! printf '%s\n' "${systemd_bin_whitelist[@]}" | grep -qx "$bin_name"; then
        rm -f "$BUILDROOT$bin_path"
    fi
done

# Debloat: mask unwanted systemd units
systemd_svc_whitelist=("basic.target" "local-fs-pre.target" "local-fs.target" "minimal.target" "network-online.target" "slices.target" "sockets.target" "sysinit.target" "systemd-journald-dev-log.socket" "systemd-journald.service" "systemd-journald.socket" "systemd-remount-fs.service" "systemd-sysctl.service")
SYSTEMD_DIR="$BUILDROOT/etc/systemd/system"
mkdir -p "$SYSTEMD_DIR"
mkosi-chroot dpkg-query -L systemd | grep -E '\.service$|\.socket$|\.timer$|\.target$|\.mount$' | sed 's|.*/||' | while read -r unit; do
    if ! printf '%s\n' "${systemd_svc_whitelist[@]}" | grep -qx "$unit"; then
        ln -sf /dev/null "$SYSTEMD_DIR/$unit"
    fi
done

# Set default systemd target
ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"
