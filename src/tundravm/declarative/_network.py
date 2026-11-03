"""What :class:`Firewall` and ``Network`` render: an nftables ruleset and systemd-networkd files.

Resolution expands a ``Firewall`` declaration with :func:`expand_firewall`, after every level
has been applied, so the ruleset opens the ports of the variant's final services.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from typing import Literal

from tundravm.errors import ValidationError
from tundravm.models import unit_name

from .model import (
    CLOUD_METADATA,
    Build,
    Declaration,
    EgressRule,
    File,
    Firewall,
    Go,
    Install,
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


MODES_CONFIG = "/etc/tundravm/modes.json"
"""What ``mode-switch`` and the attested endpoint read: the modes, their marks and spans."""
MODE_SWITCH = "/usr/bin/mode-switch"
MODE_SWITCH_UNIT = "mode-switch.service"
MODE_SWITCH_TEXT = """\
# tundravm Firewall modes: the switcher the attested endpoint hands a mode to. It extends
# RTMR3 with the switch, logs it in /run/tundravm/firewall-events and loads the mode's nft
# transaction; it opens no network socket.
[Unit]
Description=tundravm firewall mode switch
Requires=nftables.service tdx-evidence.service
After=nftables.service tdx-evidence.service

[Service]
Type=notify
ExecStart=/usr/bin/mode-switch --config /etc/tundravm/modes.json
Restart=always
RestartSec=1
StandardOutput=journal+console
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=-/run/tundravm -/sys/class/misc/tdx_guest/measurements
CapabilityBoundingSet=CAP_NET_ADMIN
RestrictAddressFamilies=AF_UNIX AF_NETLINK
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
"""
"""``mode-switch`` (``tools/cmd/mode-switch``): root, ``CAP_NET_ADMIN`` for nft, writing
only ``/run/tundravm`` and the kernel's RTMR3, after the ruleset and the execution gate."""


def mode_marks(firewall: Firewall) -> dict[str, int]:
    """Each mode's ``ct mark``, in declaration order from 1; 0 marks what every mode opens."""
    return {mode.name: at for at, mode in enumerate(firewall.modes, start=1)}


def moded_ports(firewall: Firewall) -> set[Port]:
    """The ports a mode names: open in that mode only."""
    return {port for mode in firewall.modes for port in mode.ingress if isinstance(port, Port)}


def _mark(value: int) -> str:
    return f"0x{value:08x}"


def _established(firewall: Firewall) -> list[str]:
    if not firewall.modes:
        return ["ct state established,related accept"]
    return [
        f"ct state established,related ct mark {_mark(0)} accept",
        "ct state established,related ct mark @current_mode accept",
    ]


def _mode_chains(
    firewall: Firewall, items: Iterable[Declaration], hook: Literal["input", "output"]
) -> str:
    """Each mode's own chain, then the dispatch chain the switcher rewrites (production at
    boot), each declared before what jumps to it."""
    listeners = listening(items)
    marks = mode_marks(firewall)
    text = ""
    for mode in firewall.modes:
        rules: list[str] = []
        if hook == "input":
            for port in sorted(
                (p for p in mode.ingress if isinstance(p, Port)),
                key=lambda p: (p.number, p.protocol),
            ):
                why = _comment([f"{mode.name}: {', '.join(listeners.get(port, ['allow']))}"])
                rules.append(
                    f"{port.protocol} dport {port.number} ct mark set {_mark(marks[mode.name])} "
                    f'accept comment "{why}"'
                )
        else:
            for rule in mode.egress_rules:
                why = _comment([rule.comment if rule.origins else f"{mode.name} mode"])
                rules.append(
                    f"{egress_match(rule)} ct mark set {_mark(marks[mode.name])} "
                    f'accept comment "{why}"'
                )
        body = "".join(f"\t\t{rule}\n" for rule in rules)
        text += f"\tchain {mode.name}_{hook} {{\n{body}\t}}\n"
    return text + f"\tchain mode_{hook} {{\n\t\tjump {firewall.modes[0].name}_{hook}\n\t}}\n"


