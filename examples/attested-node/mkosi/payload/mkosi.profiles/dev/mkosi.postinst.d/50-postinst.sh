#!/usr/bin/env bash
set -euo pipefail

# Debloat: unmask the systemd units this profile keeps beyond the shared payload
SYSTEMD_DIR="$BUILDROOT/etc/systemd/system"
for unit in "serial-getty@.service"; do
    if [ "$(readlink "$SYSTEMD_DIR/$unit" || true)" = /dev/null ]; then
        rm "$SYSTEMD_DIR/$unit"
    fi
done
# A kept trigger is masked exactly when its unit is
systemd_svc_whitelist=("basic.target" "local-fs-pre.target" "local-fs.target" "minimal.target" "network-online.target" "serial-getty@.service" "slices.target" "sockets.target" "sysinit.target" "systemd-journald-dev-log.socket" "systemd-journald.service" "systemd-journald.socket" "systemd-networkd-wait-online.service" "systemd-networkd.service" "systemd-networkd.socket" "systemd-remount-fs.service" "systemd-sysctl.service" "timers.target")
mkosi-chroot dpkg-query -L systemd |
    grep -E '/systemd/system/[^/]+\.(socket|timer|path|automount)$' | while read -r path; do
    trigger=${path##*/}
    printf '%s\n' "${systemd_svc_whitelist[@]}" | grep -qx "$trigger" || continue
    unit=$(sed -nE 's/^(Unit|Service)=//p' "$BUILDROOT$path" | tail -n 1)
    if [ -z "$unit" ]; then
        case $trigger in
            *.automount) unit=${trigger%.*}.mount ;;
            *) unit=${trigger%.*}.service ;;
        esac
    fi
    if [ "$(readlink "$SYSTEMD_DIR/$unit" || true)" = /dev/null ]; then
        ln -sf /dev/null "$SYSTEMD_DIR/$trigger"
    elif [ "$(readlink "$SYSTEMD_DIR/$trigger" || true)" = /dev/null ]; then
        rm "$SYSTEMD_DIR/$trigger"
    fi
done

mkosi-chroot systemctl enable serial-getty@ttyS0.service
mkosi-chroot systemctl enable tundravm-probe.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/serial-getty@ttyS0.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tundravm-probe.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
# Set root password and unlock account
SALT=$(printf 'tundravm-devtools:%s' "${IMAGE_ID:-devtools}" | sha256sum | cut -c1-16)
ROOT_PASS=$(openssl passwd -6 -salt "$SALT" "tdx")
printf 'root:%s\n' "$ROOT_PASS" | mkosi-chroot chpasswd -e

