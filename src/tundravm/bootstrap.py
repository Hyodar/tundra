"""``attest`` and ``secrets``: today's control-plane bootstrap, said to be what it is.

Until gate 3's attested endpoint exists (plan 6.3), the way into an instance is the one
gate 4B lowered: an image with a volume or a secret runs tundra-tools' ``secret-delivery``
on the management port :data:`~tundravm.instances.MANAGEMENT_PORT`, which takes the
operator's SSH public key once (an HTTP ``POST`` of its base64 body) and lets that key in
as root. That first contact is **unverified**: nothing binds the key exchange, the SSH host
key or the session to a quote, and every command, its output and each receipt says so.

- :func:`attest` asks the image's tdxs issuer for a quote over that SSH session (one
  ``socat - UNIX-CONNECT:/var/tdxs.sock`` per request), checks it against the policy with
  :func:`tundravm.attestation.attest`, and delivers nothing. On success the session's host
  key becomes the instance's ``known_hosts``.
- :func:`deliver` attests again (fresh nonce), applies :func:`release_predicate`, then
  writes each credential to ``/run/tundravm/credentials/<name>`` (mode 0600, written to a
  temporary name and renamed). :func:`unlock` attests again with the policy of the last
  successful ``attest`` and writes the passphrase into the volume's FIFO. Both refuse a
  production build in a simulated instance before anything is sent
  (:func:`fail_closed`): its services never start there, and nothing is released to it.
- The evidence is checked for the build's kind (``production``: simulated and debug
  evidence never pass) with the guest's execution-gate mode, read over the session
  (``/run/tundravm/mode``), which can only make it ``simulated``.

Values travel on the SSH command's standard input only: never on an argv, in a log, a tree,
a manifest or a receipt (receipts name what was delivered, not what it was).
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import manifest as mf
from ._modules.execution import MODE_FILE
from .attestation import Attestation, Transport
from .attestation import attest as check_quote
from .errors import AttestationError, StateError, ValidationError
from .instances import MANAGEMENT_PORT, SSH_PORT, Instance, known_hosts

UNVERIFIED = (
    "unverified first contact: the channel is the bootstrap SSH session, not bound to the "
    "quote (gate 3 adds the attested endpoint)"
)
ISSUER_SOCKET = "/var/tdxs.sock"
CREDENTIALS = "/run/tundravm/credentials"
DEFAULT_IDENTITY = Path("~/.ssh/id_ed25519")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

SshRunner = Callable[[Sequence[str], bytes], subprocess.CompletedProcess[bytes]]
"""Runs one ``ssh`` argv with bytes on its stdin (tests replace it)."""
Poster = Callable[[str, bytes], int]
"""POSTs bytes to a URL and returns the HTTP status (tests replace it)."""


def _run_ssh(argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(argv), input=stdin, capture_output=True, check=False, timeout=60)


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:  # noqa: S310 - operator's VM
            return int(reply.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError as exc:
        raise AttestationError(
            f"Cannot reach the management port at {url}: {exc}.",
            hint="Check the instance runs (`tundravm run status` / `deploy status`). "
            f":{MANAGEMENT_PORT} takes one key per boot and then closes: restart the instance, "
            "or use the key it took (--identity).",
        ) from exc


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


class Session:
    """The bootstrap SSH session to one instance."""

    def __init__(
        self,
        instance: Instance,
        identity: Path,
        *,
        runner: SshRunner | None = None,
        poster: Poster | None = None,
        hosts: Path | None = None,
    ) -> None:
        self.instance = instance
        self.identity = identity
        self.runner = runner or _run_ssh
        self.poster = poster or _post
        self.hosts = hosts or known_hosts(instance)

    def first_contact(self) -> None:
        """POST the operator's public key to the management port, once per instance."""
        bootstrap = self.instance.data.setdefault("bootstrap", {})
        public = self.identity.with_name(self.identity.name + ".pub").read_text().split()
        fingerprint = _fingerprint(public[1])
        if bootstrap.get("key") == fingerprint:
            return
        port = self.instance.port(MANAGEMENT_PORT)
        url = f"http://{self.instance.host}:{port}/"
        status = self.poster(url, public[1].encode())
        if status >= 300:
            raise AttestationError(
                f"The management port answered {status} to the operator key.",
                hint="secret-delivery takes one key per boot: an instance that already took "
                "another key does not take this one; restart it, or use that key (--identity).",
                context={"instance": self.instance.id},
            )
        bootstrap.update({"key": fingerprint, "first_contact": "unverified"})
        self.instance.save()
        self.instance.event("first-contact", key=fingerprint, note=UNVERIFIED)

    def argv(self, command: str) -> list[str]:
        port = self.instance.port(SSH_PORT) or SSH_PORT
        return [
            shutil.which("ssh") or "ssh",
            "-i",
            str(self.identity),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"UserKnownHostsFile={self.hosts}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"root@{self.instance.host}",
            command,
        ]

    def run(self, command: str, stdin: bytes = b"") -> bytes:
        result = self.runner(self.argv(command), stdin)
        if result.returncode != 0:
            said = (result.stderr or b"").decode(errors="replace").strip().splitlines()
            raise AttestationError(
                f"ssh to {self.instance.id} failed: {said[-1] if said else result.returncode}.",
                hint="The control plane takes the key POSTed to the management port; check "
                "the instance is up and that --identity is that key.",
                context={"instance": self.instance.id},
            )
        return result.stdout or b""

    def guest_mode(self) -> str | None:
        """What the guest's execution gate recorded (``tdx``, ``simulated``), ``None`` when
        it recorded nothing (an image older than the gate, or a refused production boot)."""
        said = self.run(f"head -c 64 {MODE_FILE} 2>/dev/null || true").decode(errors="replace")
        said = said.strip()
        return said or None

    def transport(self) -> Transport:
        """tdxs's JSON lines over this session (one ``socat`` per request)."""

        def send(request: Mapping[str, object]) -> Mapping[str, object]:
            line = json.dumps(request).encode() + b"\n"
            reply = self.run(f"socat - UNIX-CONNECT:{ISSUER_SOCKET}", line)
            try:
                decoded = json.loads(reply.split(b"\n", 1)[0])
            except ValueError as exc:
                raise AttestationError(
                    f"The issuer on {self.instance.id} did not answer with JSON.",
                    hint=f"Check tdxs runs ({ISSUER_SOCKET}).",
                ) from exc
            if not isinstance(decoded, dict):
                raise AttestationError(
                    "The issuer answered something that is not an object.", hint=""
                )
            return decoded

        return send


