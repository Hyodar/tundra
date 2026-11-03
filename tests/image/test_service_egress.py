"""``Service(egress=)``: outbound rules matched by the service's user, and the cloud metadata
servers reachable only by the services (and the cloud glue) that declare them."""

from __future__ import annotations

import pytest

from tundravm import Image, Service
from tundravm.components import Egress, Network
from tundravm.declarative import Dynamic, Task
from tundravm.declarative.model import EgressRule
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.testing import plan

APP = Service("app", ["/usr/bin/app"], egress=Egress(hosts=["10.0.0.5:443"]))
READER = Service("reader", ["/usr/bin/reader"], egress=Egress(metadata=True))
PLAIN = Service("plain", ["/usr/bin/plain"])
DROP = 'ip daddr { 168.63.129.16, 169.254.169.254 } drop comment "cloud metadata"'


def _chain(image: Image) -> list[str]:
    text = plan(image, "production").file("/etc/nftables.conf").text
    return [line.strip() for line in text.partition("chain output")[2].splitlines()]


@pytest.mark.parametrize(
    ("network", "present", "absent"),
    [
        (
            Network(egress=Egress(ports=[TCP(53)])),
            [
                'meta skuid "app" ip daddr 10.0.0.5 tcp dport 443 accept comment "app.service"',
                'meta skuid "reader" ip daddr 169.254.169.254 accept comment "reader.service"',
                'meta skuid "reader" ip6 daddr fd20:ce::254 accept comment "reader.service"',
            ],
            ['meta skuid "plain"'],
        ),
        (
            None,
            ['meta skuid "reader" ip daddr 168.63.129.16 accept comment "reader.service"'],
            ['meta skuid "app"', "tcp dport 443"],
        ),
    ],
)
def test_a_services_egress_is_its_users_alone(
    network: Network | None, present: list[str], absent: list[str]
) -> None:
    chain = _chain(Image("x", services=[APP, READER, PLAIN], network=network or Network()))
    for rule in present:
        assert rule in chain
        if "169.254" in rule or "168.63" in rule:
            assert chain.index(rule) < chain.index(DROP)
    assert not [line for line in chain for text in absent if text in line]
    assert 'ip daddr 169.254.169.254 udp dport 53 accept comment "metadata DNS"' in chain


def test_the_cloud_glue_alone_reaches_the_metadata_server() -> None:
    chain = _chain(Image("x", services=[PLAIN], targets=("qemu", "gcp", "azure")))
    owners = [line.partition(" ip")[0] for line in chain[: chain.index(DROP)] if "skuid" in line]
    assert owners == [
        'meta skuid 0 meta skgid "azure-complete-provisioning"',
        'meta skuid 0 meta skgid "gcp-hostname"',
        'meta skuid 0 meta skgid "gcp-forwarded-ips"',
    ]


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda: Egress(hosts=["169.254.169.254"]), "a cloud metadata server"),
        (lambda: Network(egress=Egress(metadata=True)), "every process of the image"),
        (lambda: Service("a", ["/a"], user="root", egress=Egress()), "every root process"),
        (
            lambda: Task("t", "/t", user=Dynamic(), egress=(EgressRule(host="10.0.0.5"),)),
            "transient user",
        ),
    ],
)
def test_egress_the_firewall_cannot_scope_is_refused(make: object, says: str) -> None:
    with pytest.raises(ValidationError, match=says):
        make()  # type: ignore[operator]


def test_a_service_reading_the_metadata_server_is_linted() -> None:
    found = plan(Image("x", services=[READER, PLAIN])).lint()
    assert [(d.code, d.subject) for d in found if d.code == "service-metadata"] == [
        ("service-metadata", "reader.service")
    ]
