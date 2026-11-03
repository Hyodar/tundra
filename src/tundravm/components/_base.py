"""The baseline every image implies, as components: ``TeeBase``, ``Network``, ``Firewall``,
``Tdxs``, and the two ways in for people: ``SshAccess`` and ``DevAccess``.

An image holds a default instance of ``TeeBase``, ``Network``, ``Firewall`` and ``Tdxs``
without being asked; passing one of them in ``components=`` (or ``network=``) replaces
that default, which is how an image configures its baseline. The renderers stay in
:mod:`tundravm.declarative.utils` and :mod:`tundravm._modules`; these classes are the
public contract over them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import field
from pathlib import Path
from typing import Any, Literal

from tundravm._image._component import Component, Readied, ServiceReady
from tundravm._image._values import (
    TCP,
    UDP,
    Egress,
    Mode,
    Notify,
    PortValue,
    StaticAddress,
    egress_host,
)
from tundravm._modules import tdxs as _tdxs
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.declarative.utils import Login, Sudo
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

__all__ = [
    "EVIDENCE",
    "INTEL_PCS",
    "DevAccess",
    "Firewall",
    "Login",
    "Mode",
    "Network",
    "SshAccess",
    "Sudo",
    "Tdxs",
    "TeeBase",
    "egress_rules",
]

EVIDENCE = ServiceReady("tdxs.service", Notify())
"""The implied evidence service is ready: what a service that asks for quotes requires."""

CHRONY_NTP = dm.EgressRule(dm.Port(123, "udp"), origins=("tee-base",), user="_chrony")
"""NTP to any address, from chrony's sockets alone: ``TeeBase(time=True)``'s, beside its
``outbound()``."""

INTEL_PCS = "api.trustedservices.intel.com:443"
"""Intel's provisioning certification service: TDX collateral and the PCK CRL, over HTTPS."""
INTEL_CERTIFICATES = "certificates.trustedservices.intel.com:443"
"""Where Intel publishes its root CA's CRL, over HTTPS."""

_SSH_KEY = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^\n]*)?"
)


def _diagnostic(code: str, severity: Literal["error", "warning", "info"], message: str) -> Any:
    return dm.Diagnostic(code, severity, message)


