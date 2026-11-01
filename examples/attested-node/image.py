"""Attested node: Nethermind, and its block responses signed by a key this boot generated.

Nethermind follows Hoodi, the youngest public testnet it ships a configuration for: the
smallest state an Ethereum node syncs with a consensus client, which drives it over the
Engine API on the loopback with ``jwt``, generated at boot. ``signer/`` signs each block
response with the instance key; requests reach it only through the attested TLS ingress.

    tundravm check examples/attested-node/image.py --tree examples/attested-node/mkosi
"""

from tundravm import Image, RootFS, Service
from tundravm.components import DevAccess, Egress, Nethermind, Network
from tundravm.ports import AttestedTLS
from tundravm.secrets import Generated, InstanceKey
from tundravm.sources import GoBinary, Local
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import TcpProbe

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
signer = Service(
    "signer",
    [GoBinary("signer", Local("signer")), "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc],
    credentials=[InstanceKey()],
    requires=[nethermind.rpc],
    readiness=TcpProbe(8547),
)

image = Image(
    "attested-node",
    components=[nethermind],
    services=[signer],
    ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
    network=Network(egress=Egress()),
    rootfs=RootFS.verity(),
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
