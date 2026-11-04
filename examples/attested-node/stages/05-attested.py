"""attested-node: the attested ingress, stage 5 of the guide.

Adds attested TLS on 8546 in front of the signer: the one port the firewall opens, and
the only way requests reach it. The signer asks the evidence service for a quote over its
key, checks it at start and serves it on GET /quote. Egress stays filtered: the baseline,
Nethermind's and Lighthouse's peers and the checkpoint host, nothing else.
"""

from components.lighthouse import Lighthouse
from components.nethermind import Nethermind

from tundravm import Debian, Image, RootFS, Service
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Network
from tundravm.ports import AttestedTLS
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
program = GoBinary("signer", Local("../signer"))
signer = Service(
    "signer",
    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc, "-quotes"],
    credentials=[InstanceKey()],
    quotes=True,
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
    ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
    network=Network(egress=Egress()),
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
