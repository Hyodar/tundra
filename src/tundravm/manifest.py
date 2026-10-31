"""The build manifest: the deterministic identity of one build directory, and its check.

``manifest.json`` (schema :data:`SCHEMA`) binds, by sha256, every file of the build
directory but itself, its signature and ``observations.json`` (the *artifact index*), the
``tree/`` it was built from by a digest over paths, modes, contents and symlink targets
(:func:`tree_digest`), the embedded UKI and the root hash on its command line, the
lockfile, the source pins, mkosi's package manifest, the SBOM, the measurement records and
the toolchain. What a build could not establish is written down, never left out: a part
that does not exist is ``{"state": "missing", "reason": ...}`` and every guarantee the
build does not give is in ``unchecked``.

The file is :func:`canonical` bytes (sorted keys, two-space indent, ASCII, one trailing
newline), so a signature over the file is a signature over the identity, and the build id
is the first 16 hex digits of the sha256 of the canonical manifest without its ``id``.
``manifest.sig`` is an OpenSSH signature (``ssh-keygen -Y sign``) in the
:data:`NAMESPACE` namespace. :func:`verify` checks a directory against its manifest, one
:class:`Check` per guarantee, each ``ok``, ``failed``, ``missing`` or ``unchecked``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import _ssh_sig
from .errors import StateError, TdxError

SCHEMA = "tundravm.manifest/1"
OBSERVATIONS_SCHEMA = "tundravm.observations/1"
MEASUREMENTS_SCHEMA = "tundravm.measurements/1"
NAMESPACE = "tundravm-manifest"
"""The ``ssh-keygen -n`` namespace manifests are signed in: such a signature is never a git
commit's, a file's or another tool's."""

MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
OBSERVATIONS = "observations.json"
MEASUREMENTS = "measurements.json"
SBOM = "sbom.spdx.json"
PACKAGES = "packages.json"
INPUTS = "inputs"
TREE = "tree"
UKI = "payload.efi"
UNINDEXED = frozenset({MANIFEST, SIGNATURE, OBSERVATIONS})
"""The files the artifact index leaves out: the manifest itself, its signature and the
observations (timestamps, runner, reproduction attempts: not identity)."""

ID_LENGTH = 16
READ_ONLY_MASK = 0o222
"""The write bits, which the tree digest leaves out: a published build is read-only."""

State = Literal["ok", "failed", "missing", "unchecked"]


def canonical(payload: Mapping[str, Any]) -> bytes:
    """*payload* as the canonical manifest bytes."""
    return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def build_id(payload: Mapping[str, Any]) -> str:
    """The id of a manifest: the digest of its canonical bytes without ``id``."""
    body = {key: value for key, value in payload.items() if key != "id"}
    return hashlib.sha256(canonical(body)).hexdigest()[:ID_LENGTH]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def missing(reason: str) -> dict[str, str]:
    """The record of a part the build does not have."""
    return {"state": "missing", "reason": reason}


def file_record(path: Path) -> dict[str, object]:
    return {"sha256": sha256_file(path), "size": path.stat().st_size}


def _entries(root: Path) -> Iterator[tuple[str, os.stat_result, Path]]:
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        here = Path(directory)
        for name in sorted([*dirs, *files]):
            path = here / name
            yield path.relative_to(root).as_posix(), path.lstat(), path


def tree_digest(root: Path) -> tuple[str, int]:
    """``sha256:<hex>`` over every entry of *root*, and how many files it holds.

    One line per entry in path order (MODE without the write bits): ``d MODE PATH``,
    ``f MODE SHA256 PATH`` or
    ``l TARGET PATH`` (a symlink is never followed), so a changed mode, content or link
    target changes the digest.
    """
    lines: list[str] = []
    files = 0
    for rel, info, path in _entries(root):
        mode = f"{stat.S_IMODE(info.st_mode) & ~READ_ONLY_MASK:04o}"
        if stat.S_ISLNK(info.st_mode):
            lines.append(f"l {os.readlink(path)} {rel}")
        elif stat.S_ISDIR(info.st_mode):
            lines.append(f"d {mode} {rel}")
        else:
            files += 1
            lines.append(f"f {mode} {sha256_file(path)} {rel}")
    text = "\n".join(lines) + "\n"
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest(), files


