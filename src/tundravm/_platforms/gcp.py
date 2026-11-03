"""The GCP integration's files: what lowering adds to a ``gcp`` target variant.

``tundravm.declarative.state`` installs ``udev`` and :data:`GCP_PACKAGES`, ships the
metadata server's ``/etc/hosts`` name, Google's disk naming rules and their NVMe helper (vendored from
guest-configs at :data:`GUEST_CONFIGS_COMMIT`), the hostname and forwarded-IP scripts, and
sets the ``gcp`` target. Every unit is gated by ``ConditionVirtualization=google`` and the
disk rules match Google's disk models only, so the same UKI stays inert on other clouds.
The ``gcp`` wrapper image converts the disk to the ``tar.gz`` GCE imports.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tundravm.declarative.model import Service, Task

# ---------------------------------------------------------------------------
# /etc/hosts — GCP metadata server DNS entries
# ---------------------------------------------------------------------------

GCP_HOSTS = """\
127.0.0.1 localhost
169.254.169.254 metadata.google.internal metadata"""

# ---------------------------------------------------------------------------
# The built kernel's ACPI patch, vendored unchanged
# ---------------------------------------------------------------------------

KERNEL_PATCH = (
    Path(__file__).with_name("gcp-kernel") / "0001-acpi-gcp-block-aml-systemmemory-ram-access.patch"
)
"""The BadAML sandbox a gcp target's built kernel applies: AML in the ACPI tables, which the
host writes, may not read or write the guest's private memory through a SystemMemory
region, so the host cannot use its own firmware to read secrets. Edgeless Systems' patch
(BSD-3-Clause OR GPL-2.0, its header kept), copied byte for byte from flashbots-images
``mkosi.profiles/gcp/kernel/patches/`` at ``09c7576``; it applies to 6.12 and 6.13. Without
memory encryption (QEMU without TDX) it denies every such access and logs an ACPI error."""

# ---------------------------------------------------------------------------
# Disk naming: Google's udev rules and their NVMe helper, vendored unchanged
# ---------------------------------------------------------------------------

GUEST_CONFIGS_COMMIT = "699f84a253e8008427252a45515f22fcdd7341d9"
"""The https://github.com/GoogleCloudPlatform/guest-configs commit the two files below are
copied from, byte for byte (``src/lib/udev/``; Apache-2.0, their headers kept). No Debian
package ships them: Debian's ``google-guest-agent`` carries neither."""

_VENDORED = Path(__file__).with_name("guest-configs")

DISK_RULES_PATH = "/usr/lib/udev/rules.d/65-gce-disk-naming.rules"
NVME_ID_PATH = "/usr/lib/udev/google_nvme_id"

GCE_DISK_NAMING_RULES = (_VENDORED / "65-gce-disk-naming.rules").read_text()
"""``/dev/disk/by-id/google-<device name>`` for SCSI and NVMe persistent disks and local
SSDs: what a ``Volume`` attached as ``tundravm-NAME`` is found by. The rules match Google's
disk models only, so they are inert on other clouds."""

GOOGLE_NVME_ID = (_VENDORED / "google_nvme_id").read_text()
"""The helper the rules call for NVMe disks: the device name is the JSON in the vendor
area of the namespace's identify data, which ``nvme id-ns -b`` reads (``nvme-cli``)."""

GCP_PACKAGES = ("curl", "iproute2", "nvme-cli")
"""What the GCP glue runs: ``curl`` for the metadata server, ``ip`` for the forwarded-IP
routes, ``nvme`` for :data:`GOOGLE_NVME_ID`."""

NVME_MASKED_UNITS = (
    "nvmf-autoconnect.service",
    "nvmefc-boot-connections.service",
    "uuidd.socket",
    "uuidd.service",
)
"""What ``nvme-cli`` brings and enables, masked: its NVMe-over-fabrics units (a cloud disk is
PCIe NVMe; ``nvmf-autoconnect`` runs on the ``discovery.conf`` the package ships and fails
without a fabric) and ``uuid-runtime``'s ``uuidd``, which it depends on: nothing listens
that the image did not declare."""

NVME_HOST_IDS = 'rm -f "$BUILDROOT/etc/nvme/hostnqn" "$BUILDROOT/etc/nvme/hostid"\n'
"""``nvme-cli``'s postinst writes a random host NQN and host ID, which would make every
build differ; NVMe over fabrics, their only reader, is not used."""

# ---------------------------------------------------------------------------
# Hostname from the metadata server
# ---------------------------------------------------------------------------

METADATA_SERVER = "169.254.169.254"
"""Compute Engine's metadata server: the hostname, the forwarded IPs and the identity
token. Only the units below reach it (``egress=``)."""
METADATA = f"http://{METADATA_SERVER}/computeMetadata/v1/instance"
_CURL = (
    "curl --fail --silent --show-error --connect-timeout 2 --retry 10 --retry-all-errors "
    '--retry-delay 1 --header "Metadata-Flavor: Google"'
)

