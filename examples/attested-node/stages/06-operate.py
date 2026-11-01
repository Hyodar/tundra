"""attested-node: operating it, stage 6 of the guide and image.py.

Adds what running it takes: the journal shipped (to an in-guest receiver until url= names
the deployment's, which then takes a delivered client certificate), Lighthouse's sync
status in it every hour, a staging profile that edits one file, a bench profile that is
never shipped, and the Azure and GCP disks of the same payload. The dev profile's boot
probe checks it all.
"""

from components.lighthouse import Lighthouse
from components.nethermind import Nethermind

from tundravm import Debian, File, Image, Profile, RootFS, Service
from tundravm.components import DevAccess, Egress, JournalExport, Network, Observability
from tundravm.ports import AttestedTLS
from tundravm.secrets import Generated, InstanceKey, Secret
from tundravm.sources import GoBinary, Local
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import Every, Resources, TcpProbe

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
motd = File.text("/etc/motd", "attested-node: Nethermind and Lighthouse on Hoodi\n")
sync = Service(
    "sync-status",
    ["/usr/bin/curl", "-fsS", f"http://{lighthouse.http.address}/eth/v1/node/syncing"],
    requires=[lighthouse.http],
    timer=Every("1h"),
)
logs = JournalExport(credential=Secret("logs-client"))

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates", "curl"],
    files=[motd],
    components=[nethermind, lighthouse],
    services=[signer, sync],
    ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
    network=Network(egress=Egress()),
    rootfs=RootFS.verity(),
    observability=Observability(logs=logs),
    targets=("qemu", "azure", "gcp"),
    profiles={
        "staging": Profile(changes=[motd.ref.edit(content="attested-node: staging\n")]),
        "bench": Profile(add=["fio"], production=False),
    },
    dev=DevAccess(password="tdx"),
)