def artifact_index(build: Path) -> dict[str, dict[str, object]]:
    """Every file of the build directory *build* outside ``tree/`` and :data:`UNINDEXED`,
    with its sha256 and size, by relative path."""
    found: dict[str, dict[str, object]] = {}
    for rel, info, path in _entries(build):
        if rel in UNINDEXED or rel == TREE or rel.startswith(f"{TREE}/"):
            continue
        if stat.S_ISREG(info.st_mode):
            found[rel] = file_record(path)
        elif stat.S_ISLNK(info.st_mode):
            found[rel] = {"symlink": os.readlink(path)}
    return found


def read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StateError(
            f"{path} does not exist.",
            hint=f"{what} is written by `tundravm build`; pass a build directory.",
            context={"path": str(path)},
        ) from exc
    except (OSError, ValueError) as exc:
        raise StateError(
            f"{path} is not readable JSON: {exc}",
            hint="A build directory is never edited; build again.",
            context={"path": str(path)},
        ) from exc
    if not isinstance(payload, dict):
        raise StateError(f"{path} is not a JSON object.", hint="Build again.")
    return payload


def load(build: Path) -> dict[str, Any]:
    """The manifest of the build directory *build*."""
    payload = read_json(build / MANIFEST, "manifest.json")
    if payload.get("schema") != SCHEMA:
        raise StateError(
            f"{build / MANIFEST} has schema {payload.get('schema')!r}, not {SCHEMA}.",
            hint="This tundravm reads tundravm.manifest/1 build directories; build again.",
            context={"path": str(build)},
        )
    return payload


def sign(build: Path, key: Path, *, runner: _ssh_sig.SigRunner | None = None) -> bytes:
    """The signature of *build*'s manifest bytes by the SSH private key *key*."""
    return _ssh_sig.sign((build / MANIFEST).read_bytes(), key, namespace=NAMESPACE, runner=runner)


# ── verify ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Check:
    """One guarantee about a build directory: its *name*, *state* and what was found."""

    name: str
    state: State
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "state": self.state, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Verification:
    """:func:`verify`'s answer: every :class:`Check`, and whether the build passes."""

    build: Path
    build_id: str
    checks: tuple[Check, ...]
    require_signature: bool = False
    signer: str | None = None
    unchecked: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        if any(check.state == "failed" for check in self.checks):
            return False
        if self.require_signature:
            return any(c.name == "signature" and c.state == "ok" for c in self.checks)
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "build": str(self.build),
            "id": self.build_id,
            "passed": self.passed,
            "require_signature": self.require_signature,
            "signer": self.signer,
            "checks": [check.to_dict() for check in self.checks],
            "unchecked": list(self.unchecked),
        }

    def render(self) -> str:
        width = max(len(check.name) for check in self.checks)
        lines = [f"build {self.build_id} ({self.build})"]
        for check in self.checks:
            detail = f"  {check.detail}" if check.detail else ""
            lines.append(f"  {check.name:<{width}}  {check.state}{detail}")
        for item in self.unchecked:
            lines.append(f"  unchecked: {item}")
        lines.append(f"verdict: {'pass' if self.passed else 'fail'}")
        return "\n".join(lines)


def verify(
    build: Path,
    *,
    require_signature: bool = False,
    allowed_signers: Path | None = None,
    runner: _ssh_sig.SigRunner | None = None,
) -> Verification:
    """Check the build directory *build* against its manifest, offline.

    The manifest must be canonical and its id the directory's name and its own digest;
    every indexed file must exist with its sha256, and no other file may; ``tree/`` must
    hash to the recorded digest; the UKI and its root hash must be the recorded ones. A
    signature is checked against *allowed_signers* (an OpenSSH allowed-signers file);
    without one it is ``unchecked``. *require_signature* fails the build unless the
    signature is ``ok``.
    """
    root = Path(build)
    raw = (root / MANIFEST).read_bytes() if (root / MANIFEST).is_file() else None
    payload = load(root)
    checks: list[Check] = []
    recorded = str(payload.get("id", ""))
    checks.append(_check_canonical(raw, payload))
    checks.append(_check_identity(root, payload, recorded))
    checks.extend(_check_index(root, payload))
    checks.append(_check_tree(root, payload))
    checks.append(_check_uki(root, payload))
    checks.append(_check_part(payload, "lock", "lockfile"))
    checks.append(_check_part(payload, "sbom", "SBOM"))
    checks.append(_check_part(payload, "packages", "package manifest"))
    checks.append(_check_part(payload, "summary", "mkosi summary"))
    checks.append(_check_measurements(payload))
    checks.append(_check_reproduction(root))
    signature, signer = _check_signature(root, raw, allowed_signers, runner)
    checks.append(signature)
    unchecked = tuple(str(item) for item in payload.get("unchecked", ()))
    return Verification(root, recorded, tuple(checks), require_signature, signer, unchecked)


