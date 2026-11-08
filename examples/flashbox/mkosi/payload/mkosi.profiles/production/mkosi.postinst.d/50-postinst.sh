#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot systemctl enable credential-metrics-aws.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/credential-metrics-aws.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
rm -f "$BUILDROOT/etc/nvme/hostnqn" "$BUILDROOT/etc/nvme/hostid"

