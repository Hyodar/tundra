"""``Network``, ``Firewall`` and the lint that checks a variant can be reached."""

from __future__ import annotations

import pytest

from tundravm.declarative import (
    File,
    Firewall,
    Fragment,
    Listen,
    Package,
    Port,
    Recipe,
    Secrets,
    Service,
    Unit,
    Variant,
    compile,
    resolve,
)
from tundravm.declarative.resolve import lint
from tundravm.declarative.utils import NETWORK_FILE, Network, StaticAddress
from tundravm.errors import ValidationError
from tundravm.kernel_modules import FIREWALL_MODULES

KERNEL = Package("linux-image-amd64")


def _recipe(*items: object) -> Recipe:
    return Recipe("net", items=(KERNEL, *items), variants=(Variant("default", outputs=("qemu",)),))  # type: ignore[arg-type]


def _codes(*items: object) -> list[str]:
    return [d.code for d in lint(_recipe(*items))]


def _ruleset(*items: object) -> str:
    resolved = resolve(_recipe(*items), variant="default")
    [conf] = [i for i in resolved.items if isinstance(i, File) and i.path == "/etc/nftables.conf"]
    return str(conf.content)


def test_network_defaults_to_dhcp_on_every_nic_and_waits_with_a_timeout() -> None:
    items = list(Network().compose())
    [network] = [i for i in items if isinstance(i, File)]
    assert network.path == NETWORK_FILE == "/etc/systemd/network/50-tundravm.network"
    text = str(network.content)
    assert "[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\nIPv6AcceptRA=yes\n" in text
    assert "UseHostname=no\nUseDNS=no\nUseNTP=no\n" in text
    units = {i.name: i for i in items if isinstance(i, Unit)}
    assert all(unit.enabled for unit in units.values())
    wait = units["systemd-networkd-wait-online.service"].dropin
    assert wait is not None and wait.exec_start == (
        "/usr/lib/systemd/systemd-networkd-wait-online",
        "--any",
        "--timeout=30",
    )
    assert set(units) == {
        "systemd-networkd.service",
        "systemd-networkd.socket",
        "systemd-networkd-wait-online.service",
    }
    off = {i.name: i.enabled for i in Network(wait_online=False).compose() if isinstance(i, Unit)}
    assert off["systemd-networkd-wait-online.service"] is False


def test_network_static_and_ipv4_only() -> None:
    static = StaticAddress("10.0.0.5/24", gateway="10.0.0.1")
    text = Network(dhcp=False, static=static, ipv6=False).network_file()
    assert "DHCP=no\nAddress=10.0.0.5/24\nGateway=10.0.0.1\n" in text
    assert "IPv6AcceptRA=no\nLinkLocalAddressing=no\n" in text and "[DHCPv4]" not in text
    assert "DHCP=ipv4\n" in Network(ipv6=False).network_file()


