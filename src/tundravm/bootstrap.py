"""``attest`` and ``secrets``: the operator's side of the attested endpoint (plan 6.3).

An image with a volume, a secret or an attested TLS ingress runs ``attested-endpoint``
(``tools/``) on the management port :data:`~tundravm.instances.MANAGEMENT_PORT`. It makes a
TLS key pair at start and serves ``GET /evidence?nonce=``: a quote whose ``report_data`` is
``SHA-256(binding || nonce)``, where ``binding`` (the quote's user data) is
``SHA-512(purpose, nonce, the TLS key, the SSH host keys, the instance public key, each
container's SSH host keys, each attested TLS port's certificate key)``, each part
length-prefixed (:func:`binding`).

- :func:`attest` opens one TLS session, sends a fresh nonce, and refuses evidence that does
  not answer that nonce (a replay), that binds another TLS key than this session's (a
  relayed quote, or the wrong key), or whose bound material does not hash to the quote's
  user data. Then :func:`tundravm.attestation.attest` judges the quote against the policy
  (a simulator's structurally, ``simulated`` at best). On success the bound SSH host keys,
  the host's and each container's on its own port, become the instance's ``known_hosts``
  and, when the image takes one, the operator's SSH public key goes over the same session
  (``POST /ssh-key``).
- :func:`deliver` and :func:`unlock` verify again on a session of their own, apply
  :func:`release_predicate`, then write on that session only: ``POST /credentials``,
  ``POST /unlock``. Each write carries the nonce of the evidence the session fetched, which
  the endpoint takes once, and the client refuses to send it if the session's TLS key is no
  longer the bound one. Both refuse a production build in a simulated instance before
  anything is sent (:func:`fail_closed`).
- An image with an ``OperatorKey()`` volume pins its operator: the volume's LUKS2 header
  holds the owner's SSH key and every write carries the owner's SSH signature over its
  evidence's user data, which binds the nonce and this boot's TLS key (:func:`signed`,
  namespace :data:`SIGNATURE_NAMESPACE`), so no other endpoint can replay it; the first
  signed one pins its signer. The key is the one ``attest`` used (``--identity``), recorded
  in the receipt so ``secrets deliver`` and ``secrets unlock`` sign with it; :func:`rotate`
  hands the volumes to another key.

Values travel in the TLS session's request bodies only: never on an argv, in a log, a
tree, a manifest or a receipt (receipts name what was delivered, not what it was).
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import http.client
import json
import os
import re
import secrets as random
import socket
import ssl
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import _ssh_sig
from . import manifest as mf
from .attestation import ENDPOINT_CALLER, Attestation, Transport, mode_history
from .attestation import attest as check_quote
from .attestation import connect as connect_issuer
from .errors import AttestationError, StateError, ValidationError
from .instances import MANAGEMENT_PORT, SSH_PORT, Instance, known_hosts

PURPOSE = "tundravm-endpoint/2"
"""What the endpoint's quotes are for: the first part of every binding."""
CHANNEL = "attested endpoint: a TLS session whose key the quote binds"
"""How ``attest``, ``secrets deliver`` and ``secrets unlock`` reach the instance."""
CREDENTIALS = "/run/tundravm/credentials"
DEFAULT_IDENTITY = Path("~/.ssh/id_ed25519")
SIGNATURE_NAMESPACE = "tundravm-endpoint"
"""The SSH signature namespace of a write to an endpoint that pins its operator."""
TIMEOUT_S = 60.0
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def binding(
    nonce: bytes,
    tls_key: bytes,
    host_keys: Sequence[str],
    instance_key: bytes,
    ingress_keys: Sequence[bytes] = (),
    containers: Sequence[ContainerKeys] = (),
) -> bytes:
    """The 64-byte user data a quote of the endpoint carries (``tools/pkg/endpoint``):
    SHA-512 over :data:`PURPOSE`, *nonce*, *tls_key* (DER SubjectPublicKeyInfo), the sorted
    ``type base64`` *host_keys* joined by newlines, *instance_key*, the sorted
    ``name port type base64`` lines of the *containers*' host keys joined by newlines, and
    each of *ingress_keys* (the DER SubjectPublicKeyInfo of an attested TLS port's
    certificate key, in port order), each part prefixed with its length as a big-endian
    uint32."""
    digest = hashlib.sha512()
    lines = sorted(f"{c.name} {c.port} {key}" for c in containers for key in c.host_keys)
    parts = (
        PURPOSE.encode(),
        nonce,
        tls_key,
        "\n".join(host_keys).encode(),
        instance_key,
        "\n".join(lines).encode(),
    )
    for part in (*parts, *ingress_keys):
        digest.update(struct.pack(">I", len(part)) + part)
    return digest.digest()


@dataclass(frozen=True, slots=True)
class ContainerKeys:
    """One container's sshd host keys, as the evidence binds them: by name, on *port*."""

    name: str
    port: int
    host_keys: tuple[str, ...]


class Channel(Protocol):
    """One TLS session to an endpoint."""

    def peer_key(self) -> bytes:
        """The DER SubjectPublicKeyInfo of the session's TLS key (it connects if needed)."""
        ...

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        """One request on the session: the status and the body."""
        ...


Connector = Callable[[str, int], Channel]
"""Opens a session to ``host:port`` (tests replace it with a fake endpoint's)."""

Presented = Callable[[str, int], bytes]
"""Returns the DER certificate the TLS listener at ``host:port`` presents (tests replace it)."""


