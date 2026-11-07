"""flashbox: flashbots-images' flashbox-l1, the BoB searcher sandbox, as one image.

The image runs an L1 node, Nethermind and Lighthouse on mainnet, beside the searcher's
container. Both keep their state on a volume that only the searcher's key opens. The
firewall boots into production mode. In maintenance mode it also opens the container's
SSH. The searcher switches between the two modes, and every switch lands in RTMR3. Only
recorded health booleans leave the image, signed for Amazon Managed Prometheus.

The execution client runs on the host, not in the container as in flashbox: the engine
API's jwt cannot be bound into a container, so only a host client can share it with
Lighthouse. The searcher reads the client's RPC at the same loopback address.
"""

from components.lighthouse import Lighthouse
from components.nethermind import Nethermind

from tundravm import (
    Debian,
    DebianConfig,
    File,
    Image,
    Kernel,
    KernelFragment,
    RootFS,
    Service,
)
from tundravm.components.access import DevAccess, Login, SshAccess, Sudo
from tundravm.components.container import Bind, Container, ContainerSsh
from tundravm.components.network import Egress, Firewall, Mode, Network
from tundravm.components.observability import (
    Observability,
    PrometheusRemoteWrite,
    Proxy,
    RecordingRule,
    SigV4,
)
from tundravm.ports import TCP, UDP
from tundravm.secrets import Generated, Secret
from tundravm.sources import Git, GoBinary, Local
from tundravm.storage import OperatorKey, Volume

SSH_PORT = 10022
INPUT_UDP = 27017
INPUT_TCP = 27018
DELAY = 300

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
nethermind = Nethermind(jwt=jwt, config="mainnet", state=data.directory("nethermind"))
lighthouse = Lighthouse(
    execution=nethermind.engine_api,
    jwt=jwt,
    network="mainnet",
    checkpoint_sync_url="https://mainnet.checkpoint.sigp.io",
    state=data.directory("lighthouse"),
    expose_p2p=True,
    disable_quic=True,
    disable_optimistic_finalized_sync=True,
    state_cache_size=32,
)

BUNDLES = ["18.221.59.61:443", "3.15.88.156:443"]
TX_STREAM = ["3.136.107.142:443", "3.149.14.12:443"]
BUILDERNET = {
    "200.225.47.181": "fb-om-use1-00.nodes.buildernet.org direct-us.buildernet.org",
    "200.225.47.183": "fb-om-use1-01.nodes.buildernet.org direct-us.buildernet.org",
    "198.203.202.53": "fb-om-euw1-00.nodes.buildernet.org direct-eu.buildernet.org",
    "198.203.202.55": "fb-om-euw1-01.nodes.buildernet.org direct-eu.buildernet.org",
    "34.104.157.101": "fb-gcp-ane1-00.nodes.buildernet.org direct-ap.buildernet.org",
    "35.213.62.127": "fb-gcp-ane1-01.nodes.buildernet.org direct-ap.buildernet.org",
}
HOSTS = "\n".join(
    [
        "127.0.0.1 localhost",
        "3.149.14.12 tx.tee-searcher.flashbots.net",
        "3.136.107.142 tx.tee-searcher.flashbots.net",
        "18.221.59.61 backruns.tee-searcher.flashbots.net",
        "3.15.88.156 backruns.tee-searcher.flashbots.net",
        *(f"{address} {names} rpc.buildernet.org" for address, names in BUILDERNET.items()),
        "",
    ]
)

input_dir = data.directory("input", mode=0o755)
delayed = data.directory("delayed")
searcher_state = data.directory("searcher")
searcher = Container(
    "searcher-container",
    packages=["ca-certificates", "curl", "socat"],
    files=[File.text("/etc/hosts", HOSTS)],
    state=searcher_state,
    ssh=ContainerSsh(port=SSH_PORT, keys=Secret("searcher-keys")),
    expose=[UDP(INPUT_UDP)],
    egress=Egress(hosts=BUNDLES),
    modes={
        "production": Egress(hosts=[*TX_STREAM, *(f"{a}:443" for a in BUILDERNET)]),
        "maintenance": Egress(
            ports=[TCP(53), UDP(53), TCP(80), TCP(443), TCP(853)],
        ),
    },
    requires=[nethermind.rpc],
    binds=[Bind(input_dir.path, writable=True)],
)

