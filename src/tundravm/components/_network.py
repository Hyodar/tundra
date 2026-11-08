"""The network baseline: ``Network`` (addresses, DNS, what leaves the image) and
``Firewall`` (the input and output chains, switchable by ``Mode``). An image implies both;
passing one in ``components=`` (or ``network=``) replaces that default.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence

from tundravm._image._component import Component, Readied, ServiceReady
from tundravm._image._values import (
    TCP,
    UDP,
    Egress,
    Host,
    Mode,
    Notify,
    PortValue,
    StaticAddress,
    egress_host,
    host_entries,
)
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.declarative._network import MODE_SWITCH
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError
from tundravm.models import HostsEntry

from ._base import _diagnostic

__all__ = [
    "Firewall",
    "Network",
    "check_pinned",
    "egress_rules",
    "pinned_hosts",
]


RESOLVED_USER = "systemd-resolve"
"""The user Debian's ``systemd-resolved`` runs as, whose sockets send DNS over TLS."""


class Network(Component):
    """How the image reaches the network: ``Network()`` is DHCP on ``en*``/``eth*``.

    *static* replaces DHCP with a fixed address
    (``Network(dhcp=False, static=StaticAddress("10.0.0.5/24", gateway="10.0.0.1"))``);
    *dns* names the DNS-over-TLS resolvers, ``ADDRESS#TLS-NAME`` (Cloudflare's pair by
    default; the firewall accepts TCP 853 to each, and no plain DNS);
    *egress* filters what the image may send (the image's firewall applies it).
    ``image.network`` takes it, and so does ``components=``. Exports :attr:`online`.
    Component name: ``network``.
    """

    dhcp: bool = True
    static: StaticAddress | None = None
    egress: Egress | None = None
    interfaces: Sequence[str] = ("en*", "eth*")
    dns: Sequence[str] = du.TEE_RESOLVERS

    def __post_init__(self) -> None:
        if not isinstance(self.dhcp, bool):
            raise ValidationError(
                "Network: dhcp= must be True or False.",
                hint="Pass dhcp=True, or dhcp=False with static=.",
            )
        if self.static is not None and not isinstance(self.static, StaticAddress):
            raise ValidationError(
                "Network: static= must be a StaticAddress or None.",
                hint="Pass static=StaticAddress('10.0.0.5/24', gateway='10.0.0.1').",
            )
        if self.egress is not None and not isinstance(self.egress, Egress):
            raise ValidationError(
                "Network: egress= must be an Egress or None.",
                hint="Pass egress=Egress(ports=[TCP(443)], hosts=[...]).",
            )
        _whole_image("Network", self.egress)
        if isinstance(self.interfaces, str):
            raise ValidationError(
                "Network: interfaces= takes a list of names or globs.",
                hint="Pass interfaces=['en*', 'eth*'].",
            )
        object.__setattr__(self, "interfaces", tuple(self.interfaces))
        if isinstance(self.dns, str):
            raise ValidationError(
                "Network: dns= takes a list of resolvers.",
                hint=f"Pass dns=[{self.dns!r}].",
            )
        object.__setattr__(self, "dns", tuple(self.dns))
        if not self.dns:
            raise ValidationError(
                "Network: dns= names no resolver, so nothing resolves.",
                hint="Pass dns=['9.9.9.9#dns.quad9.net'], or leave it out for Cloudflare's pair.",
            )
        for entry in self.dns:
            du.dot_resolver("Network", entry)
        if self.dhcp == (self.static is not None):
            raise ValidationError(
                "Network: a network takes DHCP or a static address: "
                + ("both are set." if self.dhcp else "neither is set."),
                hint="Network() for DHCP, Network(dhcp=False, static=StaticAddress(...)).",
            )

    @property
    def online(self) -> ServiceReady:
        """ "An interface is configured": ``systemd-networkd-wait-online``, bounded."""
        return ServiceReady("systemd-networkd-wait-online.service", Notify())

    def compose(self) -> Iterator[object]:
        network = du.Network(
            dhcp=self.dhcp,
            interfaces=tuple(self.interfaces),
            static=self.static,
            dns=tuple(self.dns),
        )
        yield from network.items

    def outbound(self) -> Egress:
        """DNS over TLS (TCP 853) to each of *dns*."""
        hosts = []
        for entry in self.dns:
            address, _name = du.dot_resolver("Network", entry)
            shown = f"[{address}]" if ":" in address else address
            hosts.append(f"{shown}:{du.DOT_PORT}")
        return Egress(hosts=hosts)

    def senders(self) -> tuple[str, ...]:
        """``systemd-resolved``'s user, which asks the resolvers."""
        return (RESOLVED_USER,)

    def readiness(self) -> tuple[Readied, ...]:
        return (self.online,)

    rules = (
        Rule(
            "network-no-route",
            "warning",
            "A static address without a gateway reaches its own subnet only, so NTS time and "
            "every remote endpoint fail.",
            'Give it a gateway: Network(static=StaticAddress("10.0.0.5/24", gateway="10.0.0.1")).',
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``network-no-route``: a static address without a gateway reaches its subnet only."""
        if self.static is None or self.static.gateway is not None:
            return ()
        return (
            _diagnostic(
                "network-no-route",
                "warning",
                f"Network(static={self.static.address}) names no gateway: the image reaches "
                "its own subnet only, so NTS time and every remote endpoint fail",
            ),
        )


RTMR3_KERNEL = (6, 16)
"""The first Linux whose ``tdx_guest`` lets user space extend RTMR3."""
RTMR3_PATH = "/sys/class/misc/tdx_guest/measurements/rtmr3:sha384"
"""What the mode switcher writes each switch's digest to (``tools/pkg/modes``)."""
DEBIAN_KERNEL = (6, 12)
"""The Linux of ``Kernel.default()``: Debian trixie's ``linux-image-amd64``."""
BACKPORTS_KERNEL = 'DebianConfig("6.18.15-1~bpo13+1", snapshot="20261003T143013Z")'
"""A trixie-backports kernel with RTMR3, as ``firewall-modes-kernel`` names it."""


def _kernel_version(resolved: dm.Resolved) -> tuple[int, int] | None:
    """The major and minor of the Linux the profile boots: its built kernel's (``None``
    when its version names none), else Debian's."""
    for item in resolved.items:
        if isinstance(item, dm.Kernel):
            found = re.match(r"(\d+)\.(\d+)", item.version)
            return None if found is None else (int(found.group(1)), int(found.group(2)))
    return DEBIAN_KERNEL


class Firewall(Component):
    """The inbound (and optionally outbound) packet filter, nftables in the image.

    Inbound it drops everything except the ports services ``expose=``, an ingress's port,
    :data:`~tundravm._image._values.MANAGEMENT_PORT` where the attested endpoint listens,
    and *allow* (ports something other than a ``Service`` listens on: a
    packaged daemon); each rule's comment names what listens. Outbound it
    filters only with *egress*, and then accepts what the baseline and every component's
    ``outbound()`` send as well, each rule commented with who asked for it; a component a
    ``Profile`` adds joins that profile's rules only. A service's own
    ``Service(egress=...)`` is accepted for its user alone. Filtered or not, the cloud
    metadata servers take only what the services declaring ``Egress(metadata=True)`` and
    tundravm's cloud glue send. The image holds ``Firewall()`` by default;
    ``network=Network(egress=...)`` is the usual way to set *egress*.

    *modes* make it switchable at run time: ``modes={"production": Mode(...),
    "maintenance": Mode(ingress=[TCP(22)], lasts="30m")}``. ``production`` is required and
    the image boots into it; ``tundravm run switch INSTANCE maintenance`` opens the window
    over the attested endpoint, each switch is extended into RTMR3 so ``attest`` shows the
    history, and the window closes itself after *lasts*. Refused: SSH in production, a
    mode named ``tdx`` or ``simulated``, ``lasts`` on production. Component name:
    ``firewall``.
    """

    allow: Sequence[PortValue] = ()
    egress: Egress | None = None
    modes: Mapping[str, Mode] = FrozenMap()

    def __post_init__(self) -> None:
        if isinstance(self.allow, (TCP, UDP)):
            raise ValidationError(
                "Firewall: allow= takes a list of ports: allow=[TCP(9100)].",
                hint="Wrap the port in a list: allow=[TCP(9100)].",
            )
        object.__setattr__(self, "allow", tuple(self.allow))
        for port in self.allow:
            if not isinstance(port, (TCP, UDP)):
                raise ValidationError(
                    f"Firewall: allow= holds {port!r}, not TCP(...) or UDP(...).",
                    hint="Pass allow=[TCP(9100), UDP(53)].",
                )
        if self.egress is not None and not isinstance(self.egress, Egress):
            raise ValidationError(
                "Firewall: egress= must be an Egress or None.",
                hint="Pass egress=Egress(...), or set it on the Network.",
            )
        _whole_image("Firewall", self.egress)
        if not isinstance(self.modes, Mapping):
            raise ValidationError(
                "Firewall: modes= maps each mode's name to a Mode.",
                hint='modes={"production": Mode(), "maintenance": Mode(ingress=[TCP(22)])}',
            )
        object.__setattr__(self, "modes", FrozenMap(dict(self.modes)))
        for name, mode in self.modes.items():
            if not isinstance(mode, Mode):
                raise ValidationError(
                    f"Firewall: modes[{name!r}] is not a Mode.",
                    hint=f'"{name}": Mode(ingress=[TCP(22)])',
                )
        if self.modes and "production" not in self.modes:
            raise ValidationError(
                "Firewall: modes= has no production mode, the one the image boots into and "
                "a verifier trusts.",
                hint='Add "production": Mode() (what every mode opens stays open in it).',
            )
        if self.modes and self.modes["production"].lasts is not None:
            raise ValidationError(
                "Firewall: the production mode closes after lasts=, but the image boots into "
                "it and returns to it.",
                hint='Drop lasts= from "production".',
            )
        self.lowered_modes()

    def lowered_modes(self) -> tuple[dm.FirewallMode, ...]:
        """*modes* as the ruleset takes them, production first."""
        ordered = sorted(self.modes.items(), key=lambda entry: entry[0] != "production")
        return tuple(
            dm.FirewallMode(
                name,
                ingress=tuple(dm.Port(p.port, p.protocol) for p in mode.ingress),
                egress_rules=()
                if mode.egress is None
                else egress_rules(((f"{name} mode", mode.egress),)),
                lasts=0 if name == "production" else mode.seconds,
            )
            for name, mode in ordered
        )

    @property
    def status(self) -> du.Sudo:
        """A :class:`~tundravm.components.access.Login` command printing the mode in force,
        the switches since boot and whether RTMR3 replays them:
        ``Sudo("/usr/bin/mode-switch", "status")``."""
        self._switchable("status")
        return du.Sudo(MODE_SWITCH, "status")

    @property
    def switch(self) -> du.Sudo:
        """A :class:`~tundravm.components.access.Login` command switching to the mode the
        client names, one of *modes*, extended into RTMR3:
        ``Sudo("/usr/bin/mode-switch", "to", Choice("mode", *modes))``."""
        self._switchable("switch")
        return du.Sudo(MODE_SWITCH, "to", du.Choice("mode", *self.modes))

    def _switchable(self, entry: str) -> None:
        if not self.modes:
            raise ValidationError(
                f"Firewall: {entry} is a login command for a switchable firewall, and this one "
                "has no modes.",
                hint='Add modes={"production": Mode(), "maintenance": Mode(...)}.',
            )

    def compose(self) -> Iterator[object]:
        allow = tuple(dm.Port(p.port, p.protocol) for p in self.allow)
        modes = self.lowered_modes()
        if self.egress is None:
            yield dm.Firewall(allow=allow, modes=modes)
            return
        yield dm.Firewall(
            allow=allow,
            egress="filter",
            egress_rules=egress_rules((("image", self.egress),)),
            modes=modes,
        )

    rules = (
        Rule(
            "firewall-ssh-open",
            "warning",
            "The firewall opens port 22 by hand, outside the SSH access that accepts only a key.",
            "Drop 22 from Firewall(allow=) and use SshAccess(...) or dev=DevAccess(...), which "
            "open it only with a key to accept.",
        ),
        Rule(
            "egress-unfiltered",
            "warning",
            "A production profile accepts every outbound packet (no Egress, or one allowing "
            "0.0.0.0/0 or ::/0), so a compromised service sends what it holds anywhere.",
            "List what its services talk to: network=Network(egress=Egress(ports=[TCP(443)], "
            "hosts=[...])), or give one service its own with Service(..., egress=Egress(...)); "
            "what the baseline sends (NTS time, resolvers) is accepted for you. A "
            "Profile(production=False) is exempt.",
        ),
        Rule(
            "firewall-modes-kernel",
            "warning",
            "The image declares firewall modes on a kernel older than Linux 6.16, which has "
            "no RTMR3 to extend from user space, so on TDX every switch is refused. "
            "Kernel.default() counts as Debian trixie's 6.12, whatever the image's release.",
            f"kernel=Kernel.build(base={BACKPORTS_KERNEL}): Debian's trixie-backports "
            "kernel, which has it.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``firewall-ssh-open``: port 22 opened by hand instead of by ``SshAccess``;
        ``egress-unfiltered``: a production profile that sends anywhere;
        ``firewall-modes-kernel``: modes on a kernel whose TDX switches are all refused."""
        found: list[dm.Diagnostic] = []
        if self.modes:
            kernel = _kernel_version(resolved)
            if kernel is not None and kernel < RTMR3_KERNEL:
                found.append(
                    dm.Diagnostic(
                        "firewall-modes-kernel",
                        "warning",
                        f"Firewall(modes=) extends RTMR3 at each switch through {RTMR3_PATH}, "
                        f"which the image's Linux {'.'.join(map(str, kernel))} lacks (6.16 has "
                        "it): on TDX every switch is refused",
                        hint=f"kernel=Kernel.build(base={BACKPORTS_KERNEL})",
                    )
                )
        if any(port.port == 22 for port in self.allow):
            found.append(
                _diagnostic(
                    "firewall-ssh-open",
                    "warning",
                    "Firewall(allow=[TCP(22)]) opens SSH by hand: use SshAccess or "
                    "dev=DevAccess(...), which open it only with a key to accept",
                )
            )
        if resolved.production:
            unfiltered = self._unfiltered()
            if unfiltered is not None:
                found.append(_diagnostic("egress-unfiltered", "warning", unfiltered))
        return tuple(found)

    def _unfiltered(self) -> str | None:
        """Why this firewall accepts every outbound packet, or ``None`` when it filters."""
        if self.egress is None:
            return (
                "the production profile accepts every outbound packet, so a compromised "
                "service sends what it holds anywhere"
            )
        everywhere = [
            host for host in self.egress.hosts if isinstance(host, str) and host.endswith("/0")
        ]
        if everywhere:
            return (
                f"Egress(hosts=[{', '.join(map(repr, everywhere))}]) accepts every "
                "destination, so it filters nothing"
            )
        return None


def egress_rules(
    contributions: Iterable[tuple[str, Egress]],
    senders: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dm.EgressRule, ...]:
    """The output-chain rules *contributions* (``(who, egress)`` pairs) ask for: one per
    destination, with everyone who asked for it, in the order asked; a host name is its
    own destination, matched by the addresses it resolves to at boot; a :class:`Host` is
    one address rule per address, commented with its name; *metadata* is one rule per
    cloud metadata server.

    *senders* maps a contributor to the users whose sockets send what it asks for: its
    rules match each of them (``meta skuid``), one rule per user. A contributor it does not
    map (the image's own ``Egress``, a mode's) sends from any process, and a destination it
    asks for is accepted for every process, whoever else asked for it too."""
    owners = {} if senders is None else senders
    found: dict[tuple[dm.Port | None, str | None, str | None, str | None], list[str]] = {}
    for who, egress in contributions:
        if egress.metadata:
            for host in dm.CLOUD_METADATA:
                found.setdefault((None, host, None, None), []).append(who)
        for value in egress.ports:
            found.setdefault((dm.Port(value.port, value.protocol), None, None, None), []).append(
                who
            )
        for entry in egress.hosts:
            for text, pinned in host_entries(egress, entry):
                host, number, named = egress_host(egress, text)
                port = None if number is None else dm.Port(number)
                label = None if pinned is None else pinned.name
                key = (port, None, host, None) if named else (port, host, None, label)
                found.setdefault(key, []).append(who)
    rules: list[dm.EgressRule] = []
    for (to, address, name, label), asked in found.items():
        origins = tuple(dict.fromkeys(asked))
        if any(not owners.get(who) for who in origins):
            rules.append(dm.EgressRule(to, address, origins, name=name, pinned=label))
            continue
        users = dict.fromkeys(user for who in origins for user in owners[who])
        for user in users:
            mine = tuple(who for who in origins if user in owners[who])
            rules.append(dm.EgressRule(to, address, mine, name=name, user=user, pinned=label))
    return tuple(rules)


def pinned_hosts(contributions: Iterable[tuple[str, Egress]]) -> tuple[HostsEntry, ...]:
    """The ``/etc/hosts`` entries the :class:`Host` pins of *contributions* ask for: one
    ``(address, names)`` per address, in the order asked, each once."""
    found: dict[HostsEntry, None] = {}
    for _who, egress in contributions:
        for host in egress.pinned:
            for address in host.addresses:
                found.setdefault((address, host.names), None)
    return tuple(found)


def check_pinned(contributions: Iterable[tuple[str, Egress]]) -> None:
    """Refuse *contributions* (``(where, egress)`` pairs, the whole image's) that pin one
    name to two sets of addresses, or pin a name in one place and leave it to DNS
    (``"NAME:PORT"``) in another."""
    pins: dict[str, tuple[tuple[str, ...], str]] = {}
    resolved: list[tuple[str, str, str]] = []
    for where, egress in contributions:
        for entry in egress.hosts:
            if isinstance(entry, Host):
                for alias in entry.names:
                    pins.setdefault(alias, (tuple(entry.addresses), where))
                addresses, first = pins[entry.name]
                if set(addresses) != set(entry.addresses):
                    raise ValidationError(
                        f"Image: {where} pins {entry.name} to {', '.join(entry.addresses)}, "
                        f"and {first} pins it to {', '.join(addresses)}.",
                        hint="Pin a name to one set of addresses: declare the Host once and "
                        "pass that value everywhere the peer is reached.",
                    )
                continue
            name, _port, named = egress_host(egress, entry)
            if named:
                resolved.append((name, entry, where))
    for name, text, where in resolved:
        if name in pins:
            addresses, first = pins[name]
            raise ValidationError(
                f"Image: {first} pins {name} to {', '.join(addresses)}, and {where} names it "
                f"as {text!r}, which DNS would resolve at boot.",
                hint=f"Use the Host everywhere: hosts=[Host({name!r}, [...], port=...)].",
            )


def _whole_image(owner: str, egress: Egress | None) -> None:
    """Refuse *egress* given to the whole image with *metadata*, a service's alone."""
    if egress is not None and egress.metadata:
        raise ValidationError(
            f"{owner}: egress=Egress(metadata=True) would let every process of the image reach "
            "the cloud metadata server and its identity token.",
            hint="Give it to the one service that reads it: "
            "Service(..., egress=Egress(metadata=True)).",
        )
