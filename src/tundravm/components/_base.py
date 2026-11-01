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
from pathlib import Path
from typing import Any, Literal

from tundravm._image._component import Component, Readied, ServiceReady
from tundravm._image._values import (
    MANAGEMENT_PORT,
    TCP,
    UDP,
    Egress,
    Notify,
    PortValue,
    StaticAddress,
    egress_host,
)
from tundravm._modules import tdxs as _tdxs
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

__all__ = [
    "EVIDENCE",
    "INTEL_PCS",
    "DevAccess",
    "Firewall",
    "Network",
    "SshAccess",
    "Tdxs",
    "TeeBase",
    "egress_rules",
]

EVIDENCE = ServiceReady("tdxs.service", Notify())
"""The implied evidence service is ready: what a service that asks for quotes requires."""

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
    hardening, a static resolver and no apt timers or console getty.

    *time*: chrony over NTS from *nts_servers*, refined by the hypervisor clocks, so the
    host cannot set the guest's clock (TLS, certificates and attestation freshness
    depend on it). *journal*: journald in RAM, capped at *journal_max_use*. *sysctl*: the
    kernel hardening set. *dns*: ``/etc/resolv.conf`` naming *nameservers*. *mask*: no
    apt timers, no getty on the host-owned console. Component name: ``tee-base``.
    """

    time: bool = True
    nts_servers: Sequence[str] = du.TEE_NTS_SERVERS
    journal: bool = True
    journal_max_use: str = "128M"
    sysctl: bool = True
    dns: bool = True
    nameservers: Sequence[str] = du.TEE_NAMESERVERS
    mask: bool = True

    def compose(self) -> Iterator[object]:
        base = du.TeeBase(
            time=self.time,
            nts_servers=tuple(self.nts_servers),
            journal=self.journal,
            journal_max_use=self.journal_max_use,
            sysctl=self.sysctl,
            dns=self.dns,
            nameservers=tuple(self.nameservers),
            mask=self.mask,
            network=False,
            firewall=False,
        )
        yield from base.items

    def outbound(self) -> Egress:
        """NTS time (TCP 4460, NTP on UDP 123) with *time*, *nameservers* with *dns*."""
        return Egress(
            ports=[TCP(4460), UDP(123)] if self.time else [],
            hosts=list(self.nameservers) if self.dns else [],
        )

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
    *egress* filters what the image may send (the image's firewall applies it).
    ``image.network`` takes it, and so does ``components=``. Exports :attr:`online`.
    Component name: ``network``.
    """

    dhcp: bool = True
    static: StaticAddress | None = None
    egress: Egress | None = None
    interfaces: Sequence[str] = ("en*", "eth*")

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
        if isinstance(self.interfaces, str):
            raise ValidationError(
                "Network: interfaces= takes a list of names or globs.",
                hint="Pass interfaces=['en*', 'eth*'].",
            )
        object.__setattr__(self, "interfaces", tuple(self.interfaces))
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
        network = du.Network(dhcp=self.dhcp, interfaces=tuple(self.interfaces), static=self.static)
        yield from network.items

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


