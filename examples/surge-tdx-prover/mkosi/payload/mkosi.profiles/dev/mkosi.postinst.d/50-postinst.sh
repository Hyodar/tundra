#!/usr/bin/env bash
set -euo pipefail

mkosi-chroot systemctl enable serial-getty@ttyS0.service
mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"
ln -sf "/etc/systemd/system/serial-getty@ttyS0.service" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"
# Set root password and unlock account
SALT=$(printf 'tundravm-devtools:%s' "${IMAGE_ID:-devtools}" | sha256sum | cut -c1-16)
ROOT_PASS=$(openssl passwd -6 -salt "$SALT" "tdx")
usermod -p "$ROOT_PASS" root
passwd -u root

# Enable password authentication for dropbear (remove restrictive flags)
if [ -f /etc/default/dropbear ]; then
    sed -i 's/ -s//g; s/ -w//g; s/ -g//g' /etc/default/dropbear
fi

# Enable password authentication for openssh
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-devtools.conf << 'SSHEOF'
 PermitRootLogin yes
 PasswordAuthentication yes
SSHEOF

