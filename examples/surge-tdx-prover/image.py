"""Surge TDX prover: Nethermind, the taiko driver and Raiko, proving inside a TD.

The workloads come from ``tundravm.components``, which pins their sources and
toolchains. Their state lives on a volume an operator unlocks after attesting the
instance; the Engine API secret, the driver's L1 configuration and Raiko's configuration
are delivered the same way and never built in. Requests reach Raiko only through the
attested TLS ingress. One verity-rooted payload boots on QEMU, Azure and GCP.

    tundravm compile examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi
"""

from tundravm import AttestedTLS, DevAccess, Image, OperatorKey, RootFS, Secret, Volume
from tundravm.components import Nethermind, Raiko, TaikoClient

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