def _fingerprint(blob: str) -> str:
    digest = base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode()
    return "SHA256:" + digest.rstrip("=")


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


def attest(
    instance: Instance,
    policy: Path,
    *,
    identity: Path | None = None,
    issuer: str | None = None,
    validator: str | None = None,
    runner: SshRunner | None = None,
    poster: Poster | None = None,
    transport: Transport | None = None,
) -> Attestation:
    """Fresh evidence from *instance* checked against *policy*; delivers nothing.

    The quote comes over the bootstrap SSH session (or from *issuer*, an endpoint the
    operator relays themselves); a simulated instance of a development build may pass as
    ``simulated``. The evidence is checked for the build's kind (a production build's never
    passes simulated or with the ``DEBUG`` attribute) and, over the session, with the
    guest's execution-gate mode. A passing attestation records the policy in the receipt
    (``secrets unlock`` reuses it) and makes the session's host key the instance's
    ``known_hosts``.
    """
    data, digest = read_policy(policy, instance)
    simulated = instance.mode == "simulated"
    production = instance.data["build"]["kind"] == "production"
    with tempfile.TemporaryDirectory(prefix="tundravm-attest-") as scratch:
        hosts = Path(scratch) / "known_hosts"
        send = transport
        session: Session | None = None
        if send is None and issuer is None:
            session = Session(
                instance, identity_path(identity), runner=runner, poster=poster, hosts=hosts
            )
            session.first_contact()
            send = session.transport()
        result = check_quote(
            issuer or f"ssh://{instance.id}{ISSUER_SOCKET}",
            data,
            validator=validator,
            allow_simulated=simulated,
            production=production,
            guest_mode=None if session is None else session.guest_mode(),
            transport=send,
        )
        if result.passed and hosts.is_file():
            shutil.copyfile(hosts, known_hosts(instance))
    instance.event(
        "attested",
        verdict=result.verdict,
        passed=result.passed,
        guest_mode=result.guest_mode,
        policy=str(policy.resolve()),
        policy_sha256=digest,
        nonce=result.nonce,
        channel=UNVERIFIED if issuer is None else f"issuer {issuer}",
    )
    if result.passed:
        instance.data["attested"] = {
            "policy": str(policy.resolve()),
            "policy_sha256": digest,
            "verdict": result.verdict,
            "at": result.observed_at,
        }
        instance.save()
    return result


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
    Partial coverage never releases.
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


