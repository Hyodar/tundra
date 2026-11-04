"""The confidential-VM baseline: ``TeeBase`` (the TD's own hardening) and ``Tdxs``, the
evidence service; :data:`EVIDENCE` is its readiness. An image implies both; passing one in
``components=`` replaces that default.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import field
from pathlib import Path
from typing import Any

from tundravm._image._component import Component, Readied, ServiceReady
from tundravm._image._values import (
    Egress,
    Notify,
)
from tundravm._modules import tdxs as _tdxs
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

from ._base import _diagnostic

__all__ = [
    "CHRONY_NTP",
    "EVIDENCE",
    "INTEL_PCS",
    "Tdxs",
    "TeeBase",
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
