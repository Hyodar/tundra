"""attested-node: the signer, stage 4 of the guide.

Adds the signer, the Go program in signer/, built from source and run as a sandboxed
service. It forwards JSON-RPC to Nethermind and signs block responses with the key this
boot generated, which it reads as a credential. A signature covers the request's method and
params, the result and the nonce the caller sends in X-Tundra-Nonce, in the message
signer/main.go defines. It listens on the loopback only.
"""

from components.lighthouse import Lighthouse
from components.nethermind import Nethermind

from tundravm import Debian, Image, RootFS, Service
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Network
from tundravm.secrets import Generated, InstanceKey
from tundravm.sources import GoBinary, Local
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import Resources, TcpProbe

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
lighthouse = Lighthouse(
    execution=nethermind.engine_api,
    jwt=jwt,
    checkpoint_sync_url="https://checkpoint-sync.hoodi.ethpandaops.io",
    state=data.directory("lighthouse"),
)
program = GoBinary(Local("../signer"))
signer = Service(
    "signer",
    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc],
    credentials=[InstanceKey()],
    requires=[nethermind.rpc],
    readiness=TcpProbe(8547),
    resources=Resources(memory_max="256M"),
    hardening="static",
)

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    components=[nethermind, lighthouse],
    services=[signer],
    network=Network(egress=Egress()),
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