class TeeBase(Component):
    """The confidential-VM baseline: authenticated time, a volatile journal, sysctl
    hardening, DNS over TLS, no apt timers or console getty, and the CPU mitigations the
    kernel boots with.

    *time*: chrony over NTS from *nts_servers*, refined by the hypervisor clocks, so the
    host cannot set the guest's clock (TLS, certificates and attestation freshness
    depend on it). *journal*: journald in RAM, capped at *journal_max_use*. *sysctl*: the
    kernel hardening set. *dns*: ``systemd-resolved`` over strict DNS over TLS to the
    ``Network``'s ``dns=`` resolvers, so the host can neither read nor forge a lookup. *mask*: no
    apt timers, no getty on the host-owned console. The UKI's command line always starts
    with :attr:`kernel_cmdline`, which ``Kernel(cmdline=)`` appends to and cannot override.
    Component name: ``tee-base``.
    """

    time: bool = True
    nts_servers: Sequence[str] = du.TEE_NTS_SERVERS
    journal: bool = True
    journal_max_use: str = "128M"
    sysctl: bool = True
    dns: bool = True
    mask: bool = True

    def compose(self) -> Iterator[object]:
        base = du.TeeBase(
            time=self.time,
            nts_servers=tuple(self.nts_servers),
            journal=self.journal,
            journal_max_use=self.journal_max_use,
            sysctl=self.sysctl,
            dns=self.dns,
            mask=self.mask,
            network=False,
            firewall=False,
        )
        yield from base.items

    @property
    def kernel_cmdline(self) -> tuple[str, ...]:
        """``mitigations=auto,nosmt spec_store_bypass_disable=on``: every CPU vulnerability
        mitigated, SMT off where that needs it, Speculative Store Bypass off for every
        process (:data:`~tundravm.declarative.utils.TEE_KERNEL_CMDLINE` says why each)."""
        return tuple(parameter for parameter, _reason in du.TEE_KERNEL_CMDLINE)

    def outbound(self) -> Egress:
        """With *time*, NTS key exchange (TCP 4460) to each of *nts_servers*. NTP on UDP 123
        goes where the key exchange says (RFC 8915 4.1.7), so the firewall accepts it to any
        address for chrony's user alone (:data:`CHRONY_NTP`). The ``Network`` names where DNS
        goes."""
        if not self.time:
            return Egress()
        servers = (f"[{s}]" if ":" in s else s for s in self.nts_servers)
        return Egress(hosts=[f"{server}:4460" for server in servers])

    rules = (
        Rule(
            "tee-base-clock",
            "warning",
            "TeeBase(time=False) leaves the guest on the host's clock, so the host decides "
            "certificate expiry and attestation freshness.",
            "Keep TeeBase(time=True), the default: authenticated NTS time.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``tee-base-clock``: an image without authenticated time trusts the host's clock."""
        if self.time:
            return ()
        return (
            _diagnostic(
                "tee-base-clock",
                "warning",
                "TeeBase(time=False): the guest clock is the host's, so certificate expiry "
                "and attestation freshness are the host's to decide",
            ),
        )


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
        fix = (
            "list what its services talk to with network=Network(egress=Egress(ports=[...], "
            "hosts=[...]))"
        )
        if self.egress is None:
            return (
                "the production profile accepts every outbound packet, so a compromised "
                f"service sends what it holds anywhere: {fix}"
            )
        everywhere = [host for host in self.egress.hosts if host.endswith("/0")]
        if everywhere:
            return (
                f"Egress(hosts=[{', '.join(map(repr, everywhere))}]) accepts every "
                f"destination, so it filters nothing: {fix}"
            )
        return None


class SshAccess(Component):
    """Root logs in over SSH with a public key, and with nothing else.

    *keys* are public keys baked into the image; *from_delivery* accepts the key the
    attested endpoint takes once ``tundravm attest`` verified the quote binding the host
    keys. *login* is an account other than root, or a list of them: a :class:`Login` gets
    in with its own keys into its forced command or shell, with its fixed ``sudo`` rules
    and nothing else. A key or a login is required. The port is
    opened in the firewall; host keys are generated per instance at boot. Exports
    :attr:`port`. Component name: ``ssh-access``.
    """

    keys: Sequence[str] = ()
    from_delivery: bool = False
    listen_port: int = 22
    login: Login | Sequence[Login] = ()

    def __post_init__(self) -> None:
        if isinstance(self.keys, str):
            raise ValidationError(
                "SshAccess: keys= takes a list of public keys.",
                hint="Pass keys=['ssh-ed25519 AAAA... me@host'].",
            )
        object.__setattr__(self, "keys", tuple(key.strip() for key in self.keys))
        for key in self.keys:
            if not _SSH_KEY.fullmatch(key):
                raise ValidationError(
                    f"SshAccess: {key!r} is not one OpenSSH public key line.",
                    hint="Paste the .pub file's line: 'ssh-ed25519 AAAA... comment'.",
                )
        logins = (self.login,) if isinstance(self.login, Login) else self.login
        if isinstance(logins, str) or not all(isinstance(item, Login) for item in logins):
            raise ValidationError(
                f"SshAccess: login= takes a Login or a list of them, got {self.login!r}.",
                hint='Pass login=Login("tenant", keys=[...], command="/usr/bin/app-status").',
            )
        object.__setattr__(self, "login", tuple(logins))
        if not self.keys and not self.from_delivery and not self.login:
            raise ValidationError(
                "SshAccess has no key to accept, so nobody could log in.",
                hint="Pass keys=[...], from_delivery=True, or login=Login(...).",
            )

    @property
    def port(self) -> TCP:
        """The port ``sshd`` listens on."""
        return TCP(self.listen_port)

    def compose(self) -> Iterator[object]:
        access = du.SshAccess(
            port=self.listen_port,
            authorized_keys=tuple(self.keys),
            from_secrets=self.from_delivery,
            logins=self.logins,
        )
        yield from access.items

    @property
    def logins(self) -> tuple[Login, ...]:
        """Every :class:`Login`, in the order given."""
        assert isinstance(self.login, tuple)
        return self.login

    rules = (
        Rule(
            "ssh-keys-baked",
            "warning",
            "SSH keys are baked into a cloud image, so every instance of it lets their holders "
            "in as root.",
            "Use SshAccess(from_delivery=True) so the control plane delivers the key at boot, "
            "or keep baked keys in a non-production profile with dev=DevAccess(...).",
        ),
        Rule(
            "ssh-login-shell",
            "warning",
            "A production login gets an interactive shell and no forced command, so whoever "
            "holds its key runs whatever that shell allows.",
            'Pin what it runs: Login("tenant", command="/usr/bin/app-status").',
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``ssh-keys-baked``: keys baked into a cloud image let in whoever holds them;
        ``ssh-login-shell``: a production login with a shell and no forced command."""
        found = [
            dm.Diagnostic(
                "ssh-login-shell",
                "warning",
                f"login {login.name!r} gets the shell {login.shell} and no command=: whoever "
                "holds its key runs whatever that shell allows",
                subject=login.name,
                hint=f'Login("{login.name}", command="/usr/bin/app-status") pins what it runs.',
            )
            for login in self.logins
            if resolved.production and login.shell is not None and login.command is None
        ]
        if self.keys and {"azure", "gcp"} & set(resolved.targets):
            found.append(
                _diagnostic(
                    "ssh-keys-baked",
                    "warning",
                    f"{len(self.keys)} SSH key(s) are baked into a cloud image: every instance "
                    "of it lets their holders in as root",
                )
            )
        return tuple(found)


class DevAccess(Component):
    """Debug access to a development build: ``dev=DevAccess(ssh_keys=["~/.ssh/id.pub"])``.

    The image gains a non-production ``dev`` profile holding it: root SSH by public key
    only, the debugging tools and a serial console. Each entry of *ssh_keys* is a public
    key, or the path of a ``.pub`` file read when the image lowers; a private key is
    refused. *password* is a root password for the serial console, never a login over the
    network. It is refused in ``components=``: debug access is a profile, never the image.
    Component name: ``dev-access``.
    """

    ssh_keys: Sequence[str] = ()
    password: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ssh_keys, str):
            raise ValidationError(
                "DevAccess: ssh_keys= takes a list of public keys, not one string.",
                hint="Pass ssh_keys=['~/.ssh/id_ed25519.pub'].",
            )
        object.__setattr__(self, "ssh_keys", tuple(self.ssh_keys))
        for key in self.ssh_keys:
            if not isinstance(key, str) or not key.strip():
                raise ValidationError(
                    f"DevAccess: ssh_keys= entry {key!r} is not a public key or a path.",
                    hint="Pass a public key line or the path of a .pub file.",
                )
            if "PRIVATE KEY" in key:
                raise ValidationError(
                    "DevAccess: ssh_keys= holds a private key.",
                    hint="Pass the .pub file or its contents.",
                )
        if self.password is not None and (not isinstance(self.password, str) or not self.password):
            raise ValidationError(
                "DevAccess: password= must be a non-empty string or None.",
                hint="Pass password='...' for a console login, or leave it out.",
            )
        if not self.ssh_keys and self.password is None:
            raise ValidationError(
                "DevAccess grants nothing: no SSH key and no password.",
                hint="Pass ssh_keys=['~/.ssh/id_ed25519.pub'].",
            )

    def public_keys(self) -> tuple[str, ...]:
        """The public key lines, reading every entry that names a ``.pub`` file."""
        found: list[str] = []
        for entry in self.ssh_keys:
            candidate = Path(entry).expanduser()
            if "\n" not in entry and candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
                found.extend(line.strip() for line in text.splitlines() if line.strip())
            elif entry.strip().endswith(".pub") and " " not in entry.strip():
                raise ValidationError(
                    f"DevAccess: ssh_keys= names {entry}, which does not exist.",
                    hint="Pass the path of an existing .pub file (relative paths are read from "
                    "the working directory), or the key's text.",
                )
            else:
                found.extend(line.strip() for line in entry.splitlines() if line.strip())
        return tuple(dict.fromkeys(found))

    def tools(self) -> tuple[object, ...]:
        """The debugging tools and, with *password*, the console login."""
        if self.password is None:
            return ()
        return (du.DevTools(root_password=self.password),)

    def compose(self) -> Iterator[object]:
        keys = self.public_keys()
        if keys:
            yield du.SshAccess(authorized_keys=keys)
        yield from self.tools()

    rules = (
        Rule(
            "dev-access-rsa-key",
            "info",
            "A development profile accepts an RSA SSH key where every current client makes "
            "ed25519.",
            "Make an ed25519 key (ssh-keygen -t ed25519) and pass its .pub instead.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``dev-access-rsa-key``: an RSA key where every current client makes ed25519."""
        rsa = [key for key in self.public_keys() if key.startswith("ssh-rsa ")]
        if not rsa:
            return ()
        return (
            _diagnostic(
                "dev-access-rsa-key",
                "info",
                f"profile {resolved.profile!r} accepts {len(rsa)} RSA SSH key(s): prefer "
                "ed25519 (ssh-keygen -t ed25519)",
            ),
        )


class Tdxs(Component):
    """The evidence service: ``tdxs`` from ``tools/`` of tundravm's repository,
    socket-activated on ``/var/tdxs.sock``, which produces the TDX quote an attestation is
    checked against.

    *source* defaults to ``tools/`` at the installed SDK's own commit, or the checkout's
    ``tools/`` when tundravm runs from one (``tools_source()``); the lock pins it. Every
    image holds ``Tdxs()``; passing one in ``components=`` replaces it, which is how an
    image points at a mirror (``Tdxs(source=Git("https://mirror.example/tundra", COMMIT,
    subdir="tools"))``) or also validates peers' quotes (*validator*,
    *expected_measurements*, or :meth:`from_policy`). The same *source* builds the volume
    and delivery tools (``key-gen``, ``disk-setup``, ``attested-endpoint``) when the image has
    volumes or secrets, so one input names all of them.

    The issuer follows the build, not the image: a production profile issues TDX quotes
    only; a development profile also ships the simulator issuer and takes it at boot where
    the execution gate finds no TDX quote path, so ``attest`` of a dev build under QEMU
    answers ``simulated`` (``tundravm._modules.tdxs``).

    Services that ask for quotes join the ``tdx`` group and require :data:`EVIDENCE`
    (``tdxs.ready``). It depends on nothing on a volume or a secret: the evidence is
    what unlocks them. Component name: ``tdxs``.
    """

    source: dm.Git | dm.Local = field(default_factory=du.tools_source)
    validator: du.TdxsType | None = None
    expected_measurements: Mapping[str, str] = FrozenMap()
    check_revocations: bool = False
    get_collateral: bool = False
    verify_imds: bool = False
    verify_identity_token: bool = False

    @classmethod
    def from_policy(
        cls,
        policy: str | Path | Mapping[str, object],
        *,
        validator: du.TdxsType = "tdx",
        allow_placeholder: bool = False,
        **kwargs: Any,
    ) -> Tdxs:
        """A verifier whose ``expected_measurements`` are *policy*'s registers.

        *policy* is the verifier policy (a ``measurements.json`` record's ``policy``) of
        the image you trust (or its dict); a placeholder policy is refused unless
        *allow_placeholder*.
        Other keyword arguments are fields of this class.
        """
        fields = {k: kwargs.pop(k) for k in ("source",) if k in kwargs}
        reader = du.Tdxs.from_policy(
            policy, validator=validator, allow_placeholder=allow_placeholder, **fields, **kwargs
        )
        return cls(
            validator=validator,
            expected_measurements=FrozenMap(dict(reader.expected_measurements)),
            check_revocations=reader.check_revocations,
            get_collateral=reader.get_collateral,
            verify_imds=reader.verify_imds,
            verify_identity_token=reader.verify_identity_token,
            **fields,
        )

    @property
    def socket(self) -> str:
        """Where a service asks for quotes (its ``tdx`` group may connect)."""
        return _tdxs.Tdxs().socket_path

    @property
    def group(self) -> str:
        """The group whose members may ask for quotes."""
        return _tdxs.Tdxs().group

    def readiness(self) -> tuple[Readied, ...]:
        return (EVIDENCE,)

    def outbound(self) -> Egress:
        """Intel's PCS over HTTPS with *get_collateral* or *check_revocations*, and the root
        CA's CRL with *check_revocations*; nothing otherwise (quotes are made in the guest)."""
        fetches = self.get_collateral or self.check_revocations
        hosts = [INTEL_PCS] if fetches else []
        return Egress(hosts=[*hosts, INTEL_CERTIFICATES] if self.check_revocations else hosts)

    def compose(self) -> Iterator[object]:
        if not isinstance(self.source, (dm.Git, dm.Local)):
            raise ValidationError(
                f"Tdxs: source {self.source!r} is not a Git or a Local source.",
                hint='Pass source=Git(url, COMMIT, subdir="tools"): a mirror of the tools.',
            )
        service = du.Tdxs(
            source=self.source,
            validator=self.validator,
            expected_measurements=dict(self.expected_measurements),
            check_revocations=self.check_revocations,
            get_collateral=self.get_collateral,
            verify_imds=self.verify_imds,
            verify_identity_token=self.verify_identity_token,
        )
        yield from service.items

    rules = (
        Rule(
            "tdxs-validator-unchecked",
            "warning",
            "Tdxs validates peers' quotes with no expected measurements, so it accepts any "
            "genuine TD whatever it runs.",
            "Pass Tdxs(expected_measurements=...), or build it with Tdxs.from_policy(POLICY).",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``tdxs-validator-unchecked``: a validator with nothing to compare a quote to."""
        if self.validator is None or self.expected_measurements:
            return ()
        return (
            _diagnostic(
                "tdxs-validator-unchecked",
                "warning",
                f"Tdxs(validator={self.validator!r}) has no expected measurements: it accepts "
                "any genuine TD, whatever it runs",
            ),
        )


def egress_rules(contributions: Iterable[tuple[str, Egress]]) -> tuple[dm.EgressRule, ...]:
    """The output-chain rules *contributions* (``(who, egress)`` pairs) ask for: one per
    destination, with everyone who asked for it, in the order asked; a host name is its
    own destination, matched by the addresses it resolves to at boot; *metadata* is one
    rule per cloud metadata server."""
    found: dict[tuple[dm.Port | None, str | None, str | None], list[str]] = {}
    for who, egress in contributions:
        if egress.metadata:
            for host in dm.CLOUD_METADATA:
                found.setdefault((None, host, None), []).append(who)
        for value in egress.ports:
            found.setdefault((dm.Port(value.port, value.protocol), None, None), []).append(who)
        for entry in egress.hosts:
            host, number, named = egress_host(egress, entry)
            port = None if number is None else dm.Port(number)
            key = (port, None, host) if named else (port, host, None)
            found.setdefault(key, []).append(who)
    return tuple(
        dm.EgressRule(port, host, tuple(dict.fromkeys(origins)), name=name)
        for (port, host, name), origins in found.items()
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
