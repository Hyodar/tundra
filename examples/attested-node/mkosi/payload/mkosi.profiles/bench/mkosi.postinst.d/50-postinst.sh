#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot systemctl enable tundravm-probe.service
mkosi-chroot systemctl enable tundravm-probe-again.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/usr/lib/systemd/system/tundravm-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-probe.service"
