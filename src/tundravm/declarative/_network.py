"""What :class:`Firewall` and ``Network`` render: an nftables ruleset and systemd-networkd files.

Resolution expands a ``Firewall`` declaration with :func:`expand_firewall`, after every level
has been applied, so the ruleset opens the ports of the variant's final services.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Literal

from tundravm.models import unit_name

from .model import (
    CLOUD_METADATA,
    Declaration,
    EgressRule,
    File,
    Firewall,
    Listen,
    Package,
    Port,
    Secrets,
    Service,
    Task,
    Unit,
    egress_owner,
)

NFTABLES_CONF = "/etc/nftables.conf"
"""The ruleset ``nftables.service`` loads at boot (``nft -f``), before the network is up."""

NETWORKD_FIREWALL_DROPIN = (
    "/usr/lib/systemd/system/systemd-networkd.service.d/50-tundravm-firewall.conf"
)
"""The drop-in tying systemd-networkd to ``nftables.service``."""

NETWORKD_FIREWALL_TEXT = (
    "# tundravm Firewall: no interface comes up unless the ruleset loaded\n"
    "[Unit]\nRequires=nftables.service\nAfter=nftables.service\n"
)
"""networkd (and ``network-online.target`` through wait-online) fails with the firewall."""

NETWORK_DIR = "/etc/systemd/network"
"""Where ``Network`` writes its ``.network`` file."""

NETWORKD_DIRS = (
    "/etc/systemd/network",
    "/run/systemd/network",
    "/usr/local/lib/systemd/network",
    "/usr/lib/systemd/network",
)
"""Where systemd-networkd reads ``.network`` files (``systemd.network(5)``)."""

NETWORK_CONFIG_PACKAGES = frozenset(
    {"ifupdown", "netplan.io", "network-manager", "cloud-init", "dhcpcd", "dhcpcd-base"}
)
"""Packages that configure interfaces themselves: a variant installing one has a network."""

ICMPV6_ESSENTIAL = (
    "destination-unreachable",
    "packet-too-big",
    "time-exceeded",
    "parameter-problem",
    "nd-router-advert",
    "nd-neighbor-solicit",
    "nd-neighbor-advert",
)
"""ICMPv6 types IPv6 needs inbound (RFC 4890 4.4.1): errors, path MTU, neighbor discovery."""

_COMMENT_MAX = 120
"""nft keeps at most 128 bytes of a rule comment."""


def _loopback(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def listen_port(listen: Listen) -> Port | None:
    """The port *listen* opens to the network: ``None`` for a socket file or a loopback host."""
    if listen.is_path:
        return None
    protocol: Literal["tcp", "udp"] = "tcp" if listen.kind == "stream" else "udp"
    if isinstance(listen.address, int):
        return Port(listen.address, protocol)
    host, _, port = listen.address.rpartition(":")
    return None if _loopback(host) else Port(int(port), protocol)


def service_ports(service: Service) -> tuple[Port, ...]:
    """The ports *service* listens on beyond loopback: its socket's, then its ``ports``."""
    socket = None if service.socket is None else listen_port(service.socket)
    found = (socket, *service.ports) if socket is not None else service.ports
    return tuple(dict.fromkeys(port for port in found if isinstance(port, Port)))


def listening(items: Iterable[Declaration]) -> dict[Port, list[str]]:
    """Port -> what in *items* listens on it, in declaration order: services, enabled units
    naming ``ports``, and ``Secrets``, whose attested endpoint serves evidence and takes the
    operator's SSH key whatever delivers the other secrets."""
    found: dict[Port, list[str]] = {}
    for item in items:
        if isinstance(item, Service):
            for port in service_ports(item):
                found.setdefault(port, []).append(unit_name(item.name))
        elif isinstance(item, Unit) and item.enabled and not item.masked:
            for opened in item.ports:
                if isinstance(opened, Port):
                    found.setdefault(opened, []).append(unit_name(item.name))
        elif isinstance(item, Secrets) and not _loopback(item.host):
            found.setdefault(Port(item.port), []).append("attested-endpoint")
    return found


def allowed_ports(firewall: Firewall, items: Iterable[Declaration]) -> dict[Port, list[str]]:
    """Port -> why *firewall* opens it (``allow``, ``ssh``, the listening units)."""
    opened: dict[Port, list[str]] = {}
    for port in firewall.allow:
        if isinstance(port, Port):
            opened.setdefault(port, []).append("allow")
    if firewall.ssh:
        opened.setdefault(Port(22), []).append("ssh")
    if firewall.collect:
        for port, units in listening(items).items():
            opened.setdefault(port, []).extend(units)
    return dict(sorted(opened.items(), key=lambda entry: (entry[0].number, entry[0].protocol)))


def _comment(reasons: list[str]) -> str:
    text = ", ".join(dict.fromkeys(reasons))
    return text if len(text) <= _COMMENT_MAX else text[: _COMMENT_MAX - 3] + "..."