@pytest.mark.parametrize(
    ("network", "match"),
    [
        (lambda: Network(static=StaticAddress("10.0.0.5/24")), "both are set"),
        (lambda: Network(dhcp=False), "neither is set"),
        (lambda: Network(interfaces=()), "interface names"),
        (lambda: Network(wait_timeout=0), "wait_timeout"),
        (lambda: Network(dhcp=False, static=StaticAddress("fd00::5/64"), ipv6=False), "IPv6"),
        (lambda: StaticAddress("10.0.0.5"), "prefix length"),
        (lambda: StaticAddress("10.0.0.5/24", gateway="fd00::1"), "IPv4"),
    ],
)
def test_network_rejects_contradictions(network: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        list(network().compose())  # type: ignore[operator]


def test_firewall_opens_the_ports_the_variant_declares() -> None:
    text = _ruleset(
        Network(),
        Service("app", "/usr/bin/app", ports=(8080, Port(9000, "udp"))),
        Service("web", "/usr/bin/web", socket=Listen("0.0.0.0:443")),
        Service("admin", "/usr/bin/admin", socket=Listen("127.0.0.1:9090")),
        Service("local", "/usr/bin/local", socket=Listen("/run/local.sock")),
        Secrets(),
        Firewall(allow=(Port(51820, "udp"),), ssh=True),
    )
    assert "type filter hook input priority filter; policy drop;" in text
    assert "ct state established,related accept" in text and 'iifname "lo" accept' in text
    assert "nd-neighbor-solicit" in text and "udp sport 67 udp dport 68 accept" in text
    opened = [
        line.strip() for line in text.splitlines() if " dport " in line and "sport" not in line
    ]
    assert opened == [
        'tcp dport 22 accept comment "ssh"',
        'tcp dport 443 accept comment "web.service"',
        'tcp dport 8080 accept comment "app.service, secrets SSH key"',
        'udp dport 9000 accept comment "app.service"',
        'udp dport 51820 accept comment "allow"',
    ]
    assert "type filter hook output priority filter; policy accept;" in text
    assert "reject" not in text
    assert "reject with tcp reset" in _ruleset(Network(), Firewall(default="reject"))


def test_firewall_expands_into_a_file_a_package_and_an_enabled_unit() -> None:
    resolved = resolve(_recipe(Network(), Firewall()), variant="default")
    assert Package("nftables") in resolved.items
    assert Unit("nftables.service", enabled=True) in resolved.items
    assert not any(isinstance(i, Firewall) for i in resolved.items)
    clash = File("/etc/nftables.conf", "flush ruleset\n")
    assert "identity-collision" in _codes(Network(), Firewall(), clash)


def test_port_unreachable_when_the_firewall_does_not_collect() -> None:
    app = Service("app", "/usr/bin/app", ports=(8080,))
    assert _codes(Network(), app, Firewall()) == []
    [found] = lint(_recipe(Network(), app, Firewall(collect=False)))
    assert (found.code, found.severity, found.subject) == (
        "port-unreachable",
        "warning",
        "app.service",
    )
    assert "8080/tcp" in found.message
    assert _codes(Network(), app, Firewall(allow=(8080,), collect=False)) == []


def test_network_missing_for_listeners_and_time_sync_without_an_interface() -> None:
    app = Service("app", "/usr/bin/app", ports=(8080,))
    [found] = lint(_recipe(app))
    assert (found.code, found.severity) == ("network-missing", "warning")
    assert "app.service listens on 8080/tcp" in found.message and "Network()" in (found.hint or "")
    assert _codes(Package("chrony")) == ["network-missing"]
    assert _codes(Service("admin", "/usr/bin/a", socket=Listen("127.0.0.1:9090"))) == []
    for network in (
        Network(),
        File("/etc/systemd/network/10-eth.network", "[Match]\nName=eth0\n"),
        Unit("network-setup.service", "[Service]\nExecStart=/usr/bin/true\n", enabled=True),
        Package("ifupdown"),
    ):
        assert _codes(app, network) == [], network


def test_a_firewall_keeps_the_nftables_kernel_modules() -> None:
    def kept(*items: object) -> str:
        tree = compile(_recipe(Network(), *items))
        [conf] = [e for e in tree.entries if e.path == "default/mkosi.conf"]
        return (conf.content or b"").decode()

    assert "nf_tables" not in kept()
    conf = kept(Firewall())
    assert all(module in conf for module in FIREWALL_MODULES)


def test_a_variant_opens_the_ports_it_adds_and_edits_the_firewall() -> None:
    debug = Fragment("debug", (Service("dbg", "/usr/bin/dbg", ports=(9999,)),))
    dev = Variant("dev", inherits="default", add=debug, changes=(Firewall.ref().edit(ssh=True),))
    recipe = Recipe(
        "net",
        items=(KERNEL, Network(), Firewall()),
        variants=(Variant("default", outputs=("qemu",)), dev),
    )
    tree = {e.path: (e.content or b"").decode() for e in compile(recipe).entries}
    assert " dport 9999 " not in tree["default/mkosi.extra/etc/nftables.conf"]
    rules = tree["dev/mkosi.extra/etc/nftables.conf"]
    assert 'tcp dport 22 accept comment "ssh"' in rules
    assert 'tcp dport 9999 accept comment "dbg.service"' in rules
