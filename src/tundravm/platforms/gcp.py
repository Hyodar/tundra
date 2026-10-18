"""The GCP integration's files: what lowering adds to a ``gcp`` target variant.

``tundravm.declarative.state`` installs ``udev``, ships the metadata DNS
files, the disk naming rules and their NVMe helper, and sets the ``gcp``
target. The current dialect applies the metadata DNS at boot through
:func:`metadata_dns_service` (on Compute Engine only); the disk rules match GCE
disk models only, so the glue is inert on other clouds. The ``gcp`` target's
tar.gz postoutput script is generated when ``MkosiOptions.generate_cloud_postoutput``
is true (the default).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tundravm.declarative.model import Service

# ---------------------------------------------------------------------------
# /etc/hosts — GCP metadata server DNS entries
# ---------------------------------------------------------------------------

GCP_HOSTS = """\
127.0.0.1 localhost
169.254.169.254 metadata.google.internal metadata"""

# ---------------------------------------------------------------------------
# /etc/resolv.conf — GCP DNS resolver
# ---------------------------------------------------------------------------

GCP_RESOLV_CONF = """\
nameserver 169.254.169.254
options edns0 trust-ad"""

# ---------------------------------------------------------------------------
# 65-gce-disk-naming.rules — udev rules for GCE persistent disk naming
# ---------------------------------------------------------------------------

GCE_DISK_NAMING_RULES = """\
# Google Compute Engine persistent disk naming rules

# SCSI persistent disks
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd*[!0-9]", IMPORT{program}="scsi_id --export --whitelisted -d /dev/%k", ENV{ID_SERIAL_SHORT}=="?*", SYMLINK+="disk/by-id/google-$env{ID_SERIAL_SHORT}"
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd*", ATTR{partition}=="?*", IMPORT{parent}=="ID_SERIAL_SHORT", ENV{ID_SERIAL_SHORT}=="?*", SYMLINK+="disk/by-id/google-$env{ID_SERIAL_SHORT}-part%n"

# SCSI local SSDs
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd*[!0-9]", ATTRS{model}=="EphemeralDisk   ", PROGRAM="scsi_id --whitelisted --export -d /dev/%k", SYMLINK+="disk/by-id/google-local-ssd-$env{ID_SERIAL_SHORT}"
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd*", ATTRS{model}=="EphemeralDisk   ", ATTR{partition}=="?*", PROGRAM="scsi_id --whitelisted --export -d /dev/%k", SYMLINK+="disk/by-id/google-local-ssd-$env{ID_SERIAL_SHORT}-part%n"

# NVMe persistent disks
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="nvme*n*", ATTRS{model}=="nvme_card-pd", PROGRAM="/usr/lib/udev/google_nvme_id %k", SYMLINK+="disk/by-id/google-%c"
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="nvme*n*p*", ATTRS{model}=="nvme_card-pd", PROGRAM="/usr/lib/udev/google_nvme_id %k", SYMLINK+="disk/by-id/google-%c-part%n"

# NVMe local SSDs
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="nvme*n*", ATTRS{model}=="nvme_card        ", PROGRAM="/usr/lib/udev/google_nvme_id %k", SYMLINK+="disk/by-id/google-local-nvme-ssd-%c"
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="nvme*n*p*", ATTRS{model}=="nvme_card        ", PROGRAM="/usr/lib/udev/google_nvme_id %k", SYMLINK+="disk/by-id/google-local-nvme-ssd-%c-part%n"
"""

# ---------------------------------------------------------------------------
# google_nvme_id — helper script for NVMe disk identification
# ---------------------------------------------------------------------------

GOOGLE_NVME_ID = """\
#!/bin/bash
# Google NVMe ID helper for udev
# Extracts disk serial from NVMe namespace for /dev/disk/by-id symlinks

set -euo pipefail

DISK="$1"

# Strip partition suffix to get the namespace device
NAMESPACE="${DISK%%p*}"

# Read the serial from sysfs
SERIAL=$(cat "/sys/class/block/${NAMESPACE}/device/serial" 2>/dev/null) || exit 1

if [[ -z "$SERIAL" ]]; then
    exit 1
fi

# Trim trailing whitespace
echo "${SERIAL}" | sed 's/[[:space:]]*$//'
"""

# ---------------------------------------------------------------------------
# Metadata DNS, applied at boot on GCP only (current dialect)
# ---------------------------------------------------------------------------

GCP_RESOLV_CONF_PATH = "/usr/lib/tundravm/gcp/resolv.conf"
"""Where the current dialect ships :data:`GCP_RESOLV_CONF`; ``gcp-metadata-dns.service``
copies it over ``/etc/resolv.conf`` on Compute Engine only."""


def metadata_dns_service() -> Service:
    """``gcp-metadata-dns.service``: a root oneshot gated by ``ConditionVirtualization=google``
    that points ``/etc/resolv.conf`` at the metadata server before the network comes up.

    A static ``/etc/resolv.conf`` naming 169.254.169.254 would break DNS on QEMU and Azure;
    gated at runtime, one UKI serves every output (``outputs=("qemu", "azure", "gcp")``).
    """
    from tundravm.declarative.model import Service
    from tundravm.declarative.systemd import Condition, Hardening, Root

    return Service(
        "gcp-metadata-dns",
        ("/usr/bin/cp", GCP_RESOLV_CONF_PATH, "/etc/resolv.conf"),
        description="GCP metadata server DNS",
        user=Root(),
        type="oneshot",
        remain_after_exit=True,
        restart="no",
        restart_sec=None,
        before=("network-pre.target", "network-setup.service"),
        needs=(),
        hardening=Hardening.relaxed(),
        write_paths=("/etc/resolv.conf",),
        conditions=(Condition.virtualization("google"),),
    )
