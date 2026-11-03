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
    ContainerNetwork,
    ContainerRoot,
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
    host_sets,
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


def container_networks(items: Iterable[Declaration]) -> list[tuple[str, ContainerNetwork]]:
    """``(container, veth)`` of each container in *items* with a veth, checked: no two on
    one /30 or veth name, and no exposed port that another container or a service takes."""
    found: list[tuple[str, ContainerNetwork]] = []
    items = tuple(items)
    taken = {port: units[0] for port, units in listening(items).items()}
    for item in items:
        if not isinstance(item, ContainerRoot) or item.network is None:
            continue
        network = item.network
        for name, other in found:
            if network.host == other.host or network.interface == other.interface:
                raise ValidationError(
                    f"Container({item.name!r}): its veth {network.interface} on "
                    f"{network.host}/30 is Container({name!r})'s too.",
                    hint="Rename one of them: the name picks the veth and its addresses.",
                )
        for port in network.expose:
            if port in taken:
                raise ValidationError(
                    f"Container({item.name!r}): expose= takes {port}, which {taken[port]} "
                    "takes too, and the host's port goes to one of them.",
                    hint="Expose another port from one of them.",
                )
            taken[port] = f"Container({item.name!r})"
        found.append((item.name, network))
    return found


def _refuse(match: str, why: str) -> list[str]:
    """Refuse what *match* matches: a TCP reset, an ICMP admin-prohibited otherwise."""
    return [
        f'{match} meta l4proto tcp reject with tcp reset comment "{why}"',
        f'{match} reject with icmpx type admin-prohibited comment "{why}"',
    ]


def _container_input(networks: list[tuple[str, ContainerNetwork]]) -> list[str]:
    """The host is no container's destination: its endpoints are relayed in its namespace."""
    rules: list[str] = []
    for name, network in networks:
        rules += _refuse(f'iifname "{network.interface}"', f"{name}: not the host")
    return rules


def _container_forward(
    firewall: Firewall, networks: list[tuple[str, ContainerNetwork]]
) -> list[str]:
    """The forward chain: what each container sends (its egress, the metadata servers only
    when it names them, the rest refused), then each port DNATed to it, a moded one through
    ``mode_input``."""
    if not networks:
        return []
    rules = [*_established(firewall), "ct state invalid drop"]
    marks = mode_marks(firewall)
    for name, network in networks:
        sent = f'iifname "{network.interface}"'
        own = network.egress_rules
        for rule in own:
            if rule.host in CLOUD_METADATA:
                rules += [f'{sent} {m} accept comment "{name}"' for m in egress_matches(rule)]
        for family in ("ip", "ip6"):
            servers = sorted(host for host in CLOUD_METADATA if _family(host) == family)
            spelled = servers[0] if len(servers) == 1 else "{ " + ", ".join(servers) + " }"
            rules += _refuse(f"{sent} {family} daddr {spelled}", f"{name}: cloud metadata")
        for rule in own:
            if rule.host not in CLOUD_METADATA:
                why = _comment([rule.comment])
                rules += [f'{sent} {m} accept comment "{why}"' for m in egress_matches(rule)]
        for mode, rule in network.modes:
            if mode not in marks:
                raise ValidationError(
                    f"Container({name!r}): modes= names {mode!r}, which the image's Firewall "
                    f"does not declare ({', '.join(marks) or 'it has no modes'}).",
                    hint='Firewall(modes={"production": Mode(), "' + mode + '": Mode(...)})',
                )
            rules += [
                f"{sent} {m} meta mark set {_mark(marks[mode])} meta mark @current_mode "
                f'ct mark set meta mark accept comment "{_comment([rule.comment])}"'
                for m in egress_matches(rule)
            ]
        rules += _refuse(sent, f"{name}: no egress rule")
    moded = moded_ports(firewall)
    for name, network in networks:
        into = f'oifname "{network.interface}" ct status dnat'
        for port in network.expose:
            if port not in moded:
                rules.append(f'{into} {port.protocol} dport {port.number} accept comment "{name}"')
        if any(port in moded for port in network.expose):
            rules.append(f"{into} jump mode_input")
    return rules


