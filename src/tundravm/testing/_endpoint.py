"""A fake attested endpoint: ``tools/pkg/endpoint``'s protocol in memory, for the operator
side's tests. It speaks through :class:`FakeSession` objects (one per TLS session), signs
simulator quotes the way tdxs's simulator issuer does and builds synthetic TDX quotes,
presents the certificate of each attested TLS listener it binds, and misbehaves on
request: ``relay``, ``replay``, ``wrong-key``, ``wrong-certificate``."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from tundravm.attestation import SIMULATOR_KEY, report_data_for
from tundravm.bootstrap import PURPOSE, binding

Misbehaviour = Literal["relay", "replay", "wrong-key", "wrong-certificate"]
"""``relay``: the session's TLS key changes after the evidence (the delivery would land on
another session); ``replay``: the evidence answers the first nonce it ever saw; ``wrong-key``:
the evidence binds a TLS key that is not the session's; ``wrong-certificate``: an attested
TLS listener presents a certificate whose key the evidence does not bind."""

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeEndpointHostKeyFakeEndpointHostKey00"
INSTANCE_KEY = "02" + "ab" * 32


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    if length < 128:
        head = bytes([length])
    else:
        size = (length.bit_length() + 7) // 8
        head = bytes([0x80 | size]) + length.to_bytes(size, "big")
    return bytes([tag]) + head + content


def listener_key() -> bytes:
    """A stand-in for an attested TLS listener's key: one DER element, as a
    SubjectPublicKeyInfo is, of random content."""
    return _der(0x30, os.urandom(89))


def der_certificate(spki: bytes) -> bytes:
    """A DER X.509 structure around *spki*, enough for
    :func:`tundravm.bootstrap.certificate_key`: a serial, an algorithm, an issuer, a
    validity and a subject before the key, then an algorithm and a signature nobody checks."""
    body = _der(0x02, b"\x01") + _der(0x30, b"") * 4 + spki
    return _der(0x30, _der(0x30, body) + _der(0x30, b"") + _der(0x03, b"\x00"))


def tdx_quote(
    report_data: bytes,
    *,
    registers: Mapping[str, bytes] | None = None,
    debug: bool = False,
) -> bytes:
    """A synthetic DCAP v4 quote: header, TD report body (``MRTD``, ``RTMR0``..``RTMR3``
    from *registers*, zeros otherwise), *report_data*, an empty signature; *debug* sets the
    TD's ``DEBUG`` attribute."""
    values = dict(registers or {})
    body = bytearray(584)
    body[120] = 1 if debug else 0
    body[136:184] = values.get("MRTD", bytes(48))
    for index in range(4):
        start = 328 + 48 * index
        body[start : start + 48] = values.get(f"RTMR{index}", bytes(48))
    body[520:584] = report_data
    header = struct.pack("<HHI", 4, 2, 0x81) + bytes(40)
    return header + bytes(body) + struct.pack("<I", 0)