channels = Local("channels")
delay_pipe = Service(
    "delay-pipe",
    [
        GoBinary("delay-pipe", channels, package="./cmd/delay-pipe"),
        searcher_state / "bob.log",
        delayed / "output.log",
        str(DELAY),
    ],
    user=searcher.run_user,
    state=delayed,
)
input_proxy = Service(
    "input-proxy",
    [
        GoBinary("input-proxy", channels, package="./cmd/input-proxy"),
        f"0.0.0.0:{INPUT_TCP}",
        input_dir / "input.sock",
    ],
    user=searcher.run_user,
    state=input_dir,
    expose=[TCP(INPUT_TCP)],
)

firewall = Firewall(
    modes={
        "production": Mode(),
        "maintenance": Mode(ingress=[TCP(SSH_PORT), TCP(30303), UDP(30303)]),
    }
)
ssh = SshAccess(
    login=Login(
        "searcher",
        owner=True,
        commands={
            "status": firewall.status,
            "logs": Sudo(
                ["/usr/bin/tail", "--lines", "{lines:int:1-10000}", delayed / "output.log"]
            ),
            "toggle": firewall.switch,
            "reboot": Sudo(["/usr/bin/systemctl", "reboot"]),
        },
    )
)

RULES = [
    RecordingRule(
        "flashbox:container_alive", 'max(tundravm_unit_active{unit="searcher-container.service"})'
    ),
    RecordingRule(
        "local:container_cpu_ratio",
        'sum(rate(tundravm_unit_cpu_seconds_total{unit="searcher-container.service"}[5m]))'
        ' / count(node_cpu_seconds_total{mode="idle"})',
    ),
    RecordingRule(
        "flashbox:container_average_cpu_is_under_80_percent",
        "(avg_over_time(local:container_cpu_ratio[15m]) < bool 0.8)"
        " * (max_over_time(local:container_cpu_ratio[10m] offset 5m) < bool 0.7)",
    ),
    RecordingRule("flashbox:container_oom_kills_count", "node_vmstat_oom_kill"),
    RecordingRule(
        "flashbox:disk_free_space_is_over_10_percent",
        '(node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})'
        " > bool 0.1",
    ),
    RecordingRule(
        "flashbox:disk_free_space_is_over_128_gb",
        f'node_filesystem_avail_bytes{{mountpoint="{data.mount}"}} > bool {128 * 1024**3}',
    ),
    RecordingRule(
        "flashbox:network_is_up",
        '(sum(rate(node_network_receive_bytes_total{device!~"lo"}[5m]))'
        ' + sum(rate(node_network_transmit_bytes_total{device!~"lo"}[5m]))) > bool 0',
    ),
]
EXPORT = [rule.record for rule in RULES if rule.record.startswith("flashbox:")]
metrics = PrometheusRemoteWrite(
    url="https://metrics.flashbox.example/api/v1/write",
    credential=SigV4("us-east-2", Secret("metrics-aws")),
    rules=RULES,
    export=EXPORT,
)

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
            {
                "CONFIG_DEFAULT_FQ": "y",
                "CONFIG_TCP_CONG_BBR": "y",
                "CONFIG_DEFAULT_BBR": "y",
            },
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
    services=[delay_pipe, input_proxy],
    network=Network(egress=Egress()),
    kernel=kernel,
    rootfs=RootFS.verity(),
    observability=Observability(metrics=metrics, proxy=Proxy("10.88.0.100", 443, passthrough=True)),
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