def _nat_chains(networks: list[tuple[str, ContainerNetwork]]) -> str:
    """``prerouting`` DNATs each exposed port to its container; ``postrouting`` masquerades
    what a container with egress sends."""
    into = [
        f'{port.protocol} dport {port.number} dnat ip to {network.address} comment "{name}"'
        for name, network in networks
        for port in network.expose
    ]
    out = [
        f'ip saddr {network.address} oifname != "{network.interface}" masquerade comment "{name}"'
        for name, network in networks
        if network.egress_rules or network.modes
    ]
    text = ""
    for chain, priority, rules in (("prerouting", "dstnat", into), ("postrouting", "srcnat", out)):
        if rules:
            body = "".join(f"\t\t{rule}\n" for rule in rules)
            text += (
                f"\tchain {chain} {{\n\t\ttype nat hook {chain} priority {priority}; "
                f"policy accept;\n{body}\t}}\n"
            )
    return text


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
    items = tuple(items)
    listeners = listening(items)
    for name, network in container_networks(items):
        for port in network.expose:
            listeners.setdefault(port, []).append(name)
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
                rules += [
                    f'{match} ct mark set {_mark(marks[mode.name])} accept comment "{why}"'
                    for match in egress_matches(rule)
                ]
        body = "".join(f"\t\t{rule}\n" for rule in rules)
        text += f"\tchain {mode.name}_{hook} {{\n{body}\t}}\n"
    return text + f"\tchain mode_{hook} {{\n\t\tjump {firewall.modes[0].name}_{hook}\n\t}}\n"


def nftables_conf(firewall: Firewall, items: Iterable[Declaration]) -> str:
    """The ``/etc/nftables.conf`` *firewall* renders for a variant declaring *items*: with
    modes, the ``mode`` set holding production's mark and the chains ``mode-switch``
    swaps."""
    items = tuple(items)
    moded = moded_ports(firewall)
    networks = container_networks(items)
    rules = [
        *_established(firewall),
        "ct state invalid drop",
        'iifname "lo" accept',
        *_container_input(networks),
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
    forward = "".join(f"\t\t{rule}\n" for rule in _container_forward(firewall, networks))
    filtered = firewall.egress == "filter"
    outbound = "".join(f"\t\t{rule}\n" for rule in _output_rules(firewall, items))
    sets = ""
    if firewall.modes:
        sets = f"\tset current_mode {{\n\t\ttype mark\n\t\telements = {{ {_mark(1)} }}\n\t}}\n"
    names = host_names(firewall, items)
    for name in names:
        for family, table in zip(("ipv4_addr", "ipv6_addr"), host_sets(name), strict=True):
            sets += f"\tset {table} {{\n\t\ttype {family}\n\t}}\n"
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
            "# Host names: egress-resolve fills each host4_/host6_ set from DNS over TLS at\n"
            "# boot and every 30s; a rule naming one accepts nothing until it has.\n"
            if names
            else ""
        )
        + (
            "# Modes: mode-switch swaps mode_input/mode_output and current_mode; a flow\n"
            "# keeps its mode's ct mark and drops once another mode is in force.\n"
            if firewall.modes
            else ""
        )
        + "flush ruleset\n\n"
        "table inet filter {\n"
        f"{sets}"
        + (_mode_chains(firewall, items, "input") if firewall.modes else "")
        + "\tchain input {\n"
        "\t\ttype filter hook input priority filter; policy drop;\n"
        f"{body}"
        "\t}\n"
        "\tchain forward {\n"
        "\t\ttype filter hook forward priority filter; policy drop;\n"
        f"{forward}"
        "\t}\n"
        f"{_nat_chains(networks)}"
        + (_mode_chains(firewall, items, "output") if firewall.modes and filtered else "")
        + "\tchain output {\n"
        f"\t\ttype filter hook output priority filter; policy {'drop' if filtered else 'accept'};\n"
        f"{outbound}"
        "\t}\n"
        "}\n"
    )


