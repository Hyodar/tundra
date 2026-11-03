#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot useradd --system --shell /usr/sbin/nologin tick
mkosi-chroot useradd --system --shell /usr/sbin/nologin journal-export
mkosi-chroot useradd --system --shell /usr/sbin/nologin metrics-export
mkosi-chroot useradd --system --shell /usr/sbin/nologin box
mkosi-chroot useradd --system --shell /usr/sbin/nologin box-ready
mkosi-chroot useradd --system --shell /usr/sbin/nologin box-endpoint-8090
mkosi-chroot useradd --system --shell /usr/sbin/nologin token
mkosi-chroot useradd --system --shell /usr/sbin/nologin go-probe
mkosi-chroot useradd --system --shell /usr/sbin/nologin rust-probe
mkosi-chroot systemctl enable tick.timer
mkosi-chroot systemctl enable journal-export.service
mkosi-chroot systemctl enable image-metrics.service
mkosi-chroot systemctl enable prometheus-node-exporter.service
mkosi-chroot systemctl enable metrics-export.service
mkosi-chroot systemctl enable box-loop.service
mkosi-chroot systemctl enable box.service
mkosi-chroot systemctl enable box-ready.service
mkosi-chroot systemctl enable box-endpoint-8090.socket
mkosi-chroot systemctl enable sink.service
mkosi-chroot systemctl enable token.service
mkosi-chroot systemctl enable go-probe.service
mkosi-chroot systemctl enable rust-probe.service
mkosi-chroot systemctl enable tundravm-probe.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/tick.timer" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/journal-export.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/image-metrics.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/prometheus-node-exporter.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/metrics-export.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/box-loop.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/box.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/box-ready.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/box-endpoint-8090.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/sink.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/token.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/go-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/rust-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tundravm-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot promtool check rules /etc/tundravm/metrics-rules.yml
mkosi-chroot systemctl mask systemd-journal-upload.service prometheus.service
