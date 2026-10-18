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
    Declaration,
    File,
    Firewall,
    Listen,
    Package,
    Port,
    Secrets,
    Service,
    Unit,
)

NFTABLES_CONF = "/etc/nftables.conf"
"""The ruleset ``nftables.service`` loads at boot (``nft -f``), before the network is up."""

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
    """Port -> what in *items* listens on it, in declaration order: services, and ``Secrets``,
    whose boot step takes root's SSH key over HTTP whatever delivers the other secrets."""
    found: dict[Port, list[str]] = {}
    for item in items:
        if isinstance(item, Service):
            for port in service_ports(item):
                found.setdefault(port, []).append(unit_name(item.name))
        elif isinstance(item, Secrets) and not _loopback(item.host):
            found.setdefault(Port(item.port), []).append(f"{item.name} SSH key")
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
    return (
        "#!/usr/sbin/nft -f\n"
        "# tundravm Firewall: inbound traffic is dropped unless a rule below accepts it;\n"
        "# outbound traffic is not filtered.\n"
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
        "\t\ttype filter hook output priority filter; policy accept;\n"
        "\t}\n"
        "}\n"
    )


def expand_firewall(firewall: Firewall, items: Iterable[Declaration]) -> tuple[Declaration, ...]:
    """The declarations *firewall* stands for in a variant declaring *items*."""
    return (
        Package("nftables"),
        File.text(NFTABLES_CONF, nftables_conf(firewall, items), mode=0o755),
        Unit("nftables.service", enabled=True),
    )
