#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot groupadd --system tdx
mkosi-chroot groupadd --system --gid 60632 searcher-container
mkosi-chroot groupadd --system azure-complete-provisioning
mkosi-chroot groupadd --system gcp-hostname
mkosi-chroot groupadd --system gcp-forwarded-ips
mkosi-chroot useradd --system --home-dir /home/tdxs --create-home --shell /usr/sbin/nologin --gid tdx tdxs
mkosi-chroot useradd --system --shell /usr/sbin/nologin nethermind
mkosi-chroot useradd --system --shell /usr/sbin/nologin nethermind-ready
mkosi-chroot useradd --system --shell /usr/sbin/nologin lighthouse
mkosi-chroot useradd --system --shell /usr/sbin/nologin lighthouse-ready
mkosi-chroot useradd --system --shell /usr/sbin/nologin --uid 60632 --gid searcher-container searcher-container
mkosi-chroot useradd --system --shell /usr/sbin/nologin searcher-container-endpoint-8545
mkosi-chroot useradd --system --shell /usr/sbin/nologin metrics-export
mkosi-chroot useradd --shell /usr/bin/login-menu searcher
mkosi-chroot systemctl enable azure-complete-provisioning.service
mkosi-chroot systemctl enable gcp-hostname.service
mkosi-chroot systemctl enable gcp-forwarded-ips.timer
mkosi-chroot systemctl enable chrony.service
mkosi-chroot systemctl enable systemd-time-wait-sync.service
mkosi-chroot systemctl enable systemd-resolved.service
mkosi-chroot systemctl enable tdxs.socket
mkosi-chroot systemctl enable systemd-networkd.service
mkosi-chroot systemctl enable systemd-networkd.socket
mkosi-chroot systemctl enable systemd-networkd-wait-online.service
mkosi-chroot systemctl enable nethermind.service
mkosi-chroot systemctl enable nethermind-ready.service
mkosi-chroot systemctl enable lighthouse.service
mkosi-chroot systemctl enable lighthouse-ready.service
mkosi-chroot systemctl enable searcher-container.service
mkosi-chroot systemctl enable searcher-container-ssh-keys.service
mkosi-chroot systemctl enable searcher-container-endpoint-8545.socket
mkosi-chroot systemctl enable image-metrics.service
mkosi-chroot systemctl enable prometheus-node-exporter.service
mkosi-chroot systemctl enable metrics-export.service
mkosi-chroot systemctl enable delay-pipe.service
mkosi-chroot systemctl enable input-proxy.service
mkosi-chroot systemctl enable tundravm-volume-owner.service
mkosi-chroot systemctl enable tundravm-endpoint.service
mkosi-chroot systemctl enable tundravm-generated-jwt.service
mkosi-chroot systemctl enable ssh-host-keys.service
mkosi-chroot systemctl enable ssh.service
mkosi-chroot systemctl enable systemd-user-sessions.service
mkosi-chroot systemctl enable credential-searcher-keys.service
mkosi-chroot systemctl enable nftables.service
mkosi-chroot systemctl enable mode-switch.service
mkosi-chroot systemctl enable egress-resolve.timer
mkosi-chroot systemctl enable tdx-evidence.service
mkosi-chroot systemctl enable tundravm-instance-key.service
mkosi-chroot systemctl enable tundravm-power-button.service
mkosi-chroot systemctl enable runtime-init.service
mkosi-chroot systemctl enable tundravm-held.target
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/usr/lib/systemd/system/azure-complete-provisioning.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/azure-complete-provisioning.service"
ln -sf "/usr/lib/systemd/system/gcp-hostname.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/gcp-hostname.service"
ln -sf "/usr/lib/systemd/system/gcp-forwarded-ips.timer" "$BUILDROOT/etc/systemd/system/minimal.target.wants/gcp-forwarded-ips.timer"
ln -sf "/usr/lib/systemd/system/chrony.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/chrony.service"
ln -sf "/usr/lib/systemd/system/systemd-time-wait-sync.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/systemd-time-wait-sync.service"
ln -sf "/usr/lib/systemd/system/systemd-resolved.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/systemd-resolved.service"
ln -sf "/usr/lib/systemd/system/tdxs.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tdxs.socket"
ln -sf "/usr/lib/systemd/system/systemd-networkd.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/systemd-networkd.service"
ln -sf "/usr/lib/systemd/system/systemd-networkd.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/systemd-networkd.socket"
ln -sf "/usr/lib/systemd/system/systemd-networkd-wait-online.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/systemd-networkd-wait-online.service"
ln -sf "/usr/lib/systemd/system/searcher-container-ssh-keys.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/searcher-container-ssh-keys.service"
ln -sf "/usr/lib/systemd/system/searcher-container-endpoint-8545.socket" "$BUILDROOT/etc/systemd/system/minimal.target.wants/searcher-container-endpoint-8545.socket"
ln -sf "/usr/lib/systemd/system/image-metrics.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/image-metrics.service"
ln -sf "/usr/lib/systemd/system/prometheus-node-exporter.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/prometheus-node-exporter.service"
ln -sf "/usr/lib/systemd/system/metrics-export.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/metrics-export.service"
ln -sf "/usr/lib/systemd/system/tundravm-volume-owner.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-volume-owner.service"
ln -sf "/usr/lib/systemd/system/tundravm-endpoint.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-endpoint.service"
ln -sf "/usr/lib/systemd/system/tundravm-generated-jwt.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-generated-jwt.service"
ln -sf "/usr/lib/systemd/system/ssh-host-keys.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/ssh-host-keys.service"
ln -sf "/usr/lib/systemd/system/ssh.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/ssh.service"
ln -sf "/usr/lib/systemd/system/systemd-user-sessions.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/systemd-user-sessions.service"
ln -sf "/usr/lib/systemd/system/credential-searcher-keys.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/credential-searcher-keys.service"
ln -sf "/usr/lib/systemd/system/nftables.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/nftables.service"
ln -sf "/usr/lib/systemd/system/mode-switch.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/mode-switch.service"
ln -sf "/usr/lib/systemd/system/egress-resolve.timer" "$BUILDROOT/etc/systemd/system/minimal.target.wants/egress-resolve.timer"
ln -sf "/usr/lib/systemd/system/tdx-evidence.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tdx-evidence.service"
ln -sf "/usr/lib/systemd/system/tundravm-instance-key.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-instance-key.service"
ln -sf "/usr/lib/systemd/system/tundravm-power-button.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-power-button.service"
ln -sf "/usr/lib/systemd/system/runtime-init.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/runtime-init.service"
ln -sf "/usr/lib/systemd/system/tundravm-held.target" "$BUILDROOT/etc/systemd/system/minimal.target.wants/tundravm-held.target"
# resolv.conf is systemd-resolved's stub file, which forwards every lookup over TLS
ln -sfn ../run/systemd/resolve/stub-resolv.conf "$BUILDROOT/etc/resolv.conf"

mkosi-chroot promtool check rules /etc/tundravm/metrics-rules.yml
mkdir -p "$BUILDROOT/etc/ssh/authorized_keys.d"
mkosi-chroot visudo -c -f /etc/sudoers.d/searcher
mkosi-chroot systemctl mask apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer getty@tty1.service prometheus.service acpid.path acpid.service acpid.socket nvmf-autoconnect.service nvmefc-boot-connections.service uuidd.socket uuidd.service

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
systemd_svc_whitelist=("basic.target" "final.target" "local-fs-pre.target" "local-fs.target" "minimal.target" "network-online.target" "poweroff.target" "reboot.target" "shutdown.target" "slices.target" "sockets.target" "sysinit.target" "systemd-journald-dev-log.socket" "systemd-journald.service" "systemd-journald.socket" "systemd-networkd-wait-online.service" "systemd-networkd.service" "systemd-networkd.socket" "systemd-poweroff.service" "systemd-reboot.service" "systemd-remount-fs.service" "systemd-resolved.service" "systemd-sysctl.service" "systemd-time-wait-sync.service" "systemd-user-sessions.service" "timers.target" "umount.target")
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
