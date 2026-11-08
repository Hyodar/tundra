"""Where the searcher sends: Flashbots' order flow, each peer pinned to its addresses.

``EGRESS`` is what the searcher reaches in every mode, and ``MODES`` what each firewall
mode adds: the transaction stream and BuilderNet in production, DNS, DNS over TLS and the
web in maintenance, when the searcher fetches what its bot needs but cannot trade, so the
web stops short of the transaction stream.
"""

from tundravm.components.network import Egress, Host
from tundravm.ports import TCP, UDP

TX_STREAM = Host("tx.tee-searcher.flashbots.net", ["3.149.14.12", "3.136.107.142"], port=443)
BUNDLES = Host("backruns.tee-searcher.flashbots.net", ["18.221.59.61", "3.15.88.156"], port=443)
BUILDERNET = [
    Host(
        f"{node}.nodes.buildernet.org", [address], port=443, aliases=[region, "rpc.buildernet.org"]
    )
    for node, address, region in [
        ("fb-om-use1-00", "200.225.47.181", "direct-us.buildernet.org"),
        ("fb-om-use1-01", "200.225.47.183", "direct-us.buildernet.org"),
        ("fb-om-euw1-00", "198.203.202.53", "direct-eu.buildernet.org"),
        ("fb-om-euw1-01", "198.203.202.55", "direct-eu.buildernet.org"),
        ("fb-gcp-ane1-00", "34.104.157.101", "direct-ap.buildernet.org"),
        ("fb-gcp-ane1-01", "35.213.62.127", "direct-ap.buildernet.org"),
    ]
]

EGRESS = Egress(hosts=[BUNDLES])
MODES = {
    "production": Egress(hosts=[TX_STREAM, *BUILDERNET]),
    "maintenance": Egress(ports=[TCP(53), UDP(53), TCP(80), TCP(443), TCP(853)], deny=[TX_STREAM]),
}