HOSTNAME_SCRIPT_PATH = "/usr/lib/tundravm/gcp/hostname"
HOSTNAME_SCRIPT = f"""\
#!/bin/sh
# tundravm: the hostname is the instance's name, as the metadata server reports it.
set -eu
name=$({_CURL} {METADATA}/name)
case $name in
    [a-z]*) ;;
    *) echo "instance name '$name' is not a hostname" >&2; exit 1 ;;
esac
case $name in
    *[!a-z0-9-]*) echo "instance name '$name' is not a hostname" >&2; exit 1 ;;
esac
/usr/bin/hostname "$name"
grep -qx "127.0.1.1 $name" /etc/hosts || echo "127.0.1.1 $name" >> /etc/hosts
"""


def hostname_service() -> Service:
    """``gcp-hostname.service``: a root oneshot gated by ``ConditionVirtualization=google``
    that names the host after the instance once the network is up and before
    ``network-online.target``, so a service ordered after the network sees the name. It
    runs with its own group, the one the firewall lets reach the metadata server."""
    from tundravm.declarative.model import EgressRule, Service
    from tundravm.declarative.systemd import Condition, Hardening, Root

    return Service(
        "gcp-hostname",
        (HOSTNAME_SCRIPT_PATH,),
        description="GCP hostname from the metadata server",
        user=Root(),
        group="gcp-hostname",
        egress=(EgressRule(host=METADATA_SERVER, origins=(("gcp", ""),)),),
        type="oneshot",
        remain_after_exit=True,
        restart="no",
        restart_sec=None,
        after=("systemd-networkd-wait-online.service",),
        before=("network-online.target",),
        needs=(),
        hardening=Hardening.relaxed(),
        write_paths=("/etc/hosts",),
        conditions=(Condition.virtualization("google"),),
    )


# ---------------------------------------------------------------------------
# Forwarded-IP routes: what a passthrough load balancer delivers
# ---------------------------------------------------------------------------

ROUTES_SCRIPT_PATH = "/usr/lib/tundravm/gcp/forwarded-ips"
ROUTES_SCRIPT = f"""\
#!/bin/sh
# tundravm: accept what GCP's passthrough load balancers and protocol forwarding deliver.
# Each NIC's forwarded IPs become local routes (proto 66, as Google's guest agent marks
# them); a route whose IP is no longer forwarded is removed.
set -eu
md="{METADATA}/network-interfaces"
get() {{ {_CURL} "$md/$1"; }}
for nic in $(get ""); do
    nic=${{nic%/}}
    mac=$(get "$nic/mac")
    dev=
    for link in /sys/class/net/*; do
        [ "$(cat "$link/address")" = "$mac" ] && dev=${{link##*/}}
    done
    [ -n "$dev" ] || continue
    wanted=
    for index in $(get "$nic/forwarded-ips/"); do
        ip=$(get "$nic/forwarded-ips/$index")
        case $ip in
            *[!0-9.]*|"") continue ;;
        esac
        wanted="$wanted $ip"
        ip route replace local "$ip" dev "$dev" proto 66 scope host table local
    done
    ip route show table local dev "$dev" proto 66 | while read -r _ ip _; do
        case " $wanted " in
            *" $ip "*) ;;
            *) ip route del local "$ip" dev "$dev" proto 66 table local ;;
        esac
    done
done
"""


def metadata_units() -> tuple[Service, Task]:
    """The GCP units that read the metadata server, each declaring it in ``egress=``."""
    return (hostname_service(), forwarded_ips_task())


def forwarded_ips_task() -> Task:
    """``gcp-forwarded-ips``: a root task on a one-minute timer, both gated by
    ``ConditionVirtualization=google``, that keeps :data:`ROUTES_SCRIPT`'s routes in step
    with the load balancers forwarding to the instance (Google's guest agent does this on
    a stock image; a tundravm image runs no agent). Its own group is the one the firewall
    lets reach the metadata server."""
    from tundravm.declarative.model import EgressRule, Every, Task
    from tundravm.declarative.systemd import Condition, Hardening, Root

    return Task(
        "gcp-forwarded-ips",
        (ROUTES_SCRIPT_PATH,),
        schedule=Every("1min", first="10s"),
        description="GCP forwarded-IP routes",
        user=Root(),
        group="gcp-forwarded-ips",
        egress=(EgressRule(host=METADATA_SERVER, origins=(("gcp", ""),)),),
        timeout="1min",
        after=("network-online.target",),
        wants=("network-online.target",),
        needs=(),
        hardening=Hardening.relaxed(),
        conditions=(Condition.virtualization("google"),),
    )
