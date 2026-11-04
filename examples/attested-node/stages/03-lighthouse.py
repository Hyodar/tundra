"""attested-node: Lighthouse, stage 3 of the guide.

Adds Lighthouse, the consensus client that drives Nethermind to the chain's head over
the Engine API with the same generated secret. It syncs from a checkpoint and keeps its
database beside Nethermind's (components/lighthouse.py).
"""

from components.lighthouse import Lighthouse
from components.nethermind import Nethermind

from tundravm import Debian, Image, RootFS
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Network
from tundravm.secrets import Generated
from tundravm.storage import OperatorKey, Volume

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
lighthouse = Lighthouse(
    execution=nethermind.engine_api,
    jwt=jwt,
    checkpoint_sync_url="https://checkpoint-sync.hoodi.ethpandaops.io",
    state=data.directory("lighthouse"),
)

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    components=[nethermind, lighthouse],
    network=Network(egress=Egress()),
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