def nftables_conf(firewall: Firewall, items: Iterable[Declaration]) -> str:
    """The ``/etc/nftables.conf`` *firewall* renders for a variant declaring *items*: with
    modes, the ``mode`` set holding production's mark and the chains ``mode-switch``
    swaps."""
    items = tuple(items)
    moded = moded_ports(firewall)
    rules = [
        *_established(firewall),
        "ct state invalid drop",
        'iifname "lo" accept',
        f"icmpv6 type {{ {', '.join(ICMPV6_ESSENTIAL)} }} accept",
        'udp sport 67 udp dport 68 accept comment "DHCPv4 replies"',
        'ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept comment "DHCPv6 replies"',
    ]
    for port, reasons in allowed_ports(firewall, items).items():
        if port not in moded:
            rules.append(
                f'{port.protocol} dport {port.number} accept comment "{_comment(reasons)}"'
            )
    if firewall.modes:
        rules.append("jump mode_input")
    if firewall.default == "reject":
        rules += [
            "meta l4proto tcp reject with tcp reset",
            "reject with icmpx type port-unreachable",
        ]
    body = "".join(f"\t\t{rule}\n" for rule in rules)
    filtered = firewall.egress == "filter"
    outbound = "".join(f"\t\t{rule}\n" for rule in _output_rules(firewall, items))
    modes = ""
    if firewall.modes:
        modes = f"\tset current_mode {{\n\t\ttype mark\n\t\telements = {{ {_mark(1)} }}\n\t}}\n"
    return (
        "#!/usr/sbin/nft -f\n"
        "# tundravm Firewall: inbound traffic is dropped unless a rule below accepts it;\n"
        + (
            "# outbound traffic is dropped unless a rule in the output chain accepts it.\n"
            if filtered
            else "# outbound traffic is not filtered.\n"
        )
        + "# The cloud metadata servers take only what the units that declare them send.\n"
        + (
            "# Modes: mode-switch swaps mode_input/mode_output and current_mode; a flow\n"
            "# keeps its mode's ct mark and drops once another mode is in force.\n"
            if firewall.modes
            else ""
        )
        + "flush ruleset\n\n"
        "table inet filter {\n"
        f"{modes}"
        + (_mode_chains(firewall, items, "input") if firewall.modes else "")
        + "\tchain input {\n"
        "\t\ttype filter hook input priority filter; policy drop;\n"
        f"{body}"
        "\t}\n"
        "\tchain forward {\n"
        "\t\ttype filter hook forward priority filter; policy drop;\n"
        "\t}\n"
        + (_mode_chains(firewall, items, "output") if firewall.modes and filtered else "")
        + "\tchain output {\n"
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
            *_established(firewall),
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
    if firewall.modes:
        rules.append("jump mode_output")
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


def modes_config(firewall: Firewall, *, production: bool) -> str:
    """:data:`MODES_CONFIG`: the table, whether the output chain switches, the build's kind
    (a production build switches only for a pinned operator) and each mode's mark and span."""
    marks = mode_marks(firewall)
    payload = {
        "table": "inet filter",
        "output": firewall.egress == "filter",
        "production_build": production,
        "modes": [
            {"name": mode.name, "mark": marks[mode.name], "lasts": mode.lasts}
            for mode in firewall.modes
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def _switcher(
    firewall: Firewall, items: tuple[Declaration, ...], production: bool
) -> tuple[Declaration, ...]:
    """``mode-switch``, built from the tools source the evidence service's build takes, its
    configuration and its unit."""
    tdxs = next((i for i in items if isinstance(i, Build) and i.name == "tdxs"), None)
    if tdxs is None:
        raise ValidationError(
            "Firewall: modes record each switch in RTMR3, which the evidence service reports, "
            "and this profile has none.",
            hint="Keep the evidence service: Tdxs() (an Image holds it by default).",
        )
    go = Go(package="./cmd/mode-switch", output="mode-switch")
    return (
        Build("mode-switch", tdxs.source, using=go, install=(Install(go.artifact, MODE_SWITCH),)),
        File.text(MODES_CONFIG, modes_config(firewall, production=production)),
        Unit(MODE_SWITCH_UNIT, MODE_SWITCH_TEXT, enabled=True),
    )


def expand_firewall(
    firewall: Firewall, items: Iterable[Declaration], *, production: bool = False
) -> tuple[Declaration, ...]:
    """The declarations *firewall* stands for in a variant declaring *items*: with an enabled
    ``systemd-networkd`` (``Network``), :data:`NETWORKD_FIREWALL_DROPIN` too; with modes,
    ``mode-switch`` (*production*: the variant is a production build)."""
    items = tuple(items)
    rendered: tuple[Declaration, ...] = (
        Package("nftables"),
        File.text(NFTABLES_CONF, nftables_conf(firewall, items), mode=0o755),
        Unit("nftables.service", enabled=True),
    )
    if firewall.egress != "filter" and any(mode.egress_rules for mode in firewall.modes):
        raise ValidationError(
            "Firewall: a mode's egress adds to what the image sends, and this image filters "
            "nothing outbound.",
            hint="Filter egress first: network=Network(egress=Egress(...)).",
        )
    if firewall.modes:
        rendered += _switcher(firewall, items, production)
    networkd = any(
        isinstance(item, Unit) and item.name == "systemd-networkd.service" and item.enabled
        for item in items
    )
    if networkd:
        rendered += (File.text(NETWORKD_FIREWALL_DROPIN, NETWORKD_FIREWALL_TEXT),)
    return rendered