class Firewall(Component):
    """The inbound (and optionally outbound) packet filter, nftables in the image.

    Inbound it drops everything except the ports services ``expose=``, an ingress's port,
    the management ingress on :data:`~tundravm._image._values.MANAGEMENT_PORT` and *allow*
    (ports something other than a ``Service`` listens on: a packaged daemon). Outbound it
    filters only with *egress*, and then accepts what the baseline and every component's
    ``outbound()`` send as well, each rule commented with who asked for it; a component a
    ``Profile`` adds joins that profile's rules only. The image holds ``Firewall()`` by
    default;
    ``network=Network(egress=...)`` is the usual way to set *egress*. Component name:
    ``firewall``.
    """

    allow: Sequence[PortValue] = ()
    egress: Egress | None = None

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

    def compose(self) -> Iterator[object]:
        allow = (dm.Port(MANAGEMENT_PORT), *(dm.Port(p.port, p.protocol) for p in self.allow))
        if self.egress is None:
            yield dm.Firewall(allow=allow)
            return
        yield dm.Firewall(
            allow=allow, egress="filter", egress_rules=egress_rules((("image", self.egress),))
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
            "hosts=[...])); what the baseline sends (NTS time, resolvers, cloud metadata) is "
            "accepted for you. A Profile(production=False) is exempt.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``firewall-ssh-open``: port 22 opened by hand instead of by ``SshAccess``;
        ``egress-unfiltered``: a production profile that sends anywhere."""
        found: list[dm.Diagnostic] = []
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
    control plane delivers at boot instead (today's ``secret-delivery`` bootstrap on the
    management port, which ``sshd`` waits for). One of them is required. The port is
    opened in the firewall; host keys are generated per instance at boot. Exports
    :attr:`port`. Component name: ``ssh-access``.
    """

    keys: Sequence[str] = ()
    from_delivery: bool = False
    listen_port: int = 22

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
        if not self.keys and not self.from_delivery:
            raise ValidationError(
                "SshAccess has no key to accept, so nobody could log in.",
                hint="Pass keys=[...], or from_delivery=True.",
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
        )
        yield from access.items

    rules = (
        Rule(
            "ssh-keys-baked",
            "warning",
            "SSH keys are baked into a cloud image, so every instance of it lets their holders "
            "in as root.",
            "Use SshAccess(from_delivery=True) so the control plane delivers the key at boot, "
            "or keep baked keys in a non-production profile with dev=DevAccess(...).",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``ssh-keys-baked``: keys baked into a cloud image let in whoever holds them."""
        if not self.keys or not {"azure", "gcp"} & set(resolved.targets):
            return ()
        return (
            _diagnostic(
                "ssh-keys-baked",
                "warning",
                f"{len(self.keys)} SSH key(s) are baked into a cloud image: every instance "
                "of it lets their holders in as root",
            ),
        )


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
    """The evidence service: ``tdxs`` from tundra-tools, socket-activated on
    ``/var/tdxs.sock``, which produces the TDX quote an attestation is checked against.

    Every image holds ``Tdxs()``; passing one in ``components=`` replaces it, which is how
    an image points at a mirror or a vendored checkout of tundra-tools
    (``Tdxs(source=Git("https://mirror.example/tundra-tools.git", "master"))``; the lock
    pins it) or also validates peers' quotes (*validator*, *expected_measurements*, or
    :meth:`from_policy`). The same *source* builds the volume and delivery tools
    (``key-gen``, ``disk-setup``, ``secret-delivery``) when the image has volumes or
    secrets, so one input names all of tundra-tools.

    Services that ask for quotes join the ``tdx`` group and require :data:`EVIDENCE`
    (``tdxs.ready``). It depends on nothing on a volume or a secret: the evidence is
    what unlocks them. Component name: ``tdxs``.
    """

    source: dm.Git = du.TUNDRA_TOOLS
    issuer: du.TdxsType | None = "tdx"
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
        fields = {k: kwargs.pop(k) for k in ("source", "issuer") if k in kwargs}
        reader = du.Tdxs.from_policy(
            policy, validator=validator, allow_placeholder=allow_placeholder, **kwargs
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
        if not isinstance(self.source, dm.Git):
            raise ValidationError(
                f"Tdxs: source {self.source!r} is not a Git source.",
                hint="Pass source=Git(url, ref): a mirror or a local checkout of tundra-tools.",
            )
        service = du.Tdxs(
            source=self.source,
            issuer=self.issuer,
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
    destination, with every ``(who, host name)`` that asked for it, in the order asked."""
    found: dict[tuple[dm.Port | None, str | None], list[tuple[str, str]]] = {}
    for who, egress in contributions:
        for value in egress.ports:
            found.setdefault((dm.Port(value.port, value.protocol), None), []).append((who, ""))
        for entry in egress.hosts:
            host, number, named = egress_host(egress, entry)
            port = None if number is None else dm.Port(number)
            key = (port, None) if named else (port, host)
            found.setdefault(key, []).append((who, host if named else ""))
    return tuple(
        dm.EgressRule(port, host, tuple(dict.fromkeys(origins)))
        for (port, host), origins in found.items()
    )
