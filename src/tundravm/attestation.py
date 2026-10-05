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
match; it is never ``trusted``.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import AttestationError, MeasurementError, ValidationError
from .formats import md_cell, md_table
from .measure.policy import is_placeholder, read_policy

Transport = Callable[[Mapping[str, object]], Mapping[str, object]]
"""Sends one tdxs request envelope (``{"method", "data"}``) and returns the reply envelope."""
RegisterVerdict = Literal["match", "mismatch", "unchecked"]
Verdict = Literal[
    "trusted", "untrusted", "measurements-match", "measurements-mismatch", "simulated"
]
"""``trusted``/``untrusted`` need a validator; without one the verdict is ``measurements-*``."""
SignatureStatus = Literal["valid", "invalid", "unchecked"]

REGISTERS = ("MRTD", "RTMR0", "RTMR1", "RTMR2", "RTMR3")
"""The registers :func:`attest` reports, in order."""
VERDICTS: tuple[Verdict, ...] = (
    "trusted",
    "untrusted",
    "measurements-match",
    "measurements-mismatch",
    "simulated",
)
"""Every verdict :func:`attest` returns; ``trusted`` and ``measurements-match`` pass."""
CHECKS_NOTE = (
    "the quote's signature, certificate chain and collateral are checked only by a "
    "tdxs validator (--validator); without one only measurements and the nonce are checked"
)
TIMEOUT_S = 30.0
SIMULATOR = "simulator"

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
    """One register: the quote's value, the policy's (``None``: not in the policy), the verdict."""

    name: str
    verdict: RegisterVerdict
    actual: str | None
    expected: str | None

    def to_dict(self) -> dict[str, object]:
        return {"actual": self.actual, "expected": self.expected, "verdict": self.verdict}


