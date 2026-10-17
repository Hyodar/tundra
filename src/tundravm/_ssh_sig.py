"""SSH signatures over evidence: ``ssh-keygen -Y sign`` and ``-Y verify``, no Python dependency.

The namespace is :data:`NAMESPACE` (a release manifest's :data:`RELEASE_NAMESPACE`), so
a signature made for evidence cannot be replayed as a git commit or file signature, nor
one of those as evidence, nor an evidence signature as a release manifest's.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import StateError, ValidationError

NAMESPACE = "tundravm-evidence"
"""The ``-n`` namespace every evidence signature is made and checked in."""
RELEASE_NAMESPACE = "tundravm-release"
"""The ``-n`` namespace a release manifest (``release-manifest.json.sig``) is signed in."""
TOOL = "ssh-keygen"
_TIMEOUT = 60

SigRunner = Callable[[Sequence[str], bytes | None], subprocess.CompletedProcess[bytes]]
"""Runs an ``ssh-keygen`` argv with optional stdin bytes (tests replace it)."""


@dataclass(frozen=True, slots=True)
class SignatureCheck:
    """What ``ssh-keygen -Y verify`` said: ``valid``, the ``signer`` principal, its ``message``."""

    valid: bool
    signer: str | None
    message: str


def _tool() -> str:
    found = shutil.which(TOOL)
    if found is None:
        raise StateError(
            f"`{TOOL}` is not on PATH: evidence signatures need it.",
            hint=(
                "Install OpenSSH's client tools (Debian/Ubuntu: apt install openssh-client; "
                "macOS ships it), then run the command again."
            ),
            context={"tool": TOOL},
        )
    return found


def _run(argv: Sequence[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        input=stdin if stdin is not None else b"",
        capture_output=True,
        check=False,
        timeout=_TIMEOUT,
    )


def _said(result: subprocess.CompletedProcess[bytes]) -> str:
    text = (result.stderr or b"").decode(errors="replace").strip()
    text = text or (result.stdout or b"").decode(errors="replace").strip()
    return text.splitlines()[-1] if text else f"exit status {result.returncode}"


def sign(
    data: bytes, key: Path, *, namespace: str = NAMESPACE, runner: SigRunner | None = None
) -> bytes:
    """The armored SSH signature of *data* by the private key at *key*, in *namespace*."""
    path = Path(key)
    if not path.is_file():
        raise ValidationError(
            f"Signing key {path} does not exist.",
            hint=(
                "Pass an SSH private key, for example one made with "
                "`ssh-keygen -t ed25519 -f release-key -N ''`."
            ),
            context={"key": str(path)},
        )
    run = runner if runner is not None else _run
    tool = TOOL if runner is not None else _tool()
    with tempfile.TemporaryDirectory(prefix="tundravm-sign-") as scratch:
        target = Path(scratch) / "evidence.json"
        target.write_bytes(data)
        result = run(
            [tool, "-Y", "sign", "-q", "-f", str(path), "-n", namespace, str(target)], None
        )
        signature = target.with_name("evidence.json.sig")
        if result.returncode != 0 or not signature.is_file():
            raise ValidationError(
                f"ssh-keygen could not sign with {path}: {_said(result)}.",
                hint=(
                    "Pass an SSH private key ssh-keygen can read without a prompt: one "
                    "without a passphrase, or its .pub with the private key in ssh-agent."
                ),
                context={"key": str(path)},
            )
        return signature.read_bytes()


def public_key(key: Path, *, runner: SigRunner | None = None) -> str:
    """The public half of the SSH private key at *key* (``ssh-keygen -y``): ``TYPE BASE64``."""
    path = Path(key)
    if not path.is_file():
        raise ValidationError(
            f"Signing key {path} does not exist.",
            hint=(
                "Pass an SSH private key, for example one made with "
                "`ssh-keygen -t ed25519 -f release-key -N ''`."
            ),
            context={"key": str(path)},
        )
    run = runner if runner is not None else _run
    tool = TOOL if runner is not None else _tool()
    result = run([tool, "-y", "-P", "", "-f", str(path)], None)
    fields = (result.stdout or b"").decode(errors="replace").split()
    if result.returncode != 0 or len(fields) < 2:
        raise ValidationError(
            f"ssh-keygen could not read the public key of {path}: {_said(result)}.",
            hint="Pass an SSH private key without a passphrase, readable by this user.",
            context={"key": str(path)},
        )
    return f"{fields[0]} {fields[1]}"


def fingerprint(public: str) -> str:
    """The ``SHA256:...`` fingerprint ``ssh-keygen -l`` prints for *public* (``TYPE BASE64``)."""
    words = public.split()
    try:
        blob = base64.b64decode(words[1], validate=True)
    except (IndexError, binascii.Error) as exc:
        raise ValidationError(
            f"{public!r} is not an SSH public key.",
            hint="Pass the key as `ssh-keygen -y -f KEY` prints it: `TYPE BASE64`.",
        ) from exc
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def verify(
    data: bytes,
    signature: bytes,
    allowed_signers: Path,
    *,
    signer: str | None = None,
    namespace: str = NAMESPACE,
    runner: SigRunner | None = None,
) -> SignatureCheck:
    """Check *signature* over *data* against *allowed_signers* (an OpenSSH allowed-signers file),
    in *namespace*.

    *signer* is the principal to check; without it the principals the file lists
    for the signing key (``ssh-keygen -Y find-principals``) are tried in order.
    """
    allowed = Path(allowed_signers)
    if not allowed.is_file():
        raise ValidationError(
            f"Allowed-signers file {allowed} does not exist.",
            hint=(
                "Pass an OpenSSH allowed-signers file: one line per signer, "
                '`PRINCIPAL namespaces="tundravm-evidence" ssh-ed25519 AAAA...`.'
            ),
            context={"allowed_signers": str(allowed)},
        )
    run = runner if runner is not None else _run
    tool = TOOL if runner is not None else _tool()
    with tempfile.TemporaryDirectory(prefix="tundravm-verify-") as scratch:
        sig = Path(scratch) / "evidence.json.sig"
        sig.write_bytes(signature)
        if signer is not None:
            principals = [signer]
        else:
            found = run([tool, "-Y", "find-principals", "-s", str(sig), "-f", str(allowed)], None)
            principals = [
                line.strip()
                for line in (found.stdout or b"").decode(errors="replace").splitlines()
                if line.strip()
            ]
            if found.returncode != 0 or not principals:
                return SignatureCheck(
                    False, None, f"no principal in {allowed} holds the signing key"
                )
        last = ""
        for principal in principals:
            argv = [tool, "-Y", "verify", "-f", str(allowed), "-I", principal, "-n", namespace]
            result = run([*argv, "-s", str(sig)], data)
            if result.returncode == 0:
                said = (result.stdout or b"").decode(errors="replace").strip()
                return SignatureCheck(True, principal, said or f"good signature for {principal}")
            last = _said(result)
        return SignatureCheck(False, signer, last or "the signature does not verify")