def _tlv(data: bytes, offset: int) -> tuple[int, int, int]:
    """``(tag, content start, end)`` of the DER element at *offset*."""
    tag, length = data[offset], data[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    return tag, start, start + length


def certificate_key(der: bytes) -> bytes:
    """The DER SubjectPublicKeyInfo of an X.509 certificate *der*."""
    try:
        _, body, _ = _tlv(der, 0)
        _, position, _ = _tlv(der, body)
        if _tlv(der, position)[0] == 0xA0:  # explicit version
            position = _tlv(der, position)[2]
        for _ in range(5):  # serial, signature, issuer, validity, subject
            position = _tlv(der, position)[2]
        return der[position : _tlv(der, position)[2]]
    except IndexError as exc:
        raise AttestationError(
            "The endpoint's TLS certificate is not DER.", hint="Is :8745 the attested endpoint?"
        ) from exc


class TlsChannel:
    """A kept-alive TLS session; the certificate is not checked against a CA, because the
    quote is what authenticates its key."""

    def __init__(self, host: str, port: int, *, timeout: float = TIMEOUT_S) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.where = f"{host}:{port}"
        self.connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)

    def _socket(self) -> ssl.SSLSocket:
        try:
            if self.connection.sock is None:
                self.connection.connect()
        except OSError as exc:
            raise AttestationError(
                f"Cannot reach the attested endpoint at {self.where}: {exc}.",
                hint="Check the instance runs (`tundravm run status` / `deploy status`): its "
                "control plane listens on the management port.",
            ) from exc
        sock = self.connection.sock
        assert isinstance(sock, ssl.SSLSocket)
        return sock

    def peer_key(self) -> bytes:
        der = self._socket().getpeercert(binary_form=True)
        if der is None:
            raise AttestationError(f"{self.where} presented no certificate.", hint="")
        return certificate_key(der)

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        self._socket()
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            self.connection.request(method, path, body=body, headers=headers)
            reply = self.connection.getresponse()
            return reply.status, reply.read()
        except (OSError, http.client.HTTPException) as exc:
            raise AttestationError(
                f"The attested endpoint at {self.where} dropped the session: {exc}.",
                hint="Run the command again: a new session fetches fresh evidence.",
            ) from exc


def _tls(host: str, port: int) -> Channel:
    return TlsChannel(host, port)


def _presented(host: str, port: int) -> bytes:
    """The DER certificate the attested TLS listener at *host*:*port* presents; the handshake
    checks nothing, since the quote is what vouches for the key."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as raw:
            with context.wrap_socket(raw) as sock:
                der = sock.getpeercert(binary_form=True)
    except OSError as exc:
        raise AttestationError(
            f"Cannot reach the attested TLS listener at {host}:{port}: {exc}.",
            hint="The relay starts with the image's services: check `tundravm run status`, and "
            "pass --tls with an AttestedTLS port of the image (`tundravm show BUILD` lists them).",
        ) from exc
    if der is None:
        raise AttestationError(
            f"{host}:{port} presented no certificate.",
            hint="Pass --tls with an AttestedTLS port of the image (`tundravm show BUILD`).",
        )
    return der


@dataclass(frozen=True, slots=True)
class Certificate:
    """The certificate an attested TLS listener presented, its key found in the binding."""

    port: int
    """The guest port the listener serves."""
    fingerprint: str
    """``sha256:`` and the hex SHA-256 of the certificate's DER, as ``openssl x509
    -fingerprint -sha256`` prints it without the colons."""
    key: bytes
    """The DER SubjectPublicKeyInfo of its key: the part the quote's user data holds."""


def key_name(spki: bytes) -> str:
    """How a refusal names a key: ``sha256:`` and the hex SHA-256 of its DER
    SubjectPublicKeyInfo."""
    return "sha256:" + hashlib.sha256(spki).hexdigest()


def check_certificate(evidence: Evidence, port: int, der: bytes) -> Certificate:
    """The certificate *der* the attested TLS listener on guest *port* presented, refused
    unless its key is the one *evidence* binds for that port; the refusal says which key the
    quote bound. It is checked against the evidence the policy was judged on, never
    another session's, so a relay cannot pair a good quote with a certificate of its own."""
    key = certificate_key(der)
    fingerprint = "sha256:" + hashlib.sha256(der).hexdigest()
    bound = dict(evidence.attested_tls)
    if port not in bound:
        ports = ", ".join(f":{p}" for p in bound) or "no port"
        raise AttestationError(
            f"Refused: the quote binds no certificate key for :{port}; it binds one for {ports}.",
            hint="Pass --tls with an AttestedTLS port of this image: `tundravm show BUILD` "
            "lists its ingress.",
        )
    if bound[port] != key:
        raise AttestationError(
            f"Refused: the certificate on :{port} ({fingerprint}) carries key {key_name(key)}, "
            f"but the quote binds key {key_name(bound[port])} for that port.",
            hint="Something between this host and the instance terminates TLS on that port: "
            "check the forward (`tundravm run status`) and attest again.",
        )
    return Certificate(port, fingerprint, key)