@dataclass(frozen=True, slots=True)
class Attestation:
    """What :func:`attest` found: the nonce and register checks, the validator's say, the verdict.

    ``source`` is ``quote`` when the registers were read from the TDX quote, or
    ``metadata`` for the simulator issuer, whose registers come from its
    ``metadata`` reply. ``signature_status`` is ``valid`` or ``invalid`` when a
    validator judged the document and ``unchecked`` without one.
    :attr:`verdict` is ``trusted`` only when the validator accepted the document,
    the full 64-byte ``report_data`` binds the nonce and every register the
    policy holds matched; a simulator issuer's quote is never trusted.
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

        ``validator-rejected``, ``nonce-mismatch``, ``register-mismatch``, ``simulated``.
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
        return tuple(found)

    @property
    def verdict(self) -> Verdict:
        measured = self.nonce_matches and all(r.verdict != "mismatch" for r in self.registers)
        if self.signature_status == "invalid":
            return "untrusted"
        if not measured:
            return "untrusted" if self.signature_status == "valid" else "measurements-mismatch"
        if self.simulated:
            return "simulated"
        return "trusted" if self.signature_status == "valid" else "measurements-match"

    @property
    def trusted(self) -> bool:
        return self.verdict == "trusted"

    @property
    def passed(self) -> bool:
        """``trusted`` or ``measurements-match``; ``simulated`` too with ``allow_simulated``."""
        verdict = self.verdict
        return verdict in ("trusted", "measurements-match") or (
            verdict == "simulated" and self.allow_simulated
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


def attest(
    issuer: str,
    policy: str | Path | Mapping[str, object],
    *,
    validator: str | None = None,
    nonce: str | bytes | None = None,
    allow_simulated: bool = False,
    transport: Transport | None = None,
    validator_transport: Transport | None = None,
) -> Attestation:
    """Request a quote from the tdxs issuer at *issuer*, check it against *policy*.

    *issuer* and *validator* are ``unix:PATH`` (or a path) or
    ``tcp://HOST:PORT``; both speak tdxs's JSON lines. *policy* is a file
    ``measure --export-policy`` wrote, or its dict; a placeholder policy is
    refused. *nonce* (hex or bytes; default 32 random bytes) must come back in
    the quote's full 64-byte ``report_data``. With *validator* the document and
    nonce go to that tdxs validator, whose answer decides authenticity
    (``trusted``/``untrusted``); without it the verdict is
    ``measurements-match`` or ``measurements-mismatch``. A simulator issuer's
    verdict is ``simulated`` at best; *allow_simulated* lets it pass.
    *transport* and *validator_transport* replace the network (tests). Raises
    ``AttestationError`` when the issuer or validator cannot be reached or
    fails, ``MeasurementError`` for a reply that is not a TDX attestation.
    """
    data = read_policy(policy)
    if is_placeholder(data):
        raise MeasurementError(
            "Refusing to attest against a placeholder policy.",
            hint="Export the policy from a real measurement: "
            "`tundravm measure MANIFEST --export-policy FILE` with measured-boot or dstack-mr.",
            context={"policy": "dict" if isinstance(policy, Mapping) else str(policy)},
        )
    expected = data["registers"]
    assert isinstance(expected, dict)
    challenge = _nonce_bytes(nonce)
    send = transport if transport is not None else connect(issuer)
    reply = _call(send, issuer, "issue", {"userData": "", "nonce": challenge.hex()})
    encoded = str(reply.get("document") or "")
    document = _document(encoded, issuer)
    platform = str(document.get("platform") or "")
    raw = _b64(document.get("raw_quote"), "raw_quote")
    wanted = report_data_for(b"", challenge)
    if platform == SIMULATOR:
        meta = _call(send, issuer, "metadata", {})
        actual = _metadata_registers(meta, issuer)
        # The simulator binds only the 32-byte hash; its padding is implicit.
        bound = raw[:32] + bytes(32)
        source: Literal["quote", "metadata"] = "metadata"
        version = None
    else:
        quote = parse_quote(raw)
        actual = quote.registers()
        bound = quote.report_data
        source, version = "quote", quote.version
    status: SignatureStatus = "unchecked"
    rejection = None
    if validator is not None:
        judge = validator_transport if validator_transport is not None else connect(validator)
        status, rejection = _validate(judge, validator, encoded, challenge)
    checks = tuple(_check(name, actual.get(name), expected.get(name)) for name in REGISTERS)
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
    )


_VALIDATOR_BROKEN = ("transport error", "not configured", "not initialized")
"""tdxs ``error`` replies that say the validator could not judge, rather than that it rejected."""


def _validate(
    send: Transport, endpoint: str, document: str, nonce: bytes
) -> tuple[SignatureStatus, str | None]:
    """``("valid", None)`` or ``("invalid", why)``; ``AttestationError`` when it cannot judge."""
    hint = (
        "Point --validator at a tdxs validator socket (tdxs with a validator configured), "
        "e.g. tcp://127.0.0.1:7001 on the verifying host."
    )
    reply = send({"method": "validate", "data": {"document": document, "nonce": nonce.hex()}})
    error = reply.get("error")
    if error:
        text = str(error)
        if any(marker in text for marker in _VALIDATOR_BROKEN):
            raise AttestationError(
                f"The tdxs validator at {endpoint} could not judge the document: {text}",
                hint=hint,
                context={"validator": endpoint},
            )
        return "invalid", text
    payload = reply.get("data")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("valid"), bool):
        raise AttestationError(
            f"The validate reply from {endpoint} has no data.valid flag.",
            hint=hint,
            context={"validator": endpoint},
        )
    if payload["valid"]:
        return "valid", None
    return "invalid", "the validator answered valid=false"


def _check(name: str, actual: str | None, expected: object) -> RegisterCheck:
    if not isinstance(expected, str):
        return RegisterCheck(name, "unchecked", actual, None)
    verdict: RegisterVerdict = "match" if actual == expected else "mismatch"
    return RegisterCheck(name, verdict, actual, expected)


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
                md_cell(r.expected, code=True),
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
        lines.append(f"{check.name:<6} {check.verdict:<9}  {check.actual or '-'}")
        if check.verdict == "mismatch":
            lines.append(f"{'':<6} {'policy':<9}  {check.expected}")
    if result.validator is None:
        lines.append(f"note: {CHECKS_NOTE}")
    if reasons:
        lines.append(f"reasons: {reasons}")
    lines.append(f"verdict: {result.verdict}")
    return "\n".join(lines)


__all__ = [
    "CHECKS_NOTE",
    "REGISTERS",
    "VERDICTS",
    "Attestation",
    "Quote",
    "RegisterCheck",
    "SignatureStatus",
    "Transport",
    "Verdict",
    "attest",
    "connect",
    "parse_quote",
    "render_attestation",
    "report_data_for",
]
