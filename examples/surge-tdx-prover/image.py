"""Surge TDX prover: Nethermind, the taiko driver and Raiko, proving inside a TD.

The workloads come from ``tundravm.components``, which pins their sources and
toolchains. Their state lives on a volume an operator unlocks after attesting the
instance; the Engine API secret, the driver's L1 configuration and Raiko's configuration
are delivered the same way and never built in. Requests reach Raiko only through the
attested TLS ingress. One verity-rooted payload boots on QEMU, Azure and GCP.

    tundravm build examples/surge-tdx-prover/image.py --tree-only examples/surge-tdx-prover/mkosi
"""

from tundravm import Image, RootFS
from tundravm.components import DevAccess, Nethermind, Raiko, TaikoClient
from tundravm.ports import AttestedTLS
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume

data = Volume("data", mount="/persistent", unlock=OperatorKey())
jwt = Secret("jwt")
chain = Secret("taiko-chain")
config = Secret("raiko-config")

execution = Nethermind(jwt=jwt, state=data.directory("nethermind"))
driver = TaikoClient(engine=execution.engine_api, rpc=execution.rpc, jwt=jwt, chain=chain)
prover = Raiko(config=config, l2_rpc=execution.rpc, state=data.directory("raiko"))

image = Image(
    "surge-tdx-prover",
    components=[execution, driver, prover],
    volumes=[data],
    secrets=[jwt, chain, config],
    ingress=[AttestedTLS(443, upstream=prover.http)],
    rootfs=RootFS.verity(),
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