@dataclass(frozen=True, slots=True)
class Evidence:
    """``GET /evidence``, its binding checked against the session it came on."""

    nonce: bytes
    tls_key: bytes
    user_data: bytes
    document: str
    metadata: Mapping[str, object] | None
    host_keys: tuple[str, ...]
    instance_key: str
    mode: str | None
    accepts: Mapping[str, Any] = field(default_factory=dict)
    attested_tls: tuple[tuple[int, bytes], ...] = ()
    """Each attested TLS port and the DER SubjectPublicKeyInfo of the certificate key the
    quote binds for it, in port order."""
    firewall: Mapping[str, Any] | None = None
    """The firewall's ``mode``, ``modes`` and switch ``events`` since boot, in an image with
    modes; the quote's RTMR3 is their replay."""
    containers: tuple[ContainerKeys, ...] = ()
    """Each container's sshd host keys the quote binds, labelled by its name and port."""

    @property
    def mode_events(self) -> tuple[str, ...]:
        return tuple(str(e) for e in (self.firewall or {}).get("events") or ())

    @property
    def firewall_modes(self) -> tuple[str, ...]:
        return tuple(str(m) for m in (self.firewall or {}).get("modes") or ())

    @property
    def credentials(self) -> tuple[str, ...]:
        return tuple(str(n) for n in self.accepts.get("credentials") or ())

    @property
    def volumes(self) -> tuple[str, ...]:
        return tuple(str(n) for n in self.accepts.get("volumes") or ())

    @property
    def takes_ssh_key(self) -> bool:
        return self.accepts.get("ssh_key") is True

    @property
    def pins_owner(self) -> bool:
        """Whether the image pins its operator (an ``OperatorKey()`` volume)."""
        return isinstance(self.accepts.get("owner"), Mapping)

    @property
    def owner(self) -> str | None:
        """The owner's fingerprint, ``None`` while no one is pinned."""
        found = self.accepts.get("owner")
        if not isinstance(found, Mapping):
            return None
        return str(found.get("fingerprint") or "") or None

    @property
    def owner_mismatch(self) -> str | None:
        """``volume NAME: owner token does not match the volume's owner`` when ``disk-setup``
        found the header naming another key than the volume's encrypted copy."""
        found = self.accepts.get("owner")
        if not isinstance(found, Mapping):
            return None
        return str(found.get("mismatch") or "") or None

    def transport(self) -> Transport:
        """The quote, as tdxs would answer the issue and metadata requests for it."""

        def send(request: Mapping[str, object]) -> Mapping[str, object]:
            data = request.get("data")
            assert isinstance(data, Mapping)
            if request["method"] == "metadata":
                return {"data": dict(self.metadata or {}), "error": None}
            assert data.get("userData") == self.user_data.hex()
            assert data.get("nonce") == self.nonce.hex()
            return {"data": {"document": self.document}, "error": None}

        return send


def _json(status: int, body: bytes, where: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body or b"{}")
    except ValueError as exc:
        raise AttestationError(
            f"The attested endpoint at {where} answered {status} without JSON.", hint=""
        ) from exc
    if not isinstance(parsed, dict):
        raise AttestationError(f"The attested endpoint at {where} answered a non-object.", hint="")
    return parsed