def _check_canonical(raw: bytes | None, payload: Mapping[str, Any]) -> Check:
    if raw == canonical(payload):
        return Check("manifest", "ok", SCHEMA)
    return Check("manifest", "failed", "manifest.json is not in canonical form: it was edited")


def _check_identity(root: Path, payload: Mapping[str, Any], recorded: str) -> Check:
    derived = build_id(payload)
    if recorded != derived:
        return Check("id", "failed", f"manifest id {recorded} but its digest is {derived}")
    if root.resolve().name != recorded:
        return Check("id", "failed", f"directory {root.resolve().name} holds build {recorded}")
    return Check("id", "ok", recorded)


def _check_index(root: Path, payload: Mapping[str, Any]) -> list[Check]:
    index = payload.get("artifacts")
    if not isinstance(index, dict):
        return [Check("artifacts", "failed", "the manifest has no artifact index")]
    found = artifact_index(root)
    bad: list[str] = []
    for rel, want in sorted(index.items()):
        have = found.get(rel)
        if have is None:
            bad.append(f"{rel} is missing")
        elif "symlink" in want or "symlink" in have:
            if have.get("symlink") != want.get("symlink"):
                bad.append(f"{rel} points elsewhere")
        elif have["sha256"] != want.get("sha256"):
            bad.append(f"{rel} differs (sha256 {str(have['sha256'])[:12]})")
    extra = sorted(set(found) - set(index))
    bad.extend(f"{rel} is not in the manifest" for rel in extra)
    if bad:
        return [Check("artifacts", "failed", "; ".join(bad))]
    return [Check("artifacts", "ok", f"{len(index)} files match")]


def _check_tree(root: Path, payload: Mapping[str, Any]) -> Check:
    tree = payload.get("tree")
    if not isinstance(tree, dict) or "digest" not in tree:
        return Check("tree", "missing", "the manifest records no tree")
    if not (root / TREE).is_dir():
        return Check("tree", "failed", "tree/ is missing")
    digest, _ = tree_digest(root / TREE)
    if digest != tree["digest"]:
        return Check("tree", "failed", f"tree/ hashes to {digest[:19]}, not {tree['digest'][:19]}")
    return Check("tree", "ok", str(tree["digest"])[:19])


def _check_uki(root: Path, payload: Mapping[str, Any]) -> Check:
    uki = payload.get("uki")
    if not isinstance(uki, dict) or "sha256" not in uki:
        return Check("uki", "missing", str((uki or {}).get("reason", "no UKI recorded")))
    path = root / str(uki.get("path", UKI))
    if not path.is_file() or sha256_file(path) != uki["sha256"]:
        return Check("uki", "failed", f"{uki.get('path')} is not the recorded UKI")
    expected = payload.get("root_hash")
    if expected is None:
        return Check("uki", "ok", str(uki["sha256"])[:12])
    found = uki_root_hash(path)
    if found != expected:
        return Check("uki", "failed", f"the UKI's command line carries root hash {found}")
    return Check("uki", "ok", f"{str(uki['sha256'])[:12]}, root hash {str(expected)[:12]}")


def _check_part(payload: Mapping[str, Any], key: str, what: str) -> Check:
    part = payload.get(key)
    if not isinstance(part, dict):
        return Check(key, "missing", f"no {what} recorded")
    if part.get("state") == "missing":
        return Check(key, "missing", str(part.get("reason", "")))
    if key == "sbom" and part.get("bound") is False:
        return Check(key, "unchecked", "the SBOM is not bound to the build (incomplete)")
    return Check(key, "ok", str(part.get("path", "")))


