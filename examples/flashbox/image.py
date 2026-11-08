"""flashbox: flashbots-images' flashbox-l1, the BoB searcher sandbox, as one image.

An L1 node, Nethermind and Lighthouse on mainnet, runs beside the searcher's container,
and both keep their state on one volume whose unlock the searcher's key signs. The
firewall boots into production mode; maintenance also opens the container's SSH, and
every switch lands in RTMR3. What leaves the image is the records the health rules
export and the image's identity series, signed for Amazon Managed Prometheus.
"""

from components.health import health_rules
from components.lighthouse import Lighthouse
from components.nethermind import Nethermind
from components.searcher import Searcher

from tundravm import Debian, DebianConfig, Image, Kernel, KernelFragment, RootFS
from tundravm.components.access import Command, DevAccess, Int, Login, SshAccess, Sudo
from tundravm.components.network import Egress, Firewall, Mode, Network
from tundravm.components.observability import Observability, PrometheusRemoteWrite, Proxy, SigV4
from tundravm.secrets import Generated, Secret
from tundravm.sources import Git
from tundravm.storage import OperatorKey, Volume

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
lighthouse_logs = data.directory("lighthouse-logs")
nethermind = Nethermind(
    jwt=jwt, config="mainnet", state=data.directory("nethermind"), expose_p2p=True
)
lighthouse = Lighthouse(
    execution=nethermind.engine_api,
    jwt=jwt,
    network="mainnet",
    checkpoint_sync_url="https://mainnet.checkpoint.sigp.io",
    state=data.directory("lighthouse"),
    logs=lighthouse_logs,
    expose_p2p=True,
    disable_quic=True,
    disable_optimistic_finalized_sync=True,
    state_cache_size=32,
)
searcher = Searcher(
    rpc=nethermind.rpc,
    state=data.directory("searcher"),
    input=data.directory("input"),
    # The searcher's login reads the delayed log through its own group, without root.
    delayed=data.directory("delayed", group="searcher"),
    delay=300,
    logs_from=[lighthouse_logs],
)

firewall = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[searcher.ssh])})
ssh = SshAccess(
    login=Login(
        "searcher",
        owner=True,
        commands={
            "status": firewall.status,
            "logs": Command("/usr/bin/tail", "--lines", Int("lines", 1, 10000), searcher.logs),
            "toggle": firewall.switch,
            "reboot": Sudo("/usr/bin/systemctl", "reboot"),
            "shell": Command("/usr/bin/bash", dev=True),
        },
    )
)
metrics = PrometheusRemoteWrite(
    url="https://metrics.flashbox.example/api/v1/write",
    credential=SigV4("us-east-2", Secret("metrics-aws")),
    rules=health_rules(searcher, data),
)

# flashbots-images builds gregkh's 6.18.57 on the config of Debian's 6.18.15 backport;
# olddefconfig gives each option added since then its default.
kernel = Kernel.build(
    "6.18.57",
    Git("https://github.com/gregkh/linux", "2ff600d29ee478d0a8d46ef8a58afb5e038fe692"),
    base=DebianConfig(
        "6.18.15-1~bpo13+1",
        snapshot="20261003T143013Z",
        sha256="7008d38386b9c9552fc347e5ea3a6f85e4e8ce9333a9e4c6f09ad3055348d959",
    ),
    fragments=[
        KernelFragment(
            {"CONFIG_DEFAULT_FQ": "y", "CONFIG_TCP_CONG_BBR": "y", "CONFIG_DEFAULT_BBR": "y"},
            reason="fq paces TCP and BBR is flashbox's congestion control",
        )
    ],
    cmdline=["transparent_hugepage=madvise"],
)

image = Image(
    "flashbox",
    base=Debian("trixie", snapshot="20261018T143805Z"),
    sysctl={"vm.max_map_count": 2097152},
    components=[nethermind, lighthouse, searcher, ssh, firewall],
    network=Network(egress=Egress()),
    kernel=kernel,
    rootfs=RootFS.verity(),
    observability=Observability(metrics=metrics, proxy=Proxy("10.88.0.100", 443, passthrough=True)),
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