METADATA_SHARED = (EgressRule(Port(123, "udp"), CLOUD_METADATA[0], ("metadata time",)),)
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
            rules += [f'{owner} {m} accept comment "{unit}"' for m in egress_matches(rule)]
    for rule in METADATA_SHARED:
        rules += [f'{m} accept comment "{rule.comment}"' for m in egress_matches(rule)]
    for family in ("ip", "ip6"):
        servers = sorted(host for host in CLOUD_METADATA if _family(host) == family)
        spelled = servers[0] if len(servers) == 1 else "{ " + ", ".join(servers) + " }"
        rules.append(f'{family} daddr {spelled} drop comment "cloud metadata"')
    if not filtered:
        return rules
    for unit, owner, rule in own:
        if rule.host not in CLOUD_METADATA:
            rules += [f'{owner} {m} accept comment "{unit}"' for m in egress_matches(rule)]
    for name, network in container_networks(items):
        for port in network.serves:
            rules.append(
                f'oifname "{network.interface}" ip daddr {network.address} '
                f'{port.protocol} dport {port.number} accept comment "{name}: serves"'
            )
    anywhere = [rule for rule in firewall.egress_rules if rule.host is None and rule.name is None]
    hosts = [rule for rule in firewall.egress_rules if rule not in anywhere]
    for rule in [*sorted(anywhere, key=_port_order), *hosts]:
        comment = _comment([rule.comment]) if rule.origins else "egress"
        rules += [f'{m} accept comment "{comment}"' for m in egress_matches(rule)]
    if firewall.modes:
        rules.append("jump mode_output")
    return rules


def egress_matches(rule: EgressRule) -> tuple[str, ...]:
    """*rule*'s matches in the output chain, as ``nft list ruleset`` prints them back: one,
    or a named rule's two, its IPv4 set's and its IPv6 set's."""
    port = [] if rule.port is None else [f"{rule.port.protocol} dport {rule.port.number}"]
    owner = [] if rule.user is None else [f'meta skuid "{rule.user}"']
    if rule.name is not None:
        four, six = host_sets(rule.name)
        return (
            " ".join([*owner, f"ip daddr @{four}", *port]),
            " ".join([*owner, f"ip6 daddr @{six}", *port]),
        )
    match = [] if rule.host is None else [f"{_family(rule.host)} daddr {rule.host}"]
    return (" ".join([*owner, *match, *port]),)


def host_names(firewall: Firewall, items: Iterable[Declaration]) -> tuple[str, ...]:
    """The host names the rules of *firewall*, its modes and the units of *items* accept
    when it filters egress, and those of each container's veth, in the order they appear:
    each has its sets."""
    items = tuple(items)
    filtered = firewall.egress == "filter"
    rules = [
        *(firewall.egress_rules if filtered else ()),
        *(rule for mode in firewall.modes if filtered for rule in mode.egress_rules),
        *(rule for _unit, _owner, rule in (unit_egress(items) if filtered else ())),
        *(
            rule
            for _name, network in container_networks(items)
            for rule in (*network.egress_rules, *(r for _mode, r in network.modes))
        ),
    ]
    return tuple(dict.fromkeys(rule.name for rule in rules if rule.name is not None))


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