@dataclass
class FakeEndpoint:
    """The endpoint of one boot: what it accepts, what it received.

    *platform* ``simulator`` signs simulator quotes; ``tdx`` builds TDX quotes with
    *registers* and *debug*. *mode* is the guest gate's recorded mode. *listeners* maps
    each attested TLS port to the DER SubjectPublicKeyInfo of its certificate key, which the
    evidence binds. :attr:`received`, :attr:`unlocked` and :attr:`ssh_keys` hold what was
    written, one per name per boot. With *pins* the image pins its operator (an
    ``OperatorKey()`` volume): every write must name a ``signer`` and carry a ``signature``,
    the first signed one pins :attr:`owner` (a public key line), and a write by another key
    is refused (403); :attr:`signers` holds who signed each write. The fake checks who
    signed, not the signature (``tools/pkg/owner`` verifies it)."""

    platform: Literal["simulator", "tdx"] = "simulator"
    registers: Mapping[str, bytes] = field(default_factory=dict)
    debug: bool = False
    mode: str = "simulated"
    credentials: tuple[str, ...] = ("api-token",)
    volumes: tuple[str, ...] = ("data",)
    ssh_key: bool = True
    listeners: Mapping[int, bytes] = field(default_factory=dict)
    misbehave: Misbehaviour | None = None
    received: dict[str, bytes] = field(default_factory=dict)
    unlocked: dict[str, bytes] = field(default_factory=dict)
    ssh_keys: list[str] = field(default_factory=list)
    sessions: list[FakeSession] = field(default_factory=list)
    first_nonce: bytes | None = None
    pins: bool = False
    owner: str | None = None
    mismatch: str | None = None
    signers: list[str] = field(default_factory=list)

    def connect(self, host: str, port: int) -> FakeSession:
        """A new TLS session (the ``connect=`` the operator commands take)."""
        del host, port
        session = FakeSession(self, os.urandom(91))
        self.sessions.append(session)
        return session

    def presented(self, host: str, port: int) -> bytes:
        """The DER certificate the attested TLS listener on *port* presents (the
        ``presented=`` that ``attest`` takes): around its bound key, or around a key the
        evidence does not bind when misbehaving or when *port* is no listener."""
        del host
        key = None if self.misbehave == "wrong-certificate" else self.listeners.get(port)
        return der_certificate(listener_key() if key is None else key)

    def evidence(self, session: FakeSession, nonce: bytes) -> dict[str, object]:
        if self.first_nonce is None:
            self.first_nonce = nonce
        if self.misbehave == "replay":
            nonce = self.first_nonce
        key = os.urandom(91) if self.misbehave == "wrong-key" else session.key
        bound_ports = sorted(self.listeners)
        user_data = binding(
            nonce,
            key,
            [HOST_KEY],
            bytes.fromhex(INSTANCE_KEY),
            [self.listeners[port] for port in bound_ports],
        )
        bound = report_data_for(user_data, nonce)
        metadata = None
        if self.platform == "simulator":
            tag = hmac.new(SIMULATOR_KEY, bound[:32], hashlib.sha256).digest()
            raw = bound[:32] + tag
            zero = bytes(48).hex()
            names = ("mrtd", "mrowner", "mrseam", "rtmr0", "rtmr1", "rtmr2", "rtmr3")
            metadata = {"issuerType": "simulator", "metadata": dict.fromkeys(names, zero)}
        else:
            raw = tdx_quote(bound, registers=self.registers, debug=self.debug)
        document = {
            "raw_quote": base64.b64encode(raw).decode(),
            "user_data": base64.b64encode(user_data).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "platform": self.platform,
        }
        session.nonces.append(nonce.hex())
        if self.misbehave == "relay":
            session.key = os.urandom(91)
        return {
            "purpose": PURPOSE,
            "nonce": nonce.hex(),
            "user_data": user_data.hex(),
            "document": json.dumps(document).encode().hex(),
            "metadata": metadata,
            "tls_public_key": base64.b64encode(key).decode(),
            "ssh_host_keys": [HOST_KEY],
            "instance_public_key": INSTANCE_KEY,
            "attested_tls": [
                {"port": port, "public_key": base64.b64encode(self.listeners[port]).decode()}
                for port in bound_ports
            ],
            "mode": self.mode,
            "accepts": {
                "credentials": list(self.credentials),
                "volumes": list(self.volumes),
                "ssh_key": self.ssh_key,
                **(
                    {"owner": {"fingerprint": self._fingerprint(), "mismatch": self.mismatch}}
                    if self.pins
                    else {}
                ),
            },
        }

    def _fingerprint(self) -> str:
        if self.owner is None:
            return ""
        blob = base64.b64decode(self.owner.split()[1])
        return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")

    def write(self, session: FakeSession, path: str, body: Mapping[str, object]) -> int:
        nonce = str(body.get("nonce") or "")
        if nonce not in session.nonces:
            return 403
        session.nonces.remove(nonce)
        if self.pins:
            signer = str(body.get("signer") or "")
            if not signer or not body.get("signature"):
                return 403
            if self.owner is not None and signer != self.owner:
                return 403
            self.owner = self.owner or signer
            self.signers.append(signer)
            if path == "/owner":
                self.owner = str(body.get("key"))
                return 200
        if path == "/credentials":
            values = body.get("credentials")
            assert isinstance(values, Mapping)
            if any(name not in self.credentials for name in values):
                return 404
            if any(name in self.received for name in values):
                return 409
            for name, value in values.items():
                self.received[name] = base64.b64decode(str(value))
            return 200
        if path == "/unlock":
            volume = str(body.get("volume"))
            if volume not in self.volumes:
                return 404
            if volume in self.unlocked:
                return 409
            self.unlocked[volume] = base64.b64decode(str(body.get("passphrase")))
            return 200
        if path == "/ssh-key" and self.ssh_key:
            self.ssh_keys.append(str(body.get("key")))
            return 200
        return 404


@dataclass
class FakeSession:
    """One TLS session to a :class:`FakeEndpoint`: its key and the nonces served on it."""

    endpoint: FakeEndpoint
    key: bytes
    nonces: list[str] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)

    def peer_key(self) -> bytes:
        return self.key

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        self.requests.append((method, path))
        url = urlsplit(path)
        if method == "GET" and url.path == "/evidence":
            nonce = bytes.fromhex(parse_qs(url.query)["nonce"][0])
            return 200, json.dumps(self.endpoint.evidence(self, nonce)).encode()
        status = self.endpoint.write(self, url.path, json.loads(body or b"{}"))
        reply = {} if status == 200 else {"error": f"refused with {status}"}
        return status, json.dumps(reply).encode()