def deliver(
    instance: Instance,
    policy: Path,
    pairs: Sequence[str],
    *,
    identity: Path | None = None,
    validator: str | None = None,
    runner: SshRunner | None = None,
    poster: Poster | None = None,
    transport: Transport | None = None,
) -> tuple[Attestation, tuple[str, ...]]:
    """Attest, apply :func:`release_predicate`, then write each ``NAME=@FILE`` credential."""
    values = _values(pairs)
    _refuse_fail_closed(instance, "delivery-refused")
    declared = {str(s["name"]) for s in instance.build.runtime().get("secrets", ())}
    unknown = [name for name, _ in values if name not in declared]
    if unknown:
        raise ValidationError(
            f"The image declares no secret {', '.join(unknown)}.",
            hint=f"Declared: {', '.join(sorted(declared)) or 'none'}.",
        )
    key = identity_path(identity)
    result = attest(
        instance,
        policy,
        identity=key,
        validator=validator,
        runner=runner,
        poster=poster,
        transport=transport,
    )
    refused = release_predicate(instance, result)
    if refused is not None:
        instance.event("delivery-refused", reason=refused)
        raise AttestationError(
            f"Nothing delivered to {instance.id}: {refused}.",
            hint="Attest with a validator (--validator) and an approved policy.",
        )
    session = Session(instance, key, runner=runner, poster=poster)
    for name, value in values:
        session.run(
            f"umask 077 && cat > {CREDENTIALS}/.{name}.part && "
            f"mv {CREDENTIALS}/.{name}.part {CREDENTIALS}/{name}",
            value,
        )
    names = tuple(name for name, _ in values)
    instance.event("delivered", secrets=list(names), verdict=result.verdict, channel=UNVERIFIED)
    return result, names


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
    identity: Path | None = None,
    validator: str | None = None,
    runner: SshRunner | None = None,
    poster: Poster | None = None,
    transport: Transport | None = None,
) -> Attestation:
    """Attest again with the last approved policy, then open an ``OperatorKey()`` volume.

    *passphrase* may be a callable: it is asked for only once the attestation released it
    (so a refused unlock never prompts)."""
    _refuse_fail_closed(instance, "unlock-refused", volume=volume)
    volumes = {str(v["name"]): v for v in instance.build.runtime().get("volumes", ())}
    found = volumes.get(volume)
    if found is None:
        raise ValidationError(
            f"The image declares no volume {volume!r}.",
            hint=f"Volumes: {', '.join(sorted(volumes)) or 'none'}.",
        )
    if (found.get("unlock") or {}).get("kind") != "operator-key":
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
    key = identity_path(identity)
    result = attest(
        instance,
        Path(approved["policy"]),
        identity=key,
        validator=validator,
        runner=runner,
        poster=poster,
        transport=transport,
    )
    refused = release_predicate(instance, result)
    if refused is not None:
        instance.event("unlock-refused", volume=volume, reason=refused)
        raise AttestationError(f"{volume} not unlocked: {refused}.", hint="")
    value = passphrase() if callable(passphrase) else passphrase
    if not value:
        raise ValidationError("An empty passphrase.", hint="Type it, or pipe it on stdin.")
    session = Session(instance, key, runner=runner, poster=poster)
    session.run(f"cat > {found['pipe']}", value)
    instance.event("unlocked", volume=volume, verdict=result.verdict, channel=UNVERIFIED)
    return result


__all__ = [
    "CREDENTIALS",
    "ISSUER_SOCKET",
    "UNVERIFIED",
    "Poster",
    "Session",
    "SshRunner",
    "attest",
    "deliver",
    "fail_closed",
    "identity_path",
    "read_passphrase",
    "read_policy",
    "release_predicate",
    "unlock",
]
