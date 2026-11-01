#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot systemctl enable tundravm-probe.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/tundravm-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
