#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot groupadd --system tdx
mkosi-chroot useradd --system --home-dir /home/tdxs --create-home --shell /usr/sbin/nologin --gid tdx tdxs
mkosi-chroot useradd --system --shell /usr/sbin/nologin nethermind
mkosi-chroot useradd --system --shell /usr/sbin/nologin nethermind-ready
mkosi-chroot useradd --system --shell /usr/sbin/nologin taiko-client
mkosi-chroot useradd --system --shell /usr/sbin/nologin raiko
mkosi-chroot useradd --system --shell /usr/sbin/nologin raiko-ready
mkosi-chroot useradd --system --shell /usr/sbin/nologin attested-tls-443
mkosi-chroot systemctl enable azure-complete-provisioning.service
mkosi-chroot systemctl enable gcp-metadata-dns.service
mkosi-chroot systemctl enable chrony.service
mkosi-chroot systemctl enable tdxs.socket
mkosi-chroot systemctl enable systemd-networkd.service
mkosi-chroot systemctl enable systemd-networkd.socket
mkosi-chroot systemctl enable systemd-networkd-wait-online.service
mkosi-chroot systemctl enable nethermind.service
mkosi-chroot systemctl enable nethermind-ready.service
mkosi-chroot systemctl enable taiko-client.service
mkosi-chroot systemctl enable raiko.service
mkosi-chroot systemctl enable raiko-ready.service
mkosi-chroot systemctl enable ssh-host-keys.service
mkosi-chroot systemctl enable ssh.service
mkosi-chroot systemctl enable credential-jwt.service
mkosi-chroot systemctl enable credential-taiko-chain.service
mkosi-chroot systemctl enable credential-raiko-config.service
mkosi-chroot systemctl enable tundravm-credentials.service
mkosi-chroot systemctl enable attested-tls-443.service
mkosi-chroot systemctl enable nftables.service
mkosi-chroot systemctl enable tdx-evidence.service
mkosi-chroot systemctl enable runtime-init.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/azure-complete-provisioning.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/gcp-metadata-dns.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/chrony.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tdxs.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/systemd-networkd.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/systemd-networkd.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/systemd-networkd-wait-online.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/nethermind.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/nethermind-ready.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/taiko-client.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/raiko.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/raiko-ready.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/ssh-host-keys.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/ssh.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/credential-jwt.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/credential-taiko-chain.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/credential-raiko-config.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tundravm-credentials.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/attested-tls-443.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/nftables.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/tdx-evidence.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
ln -sf "/etc/systemd/system/runtime-init.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
mkosi-chroot systemctl mask apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer getty@tty1.service

# Debloat: remove unwanted systemd binaries
systemd_bin_whitelist=("journalctl" "systemctl" "systemd" "systemd-tty-ask-password-agent")
if [[ " ${PROFILES:-} " == *" dev "* ]]; then
    systemd_bin_whitelist+=("systemd-analyze")
fi
mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/' | while read -r bin_path; do
    bin_name=$(basename "$bin_path")
    if ! printf '%s\n' "${systemd_bin_whitelist[@]}" | grep -qx "$bin_name"; then
        rm -f "$BUILDROOT$bin_path"
    fi
done

# Debloat: mask unwanted systemd units
systemd_svc_whitelist=("basic.target" "local-fs-pre.target" "local-fs.target" "minimal.target" "network-online.target" "slices.target" "sockets.target" "sysinit.target" "systemd-journald-dev-log.socket" "systemd-journald.service" "systemd-journald.socket" "systemd-networkd-wait-online.service" "systemd-networkd.service" "systemd-networkd.socket" "systemd-remount-fs.service" "systemd-sysctl.service" "timers.target")
SYSTEMD_DIR="$BUILDROOT/etc/systemd/system"
mkdir -p "$SYSTEMD_DIR"
# Keep the targets the enabled units order against or pull in: masking one drops the
# ordering (networkd starts after network-pre.target, which nftables.service precedes)
for link in "$SYSTEMD_DIR"/*.wants/* "$SYSTEMD_DIR"/*.requires/*; do
    [ -L "$link" ] || continue
    unit=${link##*/}
    case $unit in *@?*.*) unit="${unit%%@*}@.${unit##*.}" ;; esac
    for file in "$BUILDROOT"{/usr/lib,/etc}/systemd/system/"$unit"{,.d/*.conf}; do
        [ -f "$file" ] || continue
        pattern='s/^(After|Before|Wants|Requires|Requisite|BindsTo)=//p'
        for target in $(sed -nE "$pattern" "$file"); do
            case $target in *.target) systemd_svc_whitelist+=("$target") ;; esac
        done
    done
done
mkosi-chroot dpkg-query -L systemd |
    grep -E '\.(service|socket|timer|target|mount|path|automount)$' |
    sed 's|.*/||' | while read -r unit; do
    if ! printf '%s\n' "${systemd_svc_whitelist[@]}" | grep -qx "$unit"; then
        ln -sf /dev/null "$SYSTEMD_DIR/$unit"
    fi
done
# A kept trigger whose unit is masked can only fail ("Refusing to start, unit ... to
# trigger not loaded"): mask it too
mkosi-chroot dpkg-query -L systemd |
    grep -E '/systemd/system/[^/]+\.(socket|timer|path|automount)$' | while read -r path; do
    trigger=${path##*/}
    [ "$(readlink "$SYSTEMD_DIR/$trigger" || true)" != /dev/null ] || continue
    unit=$(sed -nE 's/^(Unit|Service)=//p' "$BUILDROOT$path" | tail -n 1)
    if [ -z "$unit" ]; then
        case $trigger in
            *.automount) unit=${trigger%.*}.mount ;;
            *) unit=${trigger%.*}.service ;;
        esac
    fi
    if [ "$(readlink "$SYSTEMD_DIR/$unit" || true)" = /dev/null ]; then
        ln -sf /dev/null "$SYSTEMD_DIR/$trigger"
    fi
done

# Set default systemd target
ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"
