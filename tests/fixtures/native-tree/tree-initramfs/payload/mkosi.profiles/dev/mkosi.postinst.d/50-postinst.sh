#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot groupadd --system tdx
mkosi-chroot useradd --system --shell /usr/sbin/nologin tick
mkosi-chroot useradd --system --shell /usr/sbin/nologin journal-export
mkosi-chroot useradd --system --shell /usr/sbin/nologin metrics-export
mkosi-chroot useradd --system --home-dir /home/tdxs --create-home --shell /usr/sbin/nologin --gid tdx tdxs
mkosi-chroot systemctl enable ssh-host-keys.service
mkosi-chroot systemctl enable ssh.service
mkosi-chroot systemctl enable tick.timer
mkosi-chroot systemctl enable journal-export.service
mkosi-chroot systemctl enable prometheus-node-exporter.service
mkosi-chroot systemctl enable metrics-export.service
mkosi-chroot systemctl enable sink.service
mkosi-chroot systemctl enable tdxs.socket
mkosi-chroot systemctl enable tundravm-endpoint.service
mkosi-chroot systemctl enable nftables.service
mkosi-chroot systemctl enable tundravm-probe.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/ssh-host-keys.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/ssh.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tick.timer" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/journal-export.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/prometheus-node-exporter.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/metrics-export.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/sink.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tdxs.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tundravm-endpoint.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/nftables.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tundravm-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl mask systemd-journal-upload.service prometheus.service