def nftables_conf(firewall: Firewall, items: Iterable[Declaration]) -> str:
    """The ``/etc/nftables.conf`` *firewall* renders for a variant declaring *items*."""
    rules = [
        "ct state established,related accept",
        "ct state invalid drop",
        'iifname "lo" accept',
        f"icmpv6 type {{ {', '.join(ICMPV6_ESSENTIAL)} }} accept",
        'udp sport 67 udp dport 68 accept comment "DHCPv4 replies"',
        'ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept comment "DHCPv6 replies"',
    ]
    for port, reasons in allowed_ports(firewall, items).items():
        rules.append(f'{port.protocol} dport {port.number} accept comment "{_comment(reasons)}"')
    if firewall.default == "reject":
        rules += [
            "meta l4proto tcp reject with tcp reset",
            "reject with icmpx type port-unreachable",
        ]
    body = "".join(f"\t\t{rule}\n" for rule in rules)
    filtered = firewall.egress == "filter"
    outbound = "".join(f"\t\t{rule}\n" for rule in _output_rules(firewall, items))
    return (
        "#!/usr/sbin/nft -f\n"
        "# tundravm Firewall: inbound traffic is dropped unless a rule below accepts it;\n"
        + (
            "# outbound traffic is dropped unless a rule in the output chain accepts it.\n"
            if filtered
            else "# outbound traffic is not filtered.\n"
        )
        + "# The cloud metadata servers take only what the units that declare them send.\n"
        "flush ruleset\n\n"
        "table inet filter {\n"
        "\tchain input {\n"
        "\t\ttype filter hook input priority filter; policy drop;\n"
        f"{body}"
        "\t}\n"
        "\tchain forward {\n"
        "\t\ttype filter hook forward priority filter; policy drop;\n"
        "\t}\n"
        "\tchain output {\n"
        f"\t\ttype filter hook output priority filter; policy {'drop' if filtered else 'accept'};\n"
        f"{outbound}"
        "\t}\n"
        "}\n"
    )


METADATA_SHARED = (EgressRule(Port(123, "udp"), CLOUD_METADATA[0], (("metadata time", ""),)),)
"""What every process may send the metadata server: its NTP, which ``chrony`` refines the
NTS time from, and which carries no identity token. Its plain DNS is not: ``TeeBase``
resolves over TLS only."""


def unit_egress(items: Iterable[Declaration]) -> list[tuple[str, str, EgressRule]]:
    """``(unit, owner match, rule)`` of every ``Service``'s and ``Task``'s own egress in
    *items*, in declaration order."""
    found: list[tuple[str, str, EgressRule]] = []
    for item in items:
        if isinstance(item, (Service, Task)) and item.egress:
            user, group = egress_owner(item)
            owner = (
                f'meta skuid "{user}"' if group is None else f'meta skuid 0 meta skgid "{group}"'
            )
            found += [(unit_name(item.name), owner, rule) for rule in item.egress]
    return found


def _output_rules(firewall: Firewall, items: Iterable[Declaration]) -> list[str]:
    """The output chain: with ``egress="filter"`` the baseline accepts, then the cloud
    metadata servers for the units that declare them and dropped for the rest, then each
    unit's other egress and *firewall*'s rules; unfiltered, the metadata part alone."""
    filtered = firewall.egress == "filter"
    rules = (
        [
            "ct state established,related accept",
            "ct state invalid drop",
            'oifname "lo" accept',
            f"icmpv6 type {{ {', '.join(ICMPV6_ESSENTIAL)} }} accept",
            'udp sport 68 udp dport 67 accept comment "DHCPv4 requests"',
            'udp sport 546 udp dport 547 accept comment "DHCPv6 requests"',
        ]
        if filtered
        else []
    )
    own = unit_egress(items)
    for unit, owner, rule in own:
        if rule.host in CLOUD_METADATA:
            rules.append(f'{owner} {egress_match(rule)} accept comment "{unit}"')
    for rule in METADATA_SHARED:
        rules.append(f'{egress_match(rule)} accept comment "{rule.comment}"')
    for family in ("ip", "ip6"):
        servers = sorted(host for host in CLOUD_METADATA if _family(host) == family)
        spelled = servers[0] if len(servers) == 1 else "{ " + ", ".join(servers) + " }"
        rules.append(f'{family} daddr {spelled} drop comment "cloud metadata"')
    if not filtered:
        return rules
    for unit, owner, rule in own:
        if rule.host not in CLOUD_METADATA:
            rules.append(f'{owner} {egress_match(rule)} accept comment "{unit}"')
    anywhere = [rule for rule in firewall.egress_rules if rule.host is None]
    hosts = [rule for rule in firewall.egress_rules if rule.host is not None]
    for rule in [*sorted(anywhere, key=_port_order), *hosts]:
        comment = _comment([rule.comment]) if rule.origins else "egress"
        rules.append(f'{egress_match(rule)} accept comment "{comment}"')
    return rules


def egress_match(rule: EgressRule) -> str:
    """*rule*'s match in the output chain, as ``nft list ruleset`` prints it back."""
    match = [] if rule.host is None else [f"{_family(rule.host)} daddr {rule.host}"]
    if rule.port is not None:
        match.append(f"{rule.port.protocol} dport {rule.port.number}")
    return " ".join(match)


def _family(host: str) -> str:
    return "ip6" if ":" in host else "ip"


def _port_order(rule: EgressRule) -> tuple[int, str]:
    assert rule.port is not None
    return rule.port.number, rule.port.protocol


def expand_firewall(firewall: Firewall, items: Iterable[Declaration]) -> tuple[Declaration, ...]:
    """The declarations *firewall* stands for in a variant declaring *items*: with an enabled
    ``systemd-networkd`` (``Network``), :data:`NETWORKD_FIREWALL_DROPIN` too."""
    items = tuple(items)
    rendered: tuple[Declaration, ...] = (
        Package("nftables"),
        File.text(NFTABLES_CONF, nftables_conf(firewall, items), mode=0o755),
        Unit("nftables.service", enabled=True),
    )
    networkd = any(
        isinstance(item, Unit) and item.name == "systemd-networkd.service" and item.enabled
        for item in items
    )
    if networkd:
        rendered += (File.text(NETWORKD_FIREWALL_DROPIN, NETWORKD_FIREWALL_TEXT),)
    return rendered
