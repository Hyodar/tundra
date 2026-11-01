"""attested-node: Nethermind, stage 2 of the guide.

Adds Nethermind on Hoodi, a component this example writes (components/nethermind.py).
Its database lives on a LUKS2 volume an operator unlocks after attesting the instance,
its Engine API secret is generated at boot, and its build is large, so the root is a
read-only dm-verity disk.
"""

from components.nethermind import Nethermind

from tundravm import Debian, Image, RootFS
from tundravm.components import DevAccess, Egress, Network
from tundravm.secrets import Generated
from tundravm.storage import OperatorKey, Volume

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    components=[nethermind],
    network=Network(egress=Egress()),
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
