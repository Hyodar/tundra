"""``attest``: ask a running image's ``tdxs`` issuer for a quote, have a validator judge it.

The ``tdxs`` service (``tundra-tools``) speaks newline-delimited JSON over a
stream socket (``/var/tdxs.sock`` in the image, socket-activated). A request
is ``{"method": "issue", "data": {"userData": HEX, "nonce": HEX}}`` and the
reply ``{"data": {"document": HEX}, "error": null}``, where *document* is the
hex of the JSON ``AttestationDocument`` (``raw_quote``, ``user_data``,
``nonce`` base64; ``platform``). The quote's ``report_data`` is
``SHA-256(userData || nonce)`` followed by 32 zero bytes. ``{"method":
"metadata", "data": {}}`` returns the issuer's measurements, which the
simulator issuer (whose quote is not a TDX quote) is checked with.

Authenticity (the quote's signature, certificate chain and collateral) is the
job of a ``tdxs`` validator: ``{"method": "validate", "data": {"document":
HEX, "nonce": HEX}}`` answers ``{"data": {"userData": HEX, "valid": true},
"error": null}`` for a document it accepts and an ``error`` for one it
rejects. Without a validator the verdict says only whether the measurements
match; it is never ``trusted``. A positive verdict also needs coverage: MRTD and
RTMR0 (the platform firmware, from the scheme or the policy's ``platform_approval``)
and RTMR1 and RTMR2 (the image) must each match an accepted value; otherwise nothing
disagreed but trust is not established and the verdict is ``partial``. Azure policies
are refused: their image measurements are vTPM PCRs, which this path cannot verify.

:meth:`Attestation.save` keeps what was observed as a ``tundravm.attestation/2``
record: the document as received, the nonce, the policy's sha256 and artifact, the
validator's whole reply and the verdict. :func:`read_attestation` re-derives the
quote's registers, the nonce binding and the verdict from that record offline. The
quote's signature, certificate chain and collateral cannot be re-checked offline:
they are only as trusted as the recorded validator reply, which the record's
producer asserts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import socket
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from .errors import AttestationError, MeasurementError, ValidationError
from .formats import md_cell, md_table
from .measure.policy import (
    IMAGE_REGISTERS,
    PLATFORM_REGISTERS,
    approved_registers,
    is_placeholder,
    match_registers,
    read_policy,
)

Transport = Callable[[Mapping[str, object]], Mapping[str, object]]
"""Sends one tdxs request envelope (``{"method", "data"}``) and returns the reply envelope."""
RegisterVerdict = Literal["match", "mismatch", "unchecked"]
Verdict = Literal[
    "trusted", "partial", "untrusted", "measurements-match", "measurements-mismatch", "simulated"
]
"""``trusted``/``untrusted`` need a validator; without one the verdict is ``measurements-*``.
``partial``: nothing disagreed, but the platform (MRTD/RTMR0) or the image (RTMR1/RTMR2) was
not checked against an approved value, so trust is not established."""
SignatureStatus = Literal["valid", "invalid", "unchecked"]

REGISTERS = ("MRTD", "RTMR0", "RTMR1", "RTMR2", "RTMR3")
"""The registers :func:`attest` reports, in order."""
VERDICTS: tuple[Verdict, ...] = (
    "trusted",
    "partial",
    "untrusted",
    "measurements-match",
    "measurements-mismatch",
    "simulated",
)
"""Every verdict :func:`attest` returns; ``trusted`` and ``measurements-match`` pass,
``partial`` only with ``accept_partial``."""
CHECKS_NOTE = (
    "the quote's signature, certificate chain and collateral are checked only by a "
    "tdxs validator (--validator); without one only measurements and the nonce are checked"
)
TIMEOUT_S = 30.0
SIMULATOR = "simulator"
ATTESTATION_SCHEMA = "tundravm.attestation/2"
"""The ``schema`` of the record :meth:`Attestation.save` (``attest --record``) writes.
Version 2 adds the ``partial`` verdict: a version 1 record could say ``trusted`` without the
platform registers approved, so it is no longer read."""
TRUST_NOTE = (
    "offline re-checking covers the quote's measurements and its nonce binding; the "
    "quote's signature, certificate chain and collateral are only as trusted as the "
    "recorded validator reply, which the record's producer asserts"
)
"""What re-reading a record (:func:`read_attestation`) proves, and what it takes on trust."""
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PARTIAL_NOTE = (
    "partial: trusted needs MRTD and RTMR0 (the platform firmware) and RTMR1 and RTMR2 (the "
    "image) each matched; approve the platform values from a quote of a host you trust with "
    "`tundravm measure --export-policy FILE --approve-mrtd HEX --approve-rtmr0 HEX`"
)
"""What a ``partial`` verdict lacks and how to close it."""

TDX_TEE_TYPE = 0x81
QUOTE_HEADER_SIZE = 48
TD_REPORT_BODY_SIZE = 584
_V5_BODY_TYPES = {2: 584, 3: 648}
"""Quote v5 body types and sizes: TDX 1.0 and TDX 1.5 report bodies (1.0 is a prefix of 1.5)."""
_BODY_FIELDS: dict[str, tuple[int, int]] = {
    "mrseam": (16, 48),
    "xfam": (128, 8),
    "mrtd": (136, 48),
    "mrowner": (232, 48),
    "rtmr0": (328, 48),
    "rtmr1": (376, 48),
    "rtmr2": (424, 48),
    "rtmr3": (472, 48),
    "report_data": (520, 64),
}
"""Offset and size of each field the parser reads, within the TD report body."""


@dataclass(frozen=True, slots=True)
class Quote:
    """The fields of a TDX quote (DCAP v4, or v5) that :func:`attest` checks."""

    version: int
    mrtd: str
    rtmrs: tuple[str, str, str, str]
    report_data: bytes
    mrseam: str
    mrowner: str
    xfam: str

    def registers(self) -> dict[str, str]:
        """``MRTD`` and ``RTMR0``..``RTMR3`` as lower-case hex."""
        return {"MRTD": self.mrtd, **{f"RTMR{i}": value for i, value in enumerate(self.rtmrs)}}


def parse_quote(raw: bytes) -> Quote:
    """The TD report body fields of a raw TDX quote; ``MeasurementError`` if it is not one.

    The header is 48 bytes: ``version`` (u16), ``att_key_type`` (u16),
    ``tee_type`` (u32, ``0x81`` for TDX), then QE/PCE SVNs, vendor id and user
    data. A v4 quote's 584-byte TD report body follows at offset 48; a v5
    quote has a body descriptor there (``type`` u16, ``size`` u32) and the body
    at 54.
    """
    hint = "The issuer must be a tdx, azure or gcp tdxs issuer running in a TD."
    if len(raw) < QUOTE_HEADER_SIZE:
        raise MeasurementError(
            f"The quote is {len(raw)} bytes, shorter than a TDX quote header.", hint=hint
        )
    version, _, tee_type = struct.unpack_from("<HHI", raw, 0)
    if tee_type != TDX_TEE_TYPE:
        raise MeasurementError(f"The quote's tee_type is {tee_type:#x}, not TDX (0x81).", hint=hint)
    if version == 4:
        offset, size = QUOTE_HEADER_SIZE, TD_REPORT_BODY_SIZE
    elif version == 5:
        if len(raw) < QUOTE_HEADER_SIZE + 6:
            raise MeasurementError("The v5 quote has no body descriptor.", hint=hint)
        body_type, size = struct.unpack_from("<HI", raw, QUOTE_HEADER_SIZE)
        if _V5_BODY_TYPES.get(body_type) != size:
            raise MeasurementError(
                f"The v5 quote's body is type {body_type} of {size} bytes, not a TD report.",
                hint=hint,
            )
        offset = QUOTE_HEADER_SIZE + 6
    else:
        raise MeasurementError(
            f"Unsupported TDX quote version {version}.",
            hint="tundravm reads DCAP quote versions 4 and 5.",
        )
    if len(raw) < offset + size:
        raise MeasurementError(
            f"The quote is truncated: {len(raw)} bytes, its report body ends at {offset + size}.",
            hint=hint,
        )

    def field(name: str) -> bytes:
        start, length = _BODY_FIELDS[name]
        return raw[offset + start : offset + start + length]

    rtmrs = tuple(field(f"rtmr{i}").hex() for i in range(4))
    return Quote(
        version=version,
        mrtd=field("mrtd").hex(),
        rtmrs=(rtmrs[0], rtmrs[1], rtmrs[2], rtmrs[3]),
        report_data=field("report_data"),
        mrseam=field("mrseam").hex(),
        mrowner=field("mrowner").hex(),
        xfam=field("xfam").hex(),
    )


def report_data_for(user_data: bytes, nonce: bytes) -> bytes:
    """The 64-byte ``report_data`` a tdxs issuer binds: ``SHA-256(user_data || nonce)``, zeros."""
    return hashlib.sha256(user_data + nonce).digest() + bytes(32)


@dataclass(frozen=True, slots=True)
class RegisterCheck:
    """One register: the quote's value, the policy's accepted values (empty: not in the
    policy), the verdict and the label of the accepted value it matched."""

    name: str
    verdict: RegisterVerdict
    actual: str | None
    expected: tuple[str | Mapping[str, str], ...] = ()
    matched: str | None = None

    @property
    def values(self) -> tuple[str, ...]:
        """The accepted values, without their labels."""
        return tuple(e if isinstance(e, str) else str(e.get("value")) for e in self.expected)

    def to_dict(self) -> dict[str, object]:
        return {
            "actual": self.actual,
            "expected": [e if isinstance(e, str) else dict(e) for e in self.expected],
            "matched": self.matched,
            "verdict": self.verdict,
        }


@dataclass(frozen=True, slots=True)
class Attestation:
    """What :func:`attest` found: the nonce and register checks, the validator's say, the verdict.

    ``source`` is ``quote`` when the registers were read from the TDX quote, or
    ``metadata`` for the simulator issuer, whose registers come from its
    ``metadata`` reply. ``signature_status`` is ``valid`` or ``invalid`` when a
    validator judged the document and ``unchecked`` without one.
    :attr:`verdict` is ``trusted`` only when the validator accepted the document,
    the full 64-byte ``report_data`` binds the nonce, every register the policy
    holds matched, and MRTD, RTMR0 (the platform: the scheme's values or the
    policy's ``platform_approval``), RTMR1 and RTMR2 (the image) each matched an
    accepted value. Nothing disagreeing but one of those unchecked is ``partial``;
    a simulator issuer's quote is never trusted.
    """

    issuer: str
    platform: str
    source: Literal["quote", "metadata"]
    quote_version: int | None
    nonce: str
    report_data: str
    expected_report_data: str
    registers: tuple[RegisterCheck, ...]
    validator: str | None = None
    signature_status: SignatureStatus = "unchecked"
    validator_error: str | None = None
    policy: str | None = None
    allow_simulated: bool = False
    accept_partial: bool = False
    """``partial`` passes (``attest --accept-partial``); it is still never ``trusted``."""
    document: str = field(default="", repr=False)
    """The tdxs attestation document exactly as the issuer sent it: the hex of its JSON."""
    user_data: str = ""
    """The ``userData`` (hex) the issue request carried; ``report_data`` binds it."""
    metadata: Mapping[str, object] | None = field(default=None, repr=False, hash=False)
    """A simulator issuer's ``metadata`` reply, whose measurements are its registers."""
    validator_reply: Mapping[str, object] | None = field(default=None, repr=False, hash=False)
    """The validator's whole reply envelope; ``None`` without a validator."""
    policy_sha256: str | None = None
    """The sha256 of the policy file (of its sorted JSON for a dict policy)."""
    policy_artifact: Mapping[str, object] | None = field(default=None, hash=False)
    """The policy's ``artifact`` (``path``, ``sha256``): what it measured."""
    policy_scheme: str | None = None
    """The policy's ``scheme``; an ``azure`` one is never trusted (its image is in the vTPM)."""
    observed_at: str = ""
    """When the quote was requested: ISO 8601 UTC, to the second."""

    @property
    def nonce_matches(self) -> bool:
        """The quote's whole ``report_data`` is ``SHA-256(userData || nonce)`` + 32 zero bytes."""
        return self.report_data == self.expected_report_data

    @property
    def simulated(self) -> bool:
        return self.platform == SIMULATOR

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why the verdict is not positive, in order.

        ``validator-rejected``, ``nonce-mismatch``, ``register-mismatch``, ``simulated``,
        ``platform-unverified`` (MRTD or RTMR0 unchecked: no approved value),
        ``image-unverified`` (RTMR1 or RTMR2 unchecked).
        """
        found: list[str] = []
        if self.signature_status == "invalid":
            found.append("validator-rejected")
        if not self.nonce_matches:
            found.append("nonce-mismatch")
        if any(r.verdict == "mismatch" for r in self.registers):
            found.append("register-mismatch")
        if self.simulated:
            found.append("simulated")
        unchecked = {r.name for r in self.registers if r.verdict == "unchecked"}
        if unchecked & set(PLATFORM_REGISTERS):
            found.append("platform-unverified")
        if unchecked & set(IMAGE_REGISTERS):
            found.append("image-unverified")
        if self.policy_scheme == "azure":
            found.append("vtpm-unverified")
        return tuple(found)

    @property
    def covered(self) -> bool:
        """MRTD, RTMR0, RTMR1 and RTMR2 are each checked: the quote holds them and so does
        the policy (its registers or ``platform_approval``)."""
        checked = {r.name for r in self.registers if r.verdict != "unchecked"}
        full = checked >= {*PLATFORM_REGISTERS, *IMAGE_REGISTERS}
        return full and self.policy_scheme != "azure"

    @property
    def verdict(self) -> Verdict:
        measured = self.nonce_matches and all(r.verdict != "mismatch" for r in self.registers)
        if self.signature_status == "invalid":
            return "untrusted"
        if not measured:
            return "untrusted" if self.signature_status == "valid" else "measurements-mismatch"
        if self.simulated:
            return "simulated"
        if not self.covered:
            return "partial"
        return "trusted" if self.signature_status == "valid" else "measurements-match"

    @property
    def trusted(self) -> bool:
        return self.verdict == "trusted"

    @property
    def passed(self) -> bool:
        """``trusted`` or ``measurements-match``; ``simulated`` too with ``allow_simulated``,
        ``partial`` with ``accept_partial``."""
        verdict = self.verdict
        return (
            verdict in ("trusted", "measurements-match")
            or (verdict == "simulated" and self.allow_simulated)
            or (verdict == "partial" and self.accept_partial)
        )

    def to_dict(self) -> dict[str, object]:
        """The JSON form ``attest --format json`` prints (sorted keys)."""
        return {
            "checks": CHECKS_NOTE,
            "issuer": self.issuer,
            "nonce": {
                "value": self.nonce,
                "report_data": self.report_data,
                "expected_report_data": self.expected_report_data,
                "verdict": "match" if self.nonce_matches else "mismatch",
            },
            "passed": self.passed,
            "platform": self.platform,
            "policy": self.policy,
            "quote_version": self.quote_version,
            "reasons": list(self.reasons),
            "registers": {r.name: r.to_dict() for r in self.registers},
            "signature_status": self.signature_status,
            "source": self.source,
            "trusted": self.trusted,
            "validator": self.validator,
            "validator_error": self.validator_error,
            "verdict": self.verdict,
        }

    def record(self) -> dict[str, object]:
        """The ``tundravm.attestation/2`` record :meth:`save` writes (``attest --record``).

        It keeps what a later offline check needs: the ``document`` as received,
        ``user_data`` and ``nonce``, ``report_data`` (``expected``, ``actual``,
        ``verdict``), ``platform``, ``source``, ``quote_version``, the simulator's
        ``metadata`` reply, ``registers``, the ``policy`` (``path``, ``sha256``,
        ``artifact``, ``scheme``), the ``validator`` (``endpoint`` and its whole ``reply``, or
        ``null``), ``signature_status``, ``verdict``, ``reasons``, ``observed_at``,
        the ``tundravm`` version and the ``trust`` note.
        """
        from tundravm import __version__

        return {
            "schema": ATTESTATION_SCHEMA,
            "tundravm": __version__,
            "observed_at": self.observed_at,
            "trust": TRUST_NOTE,
            "issuer": self.issuer,
            "document": self.document,
            "user_data": self.user_data,
            "nonce": self.nonce,
            "report_data": {
                "expected": self.expected_report_data,
                "actual": self.report_data,
                "verdict": "match" if self.nonce_matches else "mismatch",
            },
            "platform": self.platform,
            "source": self.source,
            "quote_version": self.quote_version,
            "metadata": None if self.metadata is None else dict(self.metadata),
            "registers": {r.name: r.to_dict() for r in self.registers},
            "policy": {
                "path": self.policy,
                "sha256": self.policy_sha256,
                "artifact": None if self.policy_artifact is None else dict(self.policy_artifact),
                "scheme": self.policy_scheme,
            },
            "validator": None
            if self.validator is None
            else {
                "endpoint": self.validator,
                "reply": None if self.validator_reply is None else dict(self.validator_reply),
            },
            "signature_status": self.signature_status,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }

    def save(self, path: str | Path) -> Path:
        """Write :meth:`record` to *path* as sorted, indented JSON (parents created)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.record(), indent=2, sort_keys=True) + "\n", "utf-8")
        return target