def _check_measurements(payload: Mapping[str, Any]) -> Check:
    part = payload.get("measurements")
    if not isinstance(part, dict) or part.get("state") == "missing":
        reason = "" if not isinstance(part, dict) else str(part.get("reason", ""))
        return Check("measurements", "missing", reason)
    records = part.get("records")
    states = dict(records) if isinstance(records, dict) else {}
    unknown = sorted(name for name, state in states.items() if state != "predicted")
    if not states:
        return Check("measurements", "missing", "no measurement record")
    if unknown:
        return Check("measurements", "unchecked", f"no prediction for {', '.join(unknown)}")
    return Check("measurements", "ok", ", ".join(sorted(states)))


def _check_reproduction(root: Path) -> Check:
    try:
        observed = read_json(root / OBSERVATIONS, "observations.json")
    except StateError:
        return Check("reproduction", "unchecked", "no observations.json")
    record = observed.get("reproduction")
    if not isinstance(record, dict):
        return Check(
            "reproduction", "unchecked", "no independent rebuild recorded (build --reproduce)"
        )
    state = record.get("state")
    if state == "matched":
        return Check("reproduction", "ok", "an independent build matched every artifact")
    detail = ", ".join(str(item) for item in record.get("differ", ())) or str(
        record.get("error", "")
    )
    return Check("reproduction", "failed", f"{state}: {detail}".rstrip(": "))


def _check_signature(
    root: Path,
    raw: bytes | None,
    allowed: Path | None,
    runner: _ssh_sig.SigRunner | None,
) -> tuple[Check, str | None]:
    path = root / SIGNATURE
    if not path.is_file():
        return Check("signature", "missing", "no manifest.sig (build --sign KEY)"), None
    if allowed is None:
        fingerprint = _ssh_sig.signature_fingerprint(path.read_bytes()) or "unknown key"
        return Check("signature", "unchecked", f"by {fingerprint}; no --allowed-signers"), None
    result = _ssh_sig.verify(
        raw or b"", path.read_bytes(), allowed, namespace=NAMESPACE, runner=runner
    )
    if result.valid:
        return Check("signature", "ok", f"signed by {result.signer}"), result.signer
    return Check("signature", "failed", result.message), None


def uki_root_hash(uki: Path) -> str | None:
    """The ``roothash=`` (or ``usrhash=``) on the command line *uki* carries; ``None``
    without one, or when *uki* is no PE image."""
    from .measure import pe

    try:
        section = pe.parse(uki.read_bytes()).section(".cmdline") or b""
    except (TdxError, ValueError, OSError):
        return None
    words = section.split(b"\0", 1)[0].decode("utf-8", errors="replace").split()
    for word in words:
        for key in ("roothash=", "usrhash="):
            if word.startswith(key):
                return word.removeprefix(key)
    return None


def uki_cmdline(uki: Path) -> str | None:
    """The command line *uki* carries, ``None`` when it is no PE image."""
    from .measure import pe

    try:
        section = pe.parse(uki.read_bytes()).section(".cmdline") or b""
    except (TdxError, ValueError, OSError):
        return None
    return section.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def write_canonical(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_bytes(canonical(payload))


def records_of(measurements: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """The records of a ``measurements.json`` payload."""
    found = measurements.get("records")
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


__all__ = [
    "INPUTS",
    "MANIFEST",
    "MEASUREMENTS",
    "MEASUREMENTS_SCHEMA",
    "NAMESPACE",
    "OBSERVATIONS",
    "OBSERVATIONS_SCHEMA",
    "PACKAGES",
    "SBOM",
    "SCHEMA",
    "SIGNATURE",
    "TREE",
    "UKI",
    "Check",
    "Verification",
    "artifact_index",
    "build_id",
    "canonical",
    "file_record",
    "load",
    "missing",
    "records_of",
    "sha256_file",
    "sign",
    "tree_digest",
    "uki_cmdline",
    "uki_root_hash",
    "verify",
    "write_canonical",
]
