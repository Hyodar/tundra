"""attested-node: the boot, stage 1 of the guide.

Debian trixie from one snapshot, with systemd, the TEE baseline (NTS time, a volatile
journal, sysctl hardening), DHCP, a firewall that drops inbound traffic nothing exposes,
filtered egress, and a dev profile with a serial console and root password "tdx".
"""

from tundravm import Debian, Image
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Network

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    network=Network(egress=Egress()),
    dev=DevAccess(password="tdx"),
)