def fetch_evidence(channel: Channel, nonce: bytes, where: str) -> Evidence:
    """Ask for evidence binding *nonce* on *channel*, and refuse what is not bound to this
    session: another nonce (a replay), another TLS key (relayed, or the wrong key), or bound
    material that does not hash to the quote's user data."""
    key = channel.peer_key()
    status, body = channel.request("GET", f"/evidence?nonce={nonce.hex()}")
    said = _json(status, body, where)
    if status != 200:
        raise AttestationError(
            f"The attested endpoint at {where} gave no evidence ({status}): "
            f"{said.get('error') or 'no reason'}.",
            hint="Read `journalctl -u tundravm-endpoint -u tdxs` in the guest: the endpoint "
            "needs the evidence service, which a production build without TDX never starts.",
        )
    _same_session(channel, key)
    try:
        answered = bytes.fromhex(str(said.get("nonce") or ""))
        bound_key = base64.b64decode(str(said.get("tls_public_key") or ""), validate=True)
        user_data = bytes.fromhex(str(said.get("user_data") or ""))
        instance_key = str(said.get("instance_public_key") or "")
        host_keys = tuple(str(k) for k in said.get("ssh_host_keys") or ())
        instance_bytes = bytes.fromhex(instance_key)
        listeners = tuple(
            sorted(
                (int(item["port"]), base64.b64decode(str(item["public_key"]), validate=True))
                for item in said.get("attested_tls") or ()
            )
        )
        containers = tuple(
            ContainerKeys(
                str(item["name"]),
                int(item["port"]),
                tuple(str(k) for k in item.get("ssh_host_keys") or ()),
            )
            for item in said.get("containers") or ()
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AttestationError(f"The evidence from {where} is malformed: {exc}.", hint="") from exc
    hint = "Something between this host and the instance answered for it; attest again."
    if said.get("purpose") != PURPOSE:
        raise AttestationError(
            f"Refused: the evidence is for {said.get('purpose')!r}, not {PURPOSE}.", hint=hint
        )
    if answered != nonce:
        raise AttestationError(
            "Refused: replayed evidence, it answers another nonce than this session's fresh one.",
            hint=hint,
        )
    if bound_key != key:
        raise AttestationError(
            "Refused: the quote binds another TLS key than this session's (a relayed quote).",
            hint=hint,
        )
    ingress = [k for _, k in listeners]
    if binding(nonce, key, host_keys, instance_bytes, ingress, containers) != user_data:
        raise AttestationError(
            "Refused: the bound material does not hash to the quote's user data.", hint=hint
        )
    metadata = said.get("metadata")
    accepts = said.get("accepts")
    firewall = said.get("firewall")
    return Evidence(
        nonce=nonce,
        tls_key=key,
        user_data=user_data,
        document=str(said.get("document") or ""),
        metadata=metadata if isinstance(metadata, Mapping) else None,
        host_keys=host_keys,
        instance_key=instance_key,
        mode=str(said.get("mode") or "") or None,
        accepts=accepts if isinstance(accepts, Mapping) else {},
        attested_tls=listeners,
        firewall=firewall if isinstance(firewall, Mapping) else None,
        containers=containers,
    )


def _same_session(channel: Channel, key: bytes) -> None:
    """Refuse unless *channel*'s TLS key is still *key*: a relay moved the session."""
    if channel.peer_key() != key:
        raise AttestationError(
            "Refused: the session is not the one the quote bound (its TLS key changed); "
            "nothing was sent.",
            hint="Something between this host and the instance took the session over.",
        )


def exchange(
    channel: Channel, evidence: Evidence, path: str, payload: Mapping[str, object]
) -> dict[str, Any]:
    """:func:`post` *payload*, and the endpoint's JSON answer."""
    _same_session(channel, evidence.tls_key)
    body = json.dumps({"nonce": evidence.nonce.hex(), **payload}).encode()
    status, reply = channel.request("POST", path, body)
    said = _json(status, reply, path)
    if status == 200:
        return said
    problem, _, fix = str(said.get("error") or "no reason").partition("\nfix: ")
    raise AttestationError(
        f"The attested endpoint refused {path} ({status}): {problem}.",
        hint=fix or "Read `journalctl -u tundravm-endpoint -u mode-switch` in the guest.",
    )


def post(channel: Channel, evidence: Evidence, path: str, payload: Mapping[str, object]) -> int:
    """Write *payload* on *channel* with *evidence*'s nonce, which the endpoint takes once,
    only while the session's TLS key is still the one the quote bound."""
    _same_session(channel, evidence.tls_key)
    body = json.dumps({"nonce": evidence.nonce.hex(), **payload}).encode()
    status, reply = channel.request("POST", path, body)
    if status == 200:
        return status
    said = _json(status, reply, path)
    raise AttestationError(
        f"The attested endpoint refused {path} ({status}): {said.get('error') or 'no reason'}.",
        hint="A credential and a volume take one value per boot; restart the instance to "
        "deliver again.",
    )


def identity_path(identity: Path | None) -> Path:
    """The operator's SSH private key: *identity*, else ``$TUNDRAVM_SSH_KEY``, else
    ``~/.ssh/id_ed25519``; its public half must be beside it as ``.pub``."""
    chosen = identity or Path(os.environ.get("TUNDRAVM_SSH_KEY") or DEFAULT_IDENTITY)
    path = chosen.expanduser()
    if not path.is_file() or not path.with_name(path.name + ".pub").is_file():
        raise ValidationError(
            f"No SSH key pair at {path} (and {path}.pub).",
            hint="Pass --identity KEY (an ed25519 key: `ssh-keygen -t ed25519 -f KEY`), or set "
            "TUNDRAVM_SSH_KEY.",
        )
    return path


@dataclass(frozen=True, slots=True)
class Operator:
    """The operator's SSH key pair: the private key that signs, its public line and its
    fingerprint."""

    path: Path
    public: str
    fingerprint: str


def operator(identity: Path | None) -> Operator:
    """The key pair :func:`identity_path` finds."""
    path = identity_path(identity)
    words = path.with_name(path.name + ".pub").read_text(encoding="utf-8").split()
    public = " ".join(words[:2])
    try:
        found = _ssh_sig.fingerprint(public)
    except ValidationError as exc:
        raise ValidationError(
            f"{path}.pub is not an OpenSSH public key line.", hint="ssh-keygen -y -f KEY"
        ) from exc
    return Operator(path, public, found)


def recorded_operator(instance: Instance) -> Operator | None:
    """The key the last passing ``attest`` used, else the default key pair; ``None`` without
    one."""
    recorded = instance.data.get("operator")
    identity = Path(recorded["identity"]) if isinstance(recorded, dict) else None
    try:
        return operator(identity)
    except ValidationError:
        if identity is not None:
            raise
        return None


def signed(instance: Instance, evidence: Evidence, key: Operator | None) -> dict[str, str]:
    """The ``signer`` and ``signature`` a write to *instance* carries when its image pins its
    operator (nothing when it does not), refusing before anything is sent a write the
    endpoint would refuse, and a volume that came back without the owner the receipt names."""
    if not evidence.pins_owner:
        return {}
    if key is None:
        raise ValidationError(
            f"{instance.id} takes writes signed by its operator's SSH key, and there is none.",
            hint=f"Attest with your key first: `tundravm attest {instance.id} --policy FILE "
            "--identity KEY`.",
        )
    if evidence.owner_mismatch is not None:
        raise AttestationError(
            f"Refused: {evidence.owner_mismatch} on {instance.id}: its header was rewritten "
            "outside the instance, and the volume stays closed; nothing was sent.",
            hint="Stop the instance and keep the disk; attach it to an instance you attested, "
            "whose owner is the key the volume itself names.",
        )
    owner = evidence.owner
    if owner is not None and owner != key.fingerprint:
        raise AttestationError(
            f"Refused: {instance.id} is owned by {owner}, not {key.fingerprint} ({key.path}); "
            "nothing was sent.",
            hint=f"Use the owner's key: `tundravm attest {instance.id} --policy FILE --identity "
            "OWNER_KEY` (a lost key means a new volume).",
        )
    recorded = instance.data.get("owner")
    if owner is None and isinstance(recorded, str):
        raise AttestationError(
            f"Refused: {instance.id} is owned by {recorded}, but its volume came back without an "
            "owner (a replaced header or another disk); nothing was sent.",
            hint=f"Attach the instance's own volume; to start over on a new one, launch a new "
            f"instance instead of reusing {instance.id}.",
        )
    signature = _ssh_sig.sign(
        evidence.user_data.hex().encode(), key.path, namespace=SIGNATURE_NAMESPACE
    )
    return {"signer": key.public, "signature": signature.decode()}


# ── policy ───────────────────────────────────────────────────────────────


def read_policy(path: Path, instance: Instance) -> tuple[dict[str, Any], str]:
    """The verifier policy in *path*, and the sha256 of the file.

    *path* is a ``tundravm.policy/2`` file (the operator-approved copy), or a build's
    ``measurements.json``, whose record for the instance's platform and disk boot path is
    taken (its ``policy``)."""
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ValidationError(
            f"Cannot read the policy {path}: {exc}.",
            hint="Pass a tundravm.policy/2 file, or a build's measurements.json.",
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if isinstance(payload, dict) and payload.get("schema") == mf.MEASUREMENTS_SCHEMA:
        records = [
            r
            for r in mf.records_of(payload)
            if r.get("target") == instance.platform and r.get("boot") == "disk"
        ]
        if not records or not isinstance(records[0].get("policy"), dict):
            state = records[0].get("state") if records else "absent"
            raise ValidationError(
                f"{path} has no {instance.platform} disk prediction ({state}).",
                hint="Attest against a build whose measurements were predicted, or pass a "
                "policy file.",
            )
        return dict(records[0]["policy"]), digest
    if not isinstance(payload, dict):
        raise ValidationError(f"{path} is not a JSON object.", hint="Pass a policy file.")
    return payload, digest


# ── attest ───────────────────────────────────────────────────────────────


def _session(instance: Instance, connect: Connector | None) -> tuple[Channel, str]:
    port = instance.port(MANAGEMENT_PORT) or MANAGEMENT_PORT
    return (connect or _tls)(instance.host, port), f"{instance.host}:{port}"


def verify(
    instance: Instance,
    data: Mapping[str, Any],
    channel: Channel,
    where: str,
    *,
    validator: str | None = None,
) -> tuple[Attestation, Evidence]:
    """Fresh evidence on *channel*, bound to it, judged against the policy *data* for the
    build's kind, with the guest gate's mode the evidence carries."""
    evidence = fetch_evidence(channel, random.token_bytes(32), where)
    result = check_quote(
        f"https://{where}/evidence",
        data,
        validator=validator,
        nonce=evidence.nonce,
        user_data=evidence.user_data,
        allow_simulated=instance.mode == "simulated",
        production=instance.data["build"]["kind"] == "production",
        guest_mode=evidence.mode,
        caller=ENDPOINT_CALLER,
        transport=evidence.transport(),
    )
    if evidence.firewall is not None:
        rtmr3 = next((r.actual for r in result.registers if r.name == "RTMR3"), None)
        allowed = data.get("modes")
        history = mode_history(
            evidence.mode_events, rtmr3, None if not isinstance(allowed, list) else allowed
        )
        result = replace(result, modes=history)
        instance.data["firewall_mode"] = {
            "mode": history.current,
            "switches": len(history.lines),
            "replays": history.replays,
            "at": result.observed_at,
        }
    return result, evidence


def _known_hosts(instance: Instance, evidence: Evidence) -> None:
    """The bound host keys as ``known_hosts``: the host's on its SSH port, each
    container's on the host port that reaches its sshd."""

    def name(guest: int) -> str:
        port = instance.port(guest) or guest
        return instance.host if port == SSH_PORT else f"[{instance.host}]:{port}"

    lines = [f"{name(SSH_PORT)} {key}\n" for key in evidence.host_keys]
    lines += [f"{name(c.port)} {key}\n" for c in evidence.containers for key in c.host_keys]
    known_hosts(instance).write_text("".join(lines), encoding="utf-8")


def pin_host_keys(instance: Instance, *, connect: Connector | None = None) -> Attestation:
    """A local boot's ``known_hosts``, written as :func:`attest` writes it: fresh evidence
    from the attested endpoint on the forwarded management port, bound to the session and
    judged against the build's own ``measurements.json``. Nothing is handed over and the
    receipt records no attestation; the operator's ``attest`` against an approved policy
    does both."""
    data, digest = read_policy(instance.build.path / mf.MEASUREMENTS, instance)
    channel, where = _session(instance, connect)
    result, evidence = verify(instance, data, channel, where)
    if result.passed:
        _known_hosts(instance, evidence)
        instance.event("pinned", verdict=result.verdict, policy_sha256=digest)
    return result


def attest(
    instance: Instance,
    policy: Path,
    *,
    identity: Path | None = None,
    issuer: str | None = None,
    validator: str | None = None,
    connect: Connector | None = None,
    tls: int | None = None,
    presented: Presented | None = None,
) -> Attestation:
    """Fresh evidence from *instance*'s attested endpoint, bound to the TLS session, checked
    against *policy*; delivers no secret.

    A passing attestation records the policy and the bound instance public key in the
    receipt (``secrets unlock`` reuses the policy), writes the bound SSH host keys as the
    instance's ``known_hosts`` and, when the image takes one, hands the operator's SSH
    public key (*identity*, the default key pair when there is one) over the same session.
    *issuer* checks a quote from a tdxs issuer the operator relays instead: nothing binds it
    to a session, so nothing is written or sent. *tls* also connects to the attested TLS
    listener on that guest port and refuses, before anything is written, unless the
    certificate it presents carries the key this same evidence binds for it
    (:func:`check_certificate`); the result then carries the port and the fingerprint."""
    data, digest = read_policy(policy, instance)
    evidence: Evidence | None = None
    sent_key: str | None = None
    if issuer is not None and tls is not None:
        raise ValidationError(
            "--tls checks a certificate against the attested endpoint's evidence, which "
            "--issuer does not fetch.",
            hint="Drop --issuer: `tundravm attest INSTANCE --policy FILE --tls PORT`.",
        )
    key: Operator | None = None
    if issuer is not None:
        result = check_quote(
            issuer,
            data,
            validator=validator,
            allow_simulated=instance.mode == "simulated",
            production=instance.data["build"]["kind"] == "production",
            transport=connect_issuer(issuer),
        )
    else:
        channel, where = _session(instance, connect)
        result, evidence = verify(instance, data, channel, where, validator=validator)
        if tls is not None:
            der = (presented or _presented)(instance.host, instance.port(tls) or tls)
            result = replace(
                result, certificate=(tls, check_certificate(evidence, tls, der).fingerprint)
            )
        if evidence.instance_key:
            result = replace(result, instance_key=evidence.instance_key)
            instance.data["instance_key"] = evidence.instance_key
        if result.passed:
            _known_hosts(instance, evidence)
            key = _operator(identity)
            sent_key = _hand_ssh_key(instance, channel, evidence, key)
    instance.event(
        "attested",
        verdict=result.verdict,
        passed=result.passed,
        guest_mode=result.guest_mode,
        policy=str(policy.resolve()),
        policy_sha256=digest,
        nonce=result.nonce,
        channel=CHANNEL if issuer is None else f"issuer {issuer} (not bound to a session)",
        instance_key=result.instance_key,
        certificate=result.certificate,
        ssh_key=sent_key,
        owner=None if evidence is None else evidence.owner,
    )
    if result.passed:
        instance.data["attested"] = {
            "policy": str(policy.resolve()),
            "policy_sha256": digest,
            "verdict": result.verdict,
            "at": result.observed_at,
        }
        if key is not None and evidence is not None and evidence.pins_owner:
            instance.data["operator"] = {
                "identity": str(key.path),
                "fingerprint": key.fingerprint,
            }
    if result.passed or result.instance_key is not None:
        instance.save()
    return result


def _operator(identity: Path | None) -> Operator | None:
    """*identity*'s key pair (which must exist), else the default one when there is one."""
    try:
        return operator(identity)
    except ValidationError:
        if identity is not None:
            raise
        return None


def _hand_ssh_key(
    instance: Instance, channel: Channel, evidence: Evidence, key: Operator | None
) -> str | None:
    """POST the operator's SSH public key when the image takes one, signed when it pins its
    operator; its fingerprint."""
    if not evidence.takes_ssh_key or key is None:
        return None
    post(channel, evidence, "/ssh-key", {"key": key.public, **signed(instance, evidence, key)})
    return key.fingerprint


def fail_closed(instance: Instance) -> str | None:
    """Why nothing may be released to *instance* whatever it answers: a production build in
    a simulated instance, whose services the execution gate never starts."""
    if instance.mode == "simulated" and instance.data["build"]["kind"] == "production":
        return (
            "a production build in a simulated instance is fail-closed: its services never "
            "start without TDX, and nothing is released to it"
        )
    return None


def release_predicate(instance: Instance, result: Attestation) -> str | None:
    """Why *result* may not release a secret to *instance*; ``None`` when it may.

    A production build on TDX needs ``trusted`` (a validator checked the quote, with full
    coverage), no simulation and no ``DEBUG`` attribute; a development build on TDX accepts
    ``measurements-match``; a simulated instance of a development build accepts
    ``simulated`` only, and one of a production build nothing (:func:`fail_closed`).
    Partial coverage never releases. The channel binding is checked before any verdict
    (:func:`fetch_evidence`), so a relayed or replayed quote never reaches here.
    """
    refused = fail_closed(instance)
    if refused is not None:
        return refused
    verdict = result.verdict
    if instance.mode == "simulated":
        return None if verdict == "simulated" else f"a simulated instance answered {verdict}"
    if result.simulated:
        return "a TDX instance answered with simulated evidence"
    if instance.data["build"]["kind"] == "production":
        if result.debug:
            return "the TD has the DEBUG attribute: the host can read it"
        return None if verdict == "trusted" else f"production delivery needs trusted, got {verdict}"
    if verdict in ("trusted", "measurements-match"):
        return None
    return f"verdict {verdict}"


def _values(pairs: Sequence[str]) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for pair in pairs:
        name, sep, source = pair.partition("=")
        if not sep or not source.startswith("@") or not _NAME.fullmatch(name):
            raise ValidationError(
                f"{pair!r} is not NAME=@FILE.",
                hint="Name each secret and the file holding its value: jwt=@jwt.txt "
                "(the value never goes on the command line).",
            )
        path = Path(source[1:]).expanduser()
        try:
            found.append((name, path.read_bytes()))
        except OSError as exc:
            raise ValidationError(f"Cannot read {path}: {exc.strerror}.", hint="") from exc
    return found


def _released(
    instance: Instance,
    policy: Path,
    event: str,
    validator: str | None,
    connect: Connector | None,
    **fields: object,
) -> tuple[Attestation, Evidence, Channel]:
    """Verify on a session of its own and apply :func:`release_predicate`."""
    data, _ = read_policy(policy, instance)
    channel, where = _session(instance, connect)
    result, evidence = verify(instance, data, channel, where, validator=validator)
    refused = release_predicate(instance, result)
    if refused is not None:
        instance.event(event, reason=refused, **fields)
        raise AttestationError(
            f"Nothing sent to {instance.id}: {refused}.",
            hint="Attest with a validator (--validator) and an approved policy.",
        )
    return result, evidence, channel


def deliver(
    instance: Instance,
    policy: Path,
    pairs: Sequence[str],
    *,
    validator: str | None = None,
    connect: Connector | None = None,
) -> tuple[Attestation, tuple[str, ...]]:
    """Verify, apply :func:`release_predicate`, then write each ``NAME=@FILE`` credential
    on the bound session (``POST /credentials``)."""
    values = _values(pairs)
    _refuse_fail_closed(instance, "delivery-refused")
    profile = instance.build.profile
    declared = {str(s["name"]) for s in instance.build.runtime().get("secrets", ())}
    _known(values, declared, profile)
    result, evidence, channel = _released(instance, policy, "delivery-refused", validator, connect)
    _known(values, set(evidence.credentials), profile, every=True)
    encoded = {name: base64.b64encode(value).decode() for name, value in values}
    signature = signed(instance, evidence, recorded_operator(instance))
    post(channel, evidence, "/credentials", {"credentials": encoded, **signature})
    names = tuple(name for name, _ in values)
    instance.event(
        "delivered",
        secrets=list(names),
        verdict=result.verdict,
        channel=CHANNEL,
        owner=evidence.owner or _fingerprint_of(signature),
    )
    return result, names


def _known(
    values: Sequence[tuple[str, bytes]], declared: set[str], profile: str, every: bool = False
) -> None:
    """Refuse a name *profile* does not read (*declared*); nothing declared is no check
    unless *every* (the verified endpoint's list, which is the profile's)."""
    unknown = [name for name, _ in values if name not in declared]
    if unknown and (declared or every):
        raise ValidationError(
            f"Profile {profile} reads no secret {', '.join(unknown)}, so its endpoint does "
            "not take it.",
            hint=f"It reads: {', '.join(sorted(declared)) or 'none'}.",
        )


def _refuse_fail_closed(instance: Instance, event: str, **fields: object) -> None:
    """Refuse before anything is sent when :func:`fail_closed` says so."""
    refused = fail_closed(instance)
    if refused is None:
        return
    instance.event(event, reason=refused, **fields)
    raise AttestationError(
        f"Nothing sent to {instance.id}: {refused}.",
        hint="Run a development build (`tundravm run --dev --mode simulated`) to try values, "
        "or the production build on TDX (`--mode tdx`, `deploy`).",
    )


def read_passphrase(volume: str) -> bytes:
    """The passphrase from the terminal (not echoed), or standard input when piped."""
    if sys.stdin.isatty():
        return getpass.getpass(f"passphrase for volume {volume}: ").encode()
    return sys.stdin.buffer.read().rstrip(b"\n")


def unlock(
    instance: Instance,
    volume: str,
    passphrase: bytes | Callable[[], bytes],
    *,
    validator: str | None = None,
    connect: Connector | None = None,
) -> Attestation:
    """Verify again with the last approved policy, then hand an ``OperatorKey()`` volume's
    passphrase to ``key-gen`` on the bound session (``POST /unlock``).

    *passphrase* may be a callable: it is asked for only once the attestation released it
    (so a refused unlock never prompts)."""
    _refuse_fail_closed(instance, "unlock-refused", volume=volume)
    volumes = {str(v["name"]): v for v in instance.build.runtime().get("volumes", ())}
    found = volumes.get(volume)
    if volumes and found is None:
        raise ValidationError(
            f"The image declares no volume {volume!r}.",
            hint=f"Volumes: {', '.join(sorted(volumes))}.",
        )
    if found is not None and (found.get("unlock") or {}).get("kind") != "operator-key":
        raise ValidationError(
            f"Volume {volume} does not unlock with an operator key.",
            hint="Only an OperatorKey() volume takes a passphrase over the control plane.",
        )
    approved = instance.data.get("attested")
    if not isinstance(approved, dict):
        raise StateError(
            f"{instance.id} has not passed an attestation yet.",
            hint=f"Run `tundravm attest {instance.id} --policy FILE` first.",
        )
    result, evidence, channel = _released(
        instance, Path(approved["policy"]), "unlock-refused", validator, connect, volume=volume
    )
    if volume not in evidence.volumes:
        raise ValidationError(
            f"The image declares no volume {volume!r}.",
            hint=f"Volumes: {', '.join(evidence.volumes) or 'none'}.",
        )
    signature = signed(instance, evidence, recorded_operator(instance))
    value = passphrase() if callable(passphrase) else passphrase
    if not value:
        raise ValidationError("An empty passphrase.", hint="Type it, or pipe it on stdin.")
    passphrase_b64 = base64.b64encode(value).decode()
    post(
        channel, evidence, "/unlock", {"volume": volume, "passphrase": passphrase_b64, **signature}
    )
    owner = evidence.owner or _fingerprint_of(signature)
    if owner is not None:
        instance.data["owner"] = owner
        instance.save()
    instance.event("unlocked", volume=volume, verdict=result.verdict, channel=CHANNEL, owner=owner)
    return result


def _fingerprint_of(signature: Mapping[str, str]) -> str | None:
    """The signer's fingerprint of a :func:`signed` write, ``None`` for an unsigned one."""
    signer = signature.get("signer")
    return None if signer is None else _ssh_sig.fingerprint(signer)


def switch(
    instance: Instance,
    mode: str,
    *,
    validator: str | None = None,
    connect: Connector | None = None,
) -> tuple[Attestation, dict[str, Any]]:
    """Verify again with the last approved policy, then hand *instance*'s firewall *mode* to
    its switcher on the bound session (``POST /mode``), signed by the owner when the image
    pins one: the switcher extends RTMR3 with the switch before the ruleset changes. A
    production build switches only for a pinned operator; the CLI refuses before sending.
    Returns the attestation and the switch (``mode``, ``previous``, ``at``, ``until``,
    ``switches``, ``event``)."""
    approved = instance.data.get("attested")
    if not isinstance(approved, dict):
        raise StateError(
            f"{instance.id} has not passed an attestation yet.",
            hint=f"Run `tundravm attest {instance.id} --policy FILE` first.",
        )
    result, evidence, channel = _released(
        instance, Path(approved["policy"]), "switch-refused", validator, connect, mode=mode
    )
    if evidence.firewall is None:
        raise ValidationError(
            f"{instance.id}'s image has no firewall modes.",
            hint='Declare Firewall(modes={"production": Mode(), "maintenance": '
            "Mode(ingress=[TCP(22)])}) and build again.",
        )
    if mode not in evidence.firewall_modes:
        raise ValidationError(
            f"{instance.id}'s image has no firewall mode {mode!r}.",
            hint=f"Modes: {', '.join(evidence.firewall_modes)}.",
        )
    if instance.data["build"]["kind"] == "production" and not evidence.pins_owner:
        raise AttestationError(
            f"Refused: {instance.id} is a production build that pins no operator, so anyone "
            "reaching its endpoint could open a window; nothing was sent.",
            hint="Give the image a Volume(unlock=OperatorKey()): its owner alone switches.",
        )
    signature = signed(instance, evidence, recorded_operator(instance))
    done = exchange(channel, evidence, "/mode", {"mode": mode, **signature})
    instance.data["firewall_mode"] = {
        "mode": done.get("mode"),
        "switches": done.get("switches"),
        "until": done.get("until") or None,
        "at": result.observed_at,
    }
    instance.save()
    instance.event(
        "switched",
        mode=done.get("mode"),
        previous=done.get("previous"),
        boottime=done.get("at"),
        until=done.get("until") or None,
        rtmr3_event=done.get("event"),
        channel=CHANNEL,
        owner=evidence.owner or _fingerprint_of(signature),
    )
    return result, done


def rotate(
    instance: Instance,
    identity: Path,
    *,
    validator: str | None = None,
    connect: Connector | None = None,
) -> tuple[Attestation, str]:
    """Verify again with the last approved policy, then hand *instance*'s volumes to the key
    pair at *identity* (``POST /owner``): signed by the recorded owner's key, carrying the
    new key and its own signature over the same user data. The receipt then records the new
    key; its fingerprint is returned."""
    new = operator(identity)
    approved = instance.data.get("attested")
    if not isinstance(approved, dict):
        raise StateError(
            f"{instance.id} has not passed an attestation yet.",
            hint=f"Run `tundravm attest {instance.id} --policy FILE --identity KEY` first.",
        )
    result, evidence, channel = _released(
        instance, Path(approved["policy"]), "rotate-refused", validator, connect
    )
    if not evidence.pins_owner:
        raise ValidationError(
            f"{instance.id} pins no operator: its image has no OperatorKey() volume.",
            hint="Only an OperatorKey() volume's owner rotates.",
        )
    if evidence.owner is None:
        raise ValidationError(
            f"No one owns {instance.id}'s volumes yet: nothing to rotate.",
            hint=f"Attest with the key you want: `tundravm attest {instance.id} --policy FILE "
            "--identity KEY` (its first signed write pins it).",
        )
    old = signed(instance, evidence, recorded_operator(instance))
    proof = _ssh_sig.sign(
        evidence.user_data.hex().encode(), new.path, namespace=SIGNATURE_NAMESPACE
    )
    post(
        channel,
        evidence,
        "/owner",
        {"key": new.public, "key_signature": proof.decode(), **old},
    )
    instance.data["operator"] = {"identity": str(new.path), "fingerprint": new.fingerprint}
    instance.data["owner"] = new.fingerprint
    instance.save()
    instance.event("rotated", owner=new.fingerprint, previous=evidence.owner, channel=CHANNEL)
    return result, new.fingerprint


__all__ = [
    "CHANNEL",
    "CREDENTIALS",
    "PURPOSE",
    "Certificate",
    "Channel",
    "Connector",
    "Evidence",
    "Presented",
    "TlsChannel",
    "attest",
    "binding",
    "certificate_key",
    "check_certificate",
    "deliver",
    "fail_closed",
    "fetch_evidence",
    "identity_path",
    "key_name",
    "operator",
    "recorded_operator",
    "rotate",
    "signed",
    "switch",
    "post",
    "read_passphrase",
    "pin_host_keys",
    "read_policy",
    "release_predicate",
    "unlock",
    "verify",
]