def attest(
    issuer: str,
    policy: str | Path | Mapping[str, object],
    *,
    validator: str | None = None,
    nonce: str | bytes | None = None,
    allow_simulated: bool = False,
    accept_partial: bool = False,
    transport: Transport | None = None,
    validator_transport: Transport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Attestation:
    """Request a quote from the tdxs issuer at *issuer*, check it against *policy*.

    *issuer* and *validator* are ``unix:PATH`` (or a path) or
    ``tcp://HOST:PORT``; both speak tdxs's JSON lines. *policy* is a file
    ``measure --export-policy`` wrote, or its dict; a placeholder policy is
    refused. *nonce* (hex or bytes; default 32 random bytes) must come back in
    the quote's full 64-byte ``report_data``. With *validator* the document and
    nonce go to that tdxs validator, whose answer decides authenticity
    (``trusted``/``untrusted``); without it the verdict is
    ``measurements-match`` or ``measurements-mismatch``. Either needs MRTD, RTMR0,
    RTMR1 and RTMR2 each checked: a policy that cannot predict the platform registers
    (qemu, portable) needs its ``platform_approval``, else the verdict is ``partial``
    (*accept_partial* lets it pass). An azure policy is refused: its image measurements
    are vTPM PCRs, which this path does not verify. A simulator issuer's
    verdict is ``simulated`` at best; *allow_simulated* lets it pass.
    *transport* and *validator_transport* replace the network and *clock* the wall
    clock ``observed_at`` reads (tests; ``SOURCE_DATE_EPOCH`` is not honoured, since a
    pinned clock would make every record look fresh). Raises
    ``AttestationError`` when the issuer or validator cannot be reached or
    fails, ``MeasurementError`` for a reply that is not a TDX attestation.
    """
    data = read_policy(policy)
    policy_bytes = _canonical(policy) if isinstance(policy, Mapping) else Path(policy).read_bytes()
    if is_placeholder(data):
        raise MeasurementError(
            "Refusing to attest against a placeholder policy.",
            hint="Export the policy from a real measurement: "
            "`tundravm measure --scheme SCHEME --export-policy FILE` on a real bake.",
            context={"policy": "dict" if isinstance(policy, Mapping) else str(policy)},
        )
    if data.get("scheme") == "azure":
        raise AttestationError(
            "Refusing to attest against an azure policy: Azure's paravisor owns the TD, so "
            "the TDX quote measures Azure's firmware and the image is measured into the vTPM "
            "(the policy's vtpm_pcrs), which this check does not verify.",
            hint="Azure TDX trust needs vTPM evidence (a TPM quote bound to the TD and its "
            "PCR4/9/11), which tundravm does not support yet; verify vtpm_pcrs with a vTPM "
            "verifier, or attest a qemu, gcp or portable image.",
            context={"policy": "dict" if isinstance(policy, Mapping) else str(policy)},
        )
    expected = approved_registers(data)
    challenge = _nonce_bytes(nonce)
    observed = (clock or _now)().astimezone(UTC).strftime(TIME_FORMAT)
    send = transport if transport is not None else connect(issuer)
    reply = _call(send, issuer, "issue", {"userData": "", "nonce": challenge.hex()})
    encoded = str(reply.get("document") or "")
    document = _document(encoded, issuer)
    platform = str(document.get("platform") or "")
    raw = _b64(document.get("raw_quote"), "raw_quote")
    wanted = report_data_for(b"", challenge)
    meta: Mapping[str, object] | None = None
    if platform == SIMULATOR:
        meta = _call(send, issuer, "metadata", {})
    source, version, actual, bound = _measured(platform, raw, meta, issuer)
    status: SignatureStatus = "unchecked"
    rejection = None
    answer: Mapping[str, object] | None = None
    if validator is not None:
        judge = validator_transport if validator_transport is not None else connect(validator)
        status, rejection, answer = _validate(judge, validator, encoded, challenge)
    checks = _checks(expected, actual)
    artifact = data.get("artifact")
    return Attestation(
        issuer=issuer,
        platform=platform or "unknown",
        source=source,
        quote_version=version,
        nonce=challenge.hex(),
        report_data=bound.hex(),
        expected_report_data=wanted.hex(),
        registers=checks,
        validator=validator,
        signature_status=status,
        validator_error=rejection,
        policy=None if isinstance(policy, Mapping) else str(policy),
        allow_simulated=allow_simulated,
        accept_partial=accept_partial,
        document=encoded,
        user_data="",
        metadata=None if meta is None else dict(meta),
        validator_reply=answer,
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        policy_artifact=dict(artifact) if isinstance(artifact, Mapping) else None,
        policy_scheme=str(data.get("scheme")),
        observed_at=observed,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(policy: Mapping[str, object]) -> bytes:
    """A dict policy as ``measure --export-policy`` writes it: sorted, indented JSON."""
    return (json.dumps(dict(policy), indent=2, sort_keys=True) + "\n").encode()


def _measured(
    platform: str, raw: bytes, meta: Mapping[str, object] | None, endpoint: str
) -> tuple[Literal["quote", "metadata"], int | None, dict[str, str], bytes]:
    """Where the registers come from, the quote version, the registers and the bound
    ``report_data``: the TDX quote's, or a simulator's ``metadata`` and first 32 bytes."""
    if platform == SIMULATOR:
        # The simulator binds only the 32-byte hash; its padding is implicit.
        return "metadata", None, _metadata_registers(meta or {}, endpoint), raw[:32] + bytes(32)
    quote = parse_quote(raw)
    return "quote", quote.version, quote.registers(), quote.report_data


_VALIDATOR_BROKEN = ("transport error", "not configured", "not initialized")
"""tdxs ``error`` replies that say the validator could not judge, rather than that it rejected."""


def _validate(
    send: Transport, endpoint: str, document: str, nonce: bytes
) -> tuple[SignatureStatus, str | None, dict[str, object]]:
    """``("valid", None, reply)`` or ``("invalid", why, reply)``; ``AttestationError`` when it
    cannot judge."""
    hint = (
        "Point --validator at a tdxs validator socket (tdxs with a validator configured), "
        "e.g. tcp://127.0.0.1:7001 on the verifying host."
    )
    reply = dict(send({"method": "validate", "data": {"document": document, "nonce": nonce.hex()}}))
    error = reply.get("error")
    if error and any(marker in str(error) for marker in _VALIDATOR_BROKEN):
        raise AttestationError(
            f"The tdxs validator at {endpoint} could not judge the document: {error}",
            hint=hint,
            context={"validator": endpoint},
        )
    judged = _judgement(reply)
    if judged is None:
        raise AttestationError(
            f"The validate reply from {endpoint} has no data.valid flag.",
            hint=hint,
            context={"validator": endpoint},
        )
    return judged[0], judged[1], reply


def _judgement(reply: Mapping[str, object]) -> tuple[SignatureStatus, str | None] | None:
    """What a validate *reply* says: ``valid``, ``invalid`` and why, or ``None`` (no verdict)."""
    error = reply.get("error")
    if error:
        return "invalid", str(error)
    payload = reply.get("data")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("valid"), bool):
        return None
    if payload["valid"]:
        return "valid", None
    return "invalid", "the validator answered valid=false"


def _checks(expected: Mapping[str, object], actual: Mapping[str, str]) -> tuple[RegisterCheck, ...]:
    """Each register of :data:`REGISTERS` against the policy's accepted values."""
    found = match_registers(expected, actual)
    checks = []
    for name in REGISTERS:
        verdict, hit = found[name]
        entries = expected.get(name)
        accepted_entries = tuple(entries) if isinstance(entries, list) else ()
        label = None if hit is None else hit.describe()
        checks.append(
            RegisterCheck(
                name, cast(RegisterVerdict, verdict), actual.get(name), accepted_entries, label
            )
        )
    return tuple(checks)


def _stored_expected(value: object) -> list[object] | None:
    """A record's stored ``expected``: the accepted entries (a lone string is one)."""
    if isinstance(value, str):
        return [value]
    return list(value) if isinstance(value, list) and value else None


def _nonce_bytes(nonce: str | bytes | None) -> bytes:
    if nonce is None:
        return secrets.token_bytes(32)
    if isinstance(nonce, bytes):
        value = nonce
    else:
        text = nonce.strip().lower().removeprefix("0x")
        try:
            value = bytes.fromhex(text)
        except ValueError:
            value = b""
    if not value:
        raise ValidationError(
            f"Nonce {nonce!r} is not a non-empty hex string.",
            hint="Pass --nonce as hex (e.g. 32 random bytes: `openssl rand -hex 32`), "
            "or omit it for a random one.",
        )
    return value


def _call(
    send: Transport, endpoint: str, method: str, data: Mapping[str, object]
) -> Mapping[str, object]:
    reply = send({"method": method, "data": dict(data)})
    error = reply.get("error")
    if error:
        raise AttestationError(
            f"The tdxs issuer at {endpoint} failed the {method} request: {error}",
            hint="Check `journalctl -u tdxs` in the image; the issuer needs /dev/tdx_guest "
            "(or configfs-tsm) inside a TD.",
            context={"endpoint": endpoint, "method": method},
        )
    payload = reply.get("data")
    if not isinstance(payload, Mapping):
        raise MeasurementError(
            f"The tdxs reply to {method} from {endpoint} has no data object.",
            hint="Point --issuer at a tdxs issuer (the image's /var/tdxs.sock).",
            context={"endpoint": endpoint},
        )
    return payload


def _document(encoded: str, endpoint: str) -> Mapping[str, object]:
    hint = "Point --issuer at a tdxs issuer (the image's /var/tdxs.sock)."
    try:
        document = json.loads(bytes.fromhex(encoded))
    except (ValueError, TypeError) as exc:
        raise MeasurementError(
            f"The issue reply from {endpoint} does not hold a hex-encoded attestation document.",
            hint=hint,
            context={"endpoint": endpoint},
        ) from exc
    if not isinstance(document, dict):
        raise MeasurementError(
            f"The attestation document from {endpoint} is not a JSON object.", hint=hint
        )
    return document


def _b64(value: object, name: str) -> bytes:
    if value is None:
        return b""
    try:
        return base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MeasurementError(
            f"The attestation document's {name} is not base64.",
            hint="Point --issuer at a tdxs issuer (the image's /var/tdxs.sock).",
        ) from exc


def _metadata_registers(meta: Mapping[str, object], endpoint: str) -> dict[str, str]:
    measurements = meta.get("metadata")
    if not isinstance(measurements, Mapping):
        raise MeasurementError(
            f"The metadata reply from {endpoint} carries no measurements.",
            hint="Point --issuer at a tdxs issuer (the image's /var/tdxs.sock).",
        )
    return {
        name: str(measurements[name.lower()]).lower().removeprefix("0x")
        for name in REGISTERS
        if measurements.get(name.lower())
    }


# ── records ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AttestationRecord:
    """A ``tundravm.attestation/2`` record, re-derived offline by :func:`read_attestation`.

    ``attestation`` is rebuilt from the record's ``document`` (the quote parsed again),
    its nonce and user data, the registers it was checked against (the given policy's
    when that policy is the one the record names) and the recorded validator reply.
    ``inconsistencies`` lists each stored field that disagrees with what those give;
    ``policy_matches`` says whether a given policy's sha256 is the record's (``None``:
    no policy given). ``sha256`` is the record's own.
    """

    record: Mapping[str, Any] = field(repr=False)
    attestation: Attestation
    inconsistencies: tuple[str, ...] = ()
    policy_matches: bool | None = None
    sha256: str = ""

    @property
    def consistent(self) -> bool:
        """No stored field disagrees with what the document re-derives."""
        return not self.inconsistencies

    @property
    def verdict(self) -> Verdict:
        """The verdict re-derived from the document (not the one the record states)."""
        return self.attestation.verdict

    @property
    def observed_at(self) -> str:
        return self.attestation.observed_at

    @property
    def policy_sha256(self) -> str | None:
        return self.attestation.policy_sha256

    @property
    def artifact_sha256(self) -> str | None:
        """The sha256 of the artifact the record's policy measured: what it attests."""
        sha = (self.attestation.policy_artifact or {}).get("sha256")
        return sha if isinstance(sha, str) and sha else None


_RECORD_HINT = "Write the record with `tundravm attest --record FILE` (Attestation.save)."


def read_attestation(
    source: str | Path | bytes | Mapping[str, Any],
    *,
    policy: str | Path | bytes | Mapping[str, object] | None = None,
) -> AttestationRecord:
    """Read the ``tundravm.attestation/2`` record *source* (a path, its bytes or its dict)
    and re-derive it offline.

    The document is decoded and its quote parsed again (:func:`parse_quote`; a
    simulator's registers come from the recorded ``metadata`` reply), the nonce binding
    is recomputed from the recorded nonce and user data, every register is compared
    again with the expected value and the signature status is read from the recorded
    validator reply; the verdict follows from those. With *policy* (a policy file, its
    bytes or its dict) whose sha256 is the one the record names, its registers and
    ``artifact`` replace the stored ones, so a stored expectation edited to match is
    caught. Each stored field that disagrees is an inconsistency
    (:attr:`AttestationRecord.inconsistencies`).

    This proves what the quote measured and that it binds the nonce. It cannot prove
    the quote genuine: the signature, certificate chain and collateral are only as
    trusted as the recorded validator reply, which the record's producer asserts.
    ``ValidationError`` when *source* is not a readable record or its document is not
    an attestation document.
    """
    where, data, raw_bytes = _load_record(source)
    if data.get("schema") != ATTESTATION_SCHEMA:
        legacy = data.get("schema") == "tundravm.attestation/1"
        raise ValidationError(
            f"{where} has schema {data.get('schema')!r}, not {ATTESTATION_SCHEMA}"
            + (" (its trusted verdict did not require the platform approved)." if legacy else "."),
            hint=("Attest again with this tundravm. " if legacy else "") + _RECORD_HINT,
        )
    encoded = str(data.get("document") or "")
    meta = data.get("metadata")
    try:
        document = _document(encoded, where)
        raw = _b64(document.get("raw_quote"), "raw_quote")
        platform = str(document.get("platform") or "")
        meta_map = meta if isinstance(meta, Mapping) else None
        source_kind, version, actual, bound = _measured(platform, raw, meta_map, where)
    except MeasurementError as exc:
        raise ValidationError(
            f"{where}: its document is not an attestation document: "
            f"{exc.args[0] if exc.args else exc}",
            hint=_RECORD_HINT,
        ) from exc
    nonce = _record_hex(data, "nonce", where)
    user_data = _record_hex(data, "user_data", where)
    stored_policy = data.get("policy") if isinstance(data.get("policy"), Mapping) else {}
    assert isinstance(stored_policy, Mapping)
    stored_registers = data.get("registers") if isinstance(data.get("registers"), Mapping) else {}
    assert isinstance(stored_registers, Mapping)
    expected: dict[str, object] = {
        name: _stored_expected((stored_registers.get(name) or {}).get("expected"))
        for name in REGISTERS
        if isinstance(stored_registers.get(name), Mapping)
    }
    artifact = stored_policy.get("artifact")
    scheme = stored_policy.get("scheme")
    matches: bool | None = None
    if policy is not None:
        given, given_bytes = _given_policy(policy)
        matches = hashlib.sha256(given_bytes).hexdigest() == stored_policy.get("sha256")
        if matches:
            expected = dict(approved_registers(given))
            artifact = given.get("artifact")
            scheme = given.get("scheme")
    status, why, endpoint, reply, problems = _recorded_judgement(data.get("validator"))
    checks = _checks(expected, actual)
    derived = Attestation(
        issuer=str(data.get("issuer") or ""),
        platform=platform or "unknown",
        source=source_kind,
        quote_version=version,
        nonce=nonce,
        report_data=bound.hex(),
        expected_report_data=report_data_for(bytes.fromhex(user_data), bytes.fromhex(nonce)).hex(),
        registers=checks,
        validator=endpoint,
        signature_status=status,
        validator_error=why,
        policy=None if stored_policy.get("path") is None else str(stored_policy.get("path")),
        document=encoded,
        user_data=user_data,
        metadata=dict(meta) if isinstance(meta, Mapping) else None,
        validator_reply=reply,
        policy_sha256=None
        if stored_policy.get("sha256") is None
        else str(stored_policy.get("sha256")),
        policy_artifact=dict(artifact) if isinstance(artifact, Mapping) else None,
        policy_scheme=None if scheme is None else str(scheme),
        observed_at=str(data.get("observed_at") or ""),
    )
    problems.extend(_disagreements(data, derived.record()))
    if parse_time(derived.observed_at) is None:
        problems.append(f"observed_at: {derived.observed_at!r} is not an ISO 8601 UTC time")
    return AttestationRecord(
        record=data,
        attestation=derived,
        inconsistencies=tuple(problems),
        policy_matches=matches,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def parse_time(text: str) -> datetime | None:
    """*text* (``observed_at``, ``created``: ``YYYY-MM-DDTHH:MM:SSZ``) as a UTC time, or
    ``None`` when it is not one."""
    try:
        return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _load_record(
    source: str | Path | bytes | Mapping[str, Any],
) -> tuple[str, dict[str, Any], bytes]:
    """Where the record came from, its parsed JSON and its bytes."""
    where = "the attestation record"
    if isinstance(source, Mapping):
        return where, dict(source), _canonical(source)
    if isinstance(source, bytes):
        raw = source
    else:
        where = str(source)
        try:
            raw = Path(source).read_bytes()
        except OSError as exc:
            raise ValidationError(
                f"Cannot read attestation record {source}: {exc.strerror or exc}.",
                hint=_RECORD_HINT,
            ) from exc
    try:
        data: object = json.loads(raw)
    except ValueError as exc:
        raise ValidationError(f"{where} is not JSON.", hint=_RECORD_HINT) from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{where} is not a JSON object.", hint=_RECORD_HINT)
    return where, data, raw


def _record_hex(data: Mapping[str, Any], key: str, where: str) -> str:
    text = str(data.get(key) if data.get(key) is not None else "").lower()
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise ValidationError(f"{where}: {key} is not hex.", hint=_RECORD_HINT) from exc
    return text


def _given_policy(
    policy: str | Path | bytes | Mapping[str, object],
) -> tuple[dict[str, object], bytes]:
    """The policy to re-check against, read and checked, and the bytes its sha256 covers."""
    if isinstance(policy, Mapping):
        return read_policy(policy), _canonical(policy)
    raw = policy if isinstance(policy, bytes) else Path(policy).read_bytes()
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValidationError(
            "The policy to re-check the attestation against is not JSON.",
            hint="Pass the file `tundravm measure --export-policy` wrote.",
        ) from exc
    return read_policy(parsed if isinstance(parsed, dict) else {}), raw


def _recorded_judgement(
    validator: object,
) -> tuple[SignatureStatus, str | None, str | None, dict[str, object] | None, list[str]]:
    """The signature status the recorded *validator* entry gives, why it is invalid, the
    endpoint, the reply and what is wrong with the entry."""
    if validator is None:
        return "unchecked", None, None, None, []
    if not isinstance(validator, Mapping):
        return "unchecked", None, None, None, ["validator: not an object"]
    endpoint = validator.get("endpoint")
    reply = validator.get("reply")
    shown = None if endpoint is None else str(endpoint)
    if not isinstance(reply, Mapping):
        return "unchecked", None, shown, None, ["validator.reply: the validator's reply is missing"]
    judged = _judgement(reply)
    if judged is None:
        return "unchecked", None, shown, dict(reply), ["validator.reply: it holds no verdict"]
    return judged[0], judged[1], shown, dict(reply), []


_COMPARED = ("platform", "source", "quote_version", "signature_status", "verdict", "reasons")


def _disagreements(stored: Mapping[str, Any], derived: Mapping[str, Any]) -> list[str]:
    """``field: the record says X, re-derived Y`` for each stored field that disagrees."""
    found: list[str] = []

    def compare(name: str, said: object, got: object) -> None:
        if said != got:
            found.append(f"{name}: the record says {_brief(said)}, re-derived {_brief(got)}")

    said_bound = stored.get("report_data") if isinstance(stored.get("report_data"), Mapping) else {}
    assert isinstance(said_bound, Mapping)
    for key in ("expected", "actual", "verdict"):
        got = derived["report_data"][key]
        compare(f"report_data.{key}", said_bound.get(key), got)
    for key in _COMPARED:
        compare(key, stored.get(key), derived[key])
    said_registers = stored.get("registers") if isinstance(stored.get("registers"), Mapping) else {}
    assert isinstance(said_registers, Mapping)
    got_registers = derived["registers"]
    assert isinstance(got_registers, Mapping)
    for name in REGISTERS:
        said = said_registers.get(name) if isinstance(said_registers.get(name), Mapping) else {}
        assert isinstance(said, Mapping)
        for key in ("actual", "expected", "verdict"):
            compare(f"registers.{name}.{key}", said.get(key), got_registers[name][key])
    said_policy = stored.get("policy") if isinstance(stored.get("policy"), Mapping) else {}
    assert isinstance(said_policy, Mapping)
    got_policy = derived["policy"]
    assert isinstance(got_policy, Mapping)
    compare("policy.artifact", said_policy.get("artifact"), got_policy["artifact"])
    compare("policy.scheme", said_policy.get("scheme"), got_policy["scheme"])
    return found


def _brief(value: object) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 40 else f"{text[:37]}..."


# ── transports ───────────────────────────────────────────────────────────


def connect(endpoint: str, *, timeout: float = TIMEOUT_S) -> Transport:
    """The transport for *endpoint*: tdxs's JSON lines over a unix socket or tcp."""
    if endpoint.startswith(("http://", "https://")):
        raise ValidationError(
            f"Endpoint {endpoint!r} is http, which tdxs does not serve.",
            hint="tdxs serves a unix socket or tcp; forward it with "
            "ssh -L ./tdxs.sock:/var/tdxs.sock and pass --issuer unix:./tdxs.sock.",
            context={"endpoint": endpoint},
        )
    if endpoint.startswith("tcp://"):
        host, sep, port = endpoint.removeprefix("tcp://").rstrip("/").rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValidationError(
                f"Endpoint {endpoint!r} is not tcp://HOST:PORT.",
                hint="For example tcp://127.0.0.1:7000.",
            )
        address = (host.strip("[]"), int(port))
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        return _stream(endpoint, family, address, timeout=timeout)
    path = endpoint.removeprefix("unix:")
    if path.startswith("//"):
        path = path[2:]
    if path == endpoint and "://" in endpoint:
        raise ValidationError(
            f"Unsupported endpoint {endpoint!r}.",
            hint="Use unix:PATH (or a path) or tcp://HOST:PORT.",
        )
    return _stream(endpoint, socket.AF_UNIX, path, timeout=timeout)


def _unreachable(endpoint: str, exc: OSError) -> AttestationError:
    reason = exc.strerror or exc
    return AttestationError(
        f"Cannot reach the tdxs endpoint at {endpoint}: {reason}.",
        hint="Check the VM is running and the socket is forwarded to this host, e.g. "
        "`ssh -p 2222 -N -L ./tdxs.sock:/var/tdxs.sock root@localhost` "
        "and --issuer unix:./tdxs.sock (a validator socket likewise for --validator).",
        context={"endpoint": endpoint},
    )


def _decode(endpoint: str, body: bytes) -> Mapping[str, object]:
    try:
        reply = json.loads(body)
    except ValueError as exc:
        raise MeasurementError(
            f"The reply from {endpoint} is not JSON.",
            hint="Point --issuer at a tdxs issuer (the image's /var/tdxs.sock) and "
            "--validator at a tdxs validator.",
            context={"endpoint": endpoint},
        ) from exc
    if not isinstance(reply, dict):
        raise MeasurementError(
            f"The reply from {endpoint} is not a JSON object.",
            hint="Point --issuer at a tdxs issuer (the image's /var/tdxs.sock) and "
            "--validator at a tdxs validator.",
        )
    return reply


def _stream(
    endpoint: str, family: int, address: str | tuple[str, int], *, timeout: float = TIMEOUT_S
) -> Transport:
    """tdxs's own protocol: one JSON request line, one JSON reply line, per connection."""

    def send(request: Mapping[str, object]) -> Mapping[str, object]:
        line = json.dumps(request).encode() + b"\n"
        buffer = b""
        try:
            with socket.socket(family, socket.SOCK_STREAM) as conn:
                conn.settimeout(timeout)
                conn.connect(address)
                conn.sendall(line)
                while b"\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
        except OSError as exc:
            raise _unreachable(endpoint, exc) from exc
        return _decode(endpoint, buffer.split(b"\n", 1)[0])

    return send


# ── rendering ────────────────────────────────────────────────────────────


def _signature_line(result: Attestation) -> str:
    if result.validator is None:
        return "signature unchecked (no --validator)"
    if result.signature_status == "valid":
        return f"signature valid ({result.validator})"
    return f"signature invalid ({result.validator}): {result.validator_error}"


def render_attestation(result: Attestation, fmt: str) -> str:
    """*result* as ``text`` (one line per register), ``json`` or ``markdown`` (a table)."""
    if fmt == "json":
        return json.dumps(result.to_dict(), indent=2, sort_keys=True)
    nonce = "match" if result.nonce_matches else "mismatch"
    where = f"quote v{result.quote_version}" if result.source == "quote" else "issuer metadata"
    reasons = ", ".join(result.reasons)
    if fmt == "markdown":
        rows = [
            (
                md_cell(r.name),
                md_cell(r.verdict),
                md_cell(r.actual, code=True),
                md_cell(r.matched or ", ".join(v[:12] for v in r.values), code=True),
            )
            for r in result.registers
        ]
        because = f" Reasons: {reasons}." if reasons else ""
        return "\n\n".join(
            (
                f"# tundravm attest: `{result.issuer}`",
                f"**Verdict:** `{result.verdict}` ({result.platform}, {where}; "
                f"nonce {nonce}; {_signature_line(result)}).{because}",
                md_table(("Register", "Verdict", "Quote", "Policy"), rows),
                f"Note: {CHECKS_NOTE}.",
            )
        )
    lines = [
        f"attestation {result.issuer} ({result.platform}, {where})",
        f"policy: {result.policy or '(dict)'}",
        _signature_line(result),
        f"nonce  {nonce:<9}  {result.nonce}",
    ]
    if not result.nonce_matches:
        lines.append(f"{'':<6} {'quote':<9}  {result.report_data}")
        lines.append(f"{'':<6} {'expected':<9}  {result.expected_report_data}")
    for check in result.registers:
        via = f"  ({check.matched})" if check.matched else ""
        lines.append(f"{check.name:<6} {check.verdict:<9}  {check.actual or '-'}{via}")
        if check.verdict == "mismatch":
            accepted_values = check.values
            shown = (
                accepted_values[0]
                if len(accepted_values) == 1
                else (f"one of {len(accepted_values)} accepted values")
            )
            lines.append(f"{'':<6} {'policy':<9}  {shown}")
    if result.validator is None:
        lines.append(f"note: {CHECKS_NOTE}")
    if result.verdict == "partial":
        lines.append(f"note: {PARTIAL_NOTE}")
    if reasons:
        lines.append(f"reasons: {reasons}")
    lines.append(f"verdict: {result.verdict}")
    return "\n".join(lines)


__all__ = [
    "ATTESTATION_SCHEMA",
    "CHECKS_NOTE",
    "REGISTERS",
    "TRUST_NOTE",
    "VERDICTS",
    "Attestation",
    "AttestationRecord",
    "Quote",
    "RegisterCheck",
    "SignatureStatus",
    "Transport",
    "Verdict",
    "attest",
    "connect",
    "parse_quote",
    "parse_time",
    "read_attestation",
    "render_attestation",
    "report_data_for",
]