EGRESS_HOSTS = "/etc/tundravm/egress-hosts"
"""The host names the output chain's rules accept, one per line, for ``egress-resolve``."""
EGRESS_RESOLVE = "/usr/lib/tundravm/egress-resolve"
EGRESS_RESOLVE_UNIT = "egress-resolve.service"
EGRESS_RESOLVE_TEXT = r"""#!/bin/sh
# tundravm Firewall: each host name of /etc/tundravm/egress-hosts resolved through
# systemd-resolved (DNS over TLS to the literal resolvers) into its host4_/host6_ sets,
# which the output chain's rules for that name accept alone. A name that does not resolve
# keeps what its sets held: nothing, before its first answer.
set -u
state=/run/tundravm-egress
addresses() {
    getent "$1" "$2" 2>/dev/null | while read -r address _; do
        case $address in ::ffff:*) ;; *) echo "$address" ;; esac
    done | sort -u | paste -sd, -
}
transaction=
changed=
while read -r name; do
    [ -n "$name" ] || continue
    four=$(addresses ahostsv4 "$name")
    six=$(addresses ahostsv6 "$name")
    if [ -z "$four$six" ]; then
        if [ "$(cat "$state/$name" 2>/dev/null)" != - ]; then
            echo "egress-resolve: $name does not resolve; its rules accept what they held"
            echo - > "$state/$name"
        fi
        continue
    fi
    transaction="${transaction}flush set inet filter host4_$name
flush set inet filter host6_$name
"
    [ -z "$four" ] || transaction="${transaction}add element inet filter host4_$name { $four }
"
    [ -z "$six" ] || transaction="${transaction}add element inet filter host6_$name { $six }
"
    held="$four${four:+${six:+,}}$six"
    [ "$(cat "$state/$name" 2>/dev/null)" = "$held" ] || changed="$changed $name=$held"
done < /etc/tundravm/egress-hosts
[ -n "$transaction" ] || exit 0
printf '%s' "$transaction" | nft -f - || exit 1
for entry in $changed; do
    echo "$entry" | sed 's/=/ holds /; s/,/, /g; s/^/egress-resolve: /'
    printf '%s\n' "${entry#*=}" > "$state/${entry%%=*}"
done
"""
"""``egress-resolve``: an empty answer leaves a name's sets as they were, so a resolver
outage keeps the last addresses and a name never answered stays unreachable."""
EGRESS_RESOLVE_SERVICE = """\
# tundravm Firewall: the host names an Egress accepts, resolved into the ruleset's sets,
# started by egress-resolve.timer at boot and every 30s after; root for nft alone,
# writing only its own run directory.
[Unit]
Description=tundravm egress host names into the firewall's sets
Requires=nftables.service
Wants=network-online.target
After=nftables.service systemd-resolved.service network-online.target

[Service]
Type=oneshot
ExecStart=/usr/lib/tundravm/egress-resolve
StandardOutput=journal+console
RuntimeDirectory=tundravm-egress
RuntimeDirectoryPreserve=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
CapabilityBoundingSet=CAP_NET_ADMIN
RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
"""
EGRESS_RESOLVE_TIMER = """\
# tundravm Firewall: egress-resolve at boot (it waits for the network) and every 30s;
# systemd-resolved answers from its cache until a record's TTL runs out, so a set
# follows DNS within 30s of it.
[Unit]
Description=tundravm egress host names, refreshed

[Timer]
OnBootSec=1s
OnUnitActiveSec=30s

[Install]
WantedBy=timers.target
"""


CHRONY_DROPIN = "/usr/lib/systemd/system/chrony.service.d/50-tundravm-egress.conf"
"""What orders chrony after the first resolution: its NTS servers are host names."""
CHRONY_DROPIN_TEXT = (
    "# tundravm Firewall: the NTS servers are host names, reachable once egress-resolve\n"
    "# filled their sets\n"
    "[Unit]\nWants=egress-resolve.service\nAfter=egress-resolve.service\n"
)


def _resolver(names: tuple[str, ...], items: tuple[Declaration, ...]) -> tuple[Declaration, ...]:
    """``egress-resolve``, its host names and its unit and timer; with chrony's
    configuration, the drop-in that starts chrony after the first resolution."""
    chrony = any(
        isinstance(item, File) and item.path == "/etc/chrony/chrony.conf" for item in items
    )
    return (
        File.text(EGRESS_HOSTS, "".join(f"{name}\n" for name in names)),
        File.text(EGRESS_RESOLVE, EGRESS_RESOLVE_TEXT, mode=0o755),
        Unit(EGRESS_RESOLVE_UNIT, EGRESS_RESOLVE_SERVICE),
        Unit("egress-resolve.timer", EGRESS_RESOLVE_TIMER, enabled=True),
        *((File.text(CHRONY_DROPIN, CHRONY_DROPIN_TEXT),) if chrony else ()),
    )


def expand_firewall(
    firewall: Firewall, items: Iterable[Declaration], *, production: bool = False
) -> tuple[Declaration, ...]:
    """The declarations *firewall* stands for in a variant declaring *items*: with an enabled
    ``systemd-networkd`` (``Network``), :data:`NETWORKD_FIREWALL_DROPIN` too; with modes,
    ``mode-switch`` (*production*: the variant is a production build); with a host name
    a filtered output chain accepts, ``egress-resolve`` and its timer."""
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
    names = host_names(firewall, items)
    if names:
        rendered += _resolver(names, items)
    networkd = any(
        isinstance(item, Unit) and item.name == "systemd-networkd.service" and item.enabled
        for item in items
    )
    if networkd:
        rendered += (File.text(NETWORKD_FIREWALL_DROPIN, NETWORKD_FIREWALL_TEXT),)
    return rendered
