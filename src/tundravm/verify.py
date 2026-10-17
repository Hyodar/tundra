"""Verify an evidence bundle or directory on its own: no recipe, no checkout, no bake directory.

:func:`verify_evidence` reads what ``tundravm evidence`` wrote (the ``.tar.gz``
bundle or the directory) and checks it from the inside out: every member against
the sha256 the ``evidence.json`` index lists, the index against its members (the
lockfile against the ``lock_sha256`` the bake recorded, each SBOM and policy
against the artifact it names, each ``tree.json`` against its digests, the
recorded verdict recomputed from the recorded facts), artifacts handed in against
the index, the signature against an allowed-signers file, and the requirements of
``evidence --require``. The answer is an :class:`EvidenceVerification`: one
:class:`VerifyCheck` per check, each ``pass``, ``fail`` or ``missing``.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import tarfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Literal

from . import _ssh_sig
from .errors import TdxError, ValidationError
from .evidence import (
    BUNDLE_ROOT,
    EVIDENCE_REQUIREMENTS,
    INDEX_NAME,
    SIGNATURE_NAME,
    TREE_SCHEMA,
    _requirements,
    _table,
    _verdict,
    tree_rows_digest,
)
from .measure.policy import is_placeholder, read_policy

VERIFY_SCHEMA = "tundravm.verify/1"
"""The ``schema`` key of :meth:`EvidenceVerification.to_dict` (``verify --format json``)."""
VerifyState = Literal["pass", "fail", "missing"]
VerifyFormat = Literal["text", "json"]
VERIFY_FORMATS: tuple[VerifyFormat, ...] = ("text", "json")
CHECK_KINDS = ("member", "index", "artifact", "signature", "verdict")
"""The kinds of :class:`VerifyCheck`, in the order they are reported."""
_EVIDENCE_HINT = (
    "Pass the bundle `tundravm evidence --bundle` wrote, or its BUILD/evidence directory."
)
MEMBER_LIMIT = 256 * 1024 * 1024
"""The largest member a bundle may hold (a guard against decompression bombs)."""


@dataclass(frozen=True, slots=True)
class VerifyCheck:
    """One check: its ``kind`` (:data:`CHECK_KINDS`), ``subject``, ``state`` and, unless it
    passed, a stable reason ``code``; ``message`` says what was compared."""

    kind: str
    subject: str | None
    state: VerifyState
    message: str
    code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "state": self.state,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class EvidenceVerification:
    """What :func:`verify_evidence` found; :attr:`passed` only when every check passed."""

    path: str
    checks: tuple[VerifyCheck, ...]
    index: Mapping[str, Any] = field(repr=False)
    required: tuple[str, ...] = ()
    signer: str | None = None
    """The principal whose signature verified, when one did."""
    notes: tuple[Mapping[str, str], ...] = ()
    """``{"code", "message"}`` each: what was not checked (``unsigned``, ``signature-unchecked``,
    ``artifacts-unchecked``) and why."""

    @property
    def verdict(self) -> str:
        """``fail`` when a check failed, ``incomplete`` when one is missing, else ``pass``."""
        states = {check.state for check in self.checks}
        return "fail" if "fail" in states else "incomplete" if "missing" in states else "pass"

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def problems(self) -> tuple[VerifyCheck, ...]:
        """The checks that did not pass."""
        return tuple(check for check in self.checks if check.state != "pass")

    def counts(self) -> dict[str, int]:
        found = {"pass": 0, "fail": 0, "missing": 0}
        for check in self.checks:
            found[check.state] += 1
        return found

    def to_dict(self) -> dict[str, object]:
        """What ``verify --format json`` prints; ``schema`` is ``tundravm.verify/1``."""
        recorded = self.index.get("verdict") if isinstance(self.index, Mapping) else None
        recipe = self.index.get("recipe") if isinstance(self.index, Mapping) else None
        return {
            "schema": VERIFY_SCHEMA,
            "evidence": self.path,
            "verdict": self.verdict,
            "passed": self.passed,
            "counts": self.counts(),
            "required": list(self.required),
            "signer": self.signer,
            "recorded": {
                "created": self.index.get("created") if isinstance(self.index, Mapping) else None,
                "recipe": recipe.get("name") if isinstance(recipe, Mapping) else None,
                "verdict": recorded.get("overall") if isinstance(recorded, Mapping) else None,
            },
            "checks": [check.to_dict() for check in self.checks],
            "notes": [dict(note) for note in self.notes],
        }

    def render(self, format: VerifyFormat = "text") -> str:
        """``json`` (:meth:`to_dict`) or ``text``: a line per check kind, then every problem."""
        if format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        return _text(self)

    def html(self) -> str:
        """One self-contained HTML page: a verdict banner, the problems, then every check."""
        return _html(self)


# ── reading ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Read:
    files: dict[str, bytes]
    problems: tuple[str, ...]


def read_evidence(path: Path) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """The files of the evidence at *path* by relative name, and what was not a plain file.

    *path* is a bundle (``tar.gz``, members under ``evidence/``), the directory
    ``evidence`` wrote, or its ``evidence.json``. Nothing is extracted to disk and
    no link is followed. ``ValidationError`` when *path* is none of those.
    """
    found = _read(Path(path))
    if INDEX_NAME not in found.files:
        raise ValidationError(
            f"{path} holds no {INDEX_NAME}: it is not evidence.",
            hint=_EVIDENCE_HINT,
            context={"path": str(path)},
        )
    return found.files, found.problems


def _read(path: Path) -> _Read:
    if path.is_file() and path.name == INDEX_NAME:
        path = path.parent
    if path.is_dir():
        return _read_directory(path)
    if not path.is_file():
        raise ValidationError(
            f"Evidence {path} does not exist.",
            hint=_EVIDENCE_HINT,
            context={"path": str(path)},
        )
    try:
        return _read_bundle(path)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValidationError(
            f"Evidence {path} is not a readable tar.gz bundle: {exc}.",
            hint=_EVIDENCE_HINT,
            context={"path": str(path)},
        ) from exc


def _read_bundle(path: Path) -> _Read:
    files: dict[str, bytes] = {}
    problems: list[str] = []
    with tarfile.open(path, mode="r:*") as archive:
        for member in archive:
            name = posixpath.normpath(member.name)
            root, _, rest = name.partition("/")
            if member.isdir() and (name == BUNDLE_ROOT or root == BUNDLE_ROOT):
                continue
            if root != BUNDLE_ROOT or not rest or rest.startswith("../") or name.startswith("/"):
                problems.append(f"{member.name} is not under {BUNDLE_ROOT}/")
                continue
            if not member.isfile():
                problems.append(f"{rest} is not a plain file in the bundle")
                continue
            if member.size > MEMBER_LIMIT:
                raise ValidationError(
                    f"Bundle member {rest} is {member.size} bytes, over the {MEMBER_LIMIT} limit.",
                    hint="Evidence members are small JSON files; this is not tundravm evidence.",
                    context={"path": str(path)},
                )
            handle = archive.extractfile(member)
            if handle is None:
                problems.append(f"{rest} cannot be read from the bundle")
                continue
            if rest in files:
                problems.append(f"{rest} appears twice in the bundle")
            files[rest] = handle.read()
    return _Read(files, tuple(problems))


def _read_directory(root: Path) -> _Read:
    files: dict[str, bytes] = {}
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            if (base / name).is_symlink():
                dirnames.remove(name)
                problems.append(f"{(base / name).relative_to(root).as_posix()} is a link")
        dirnames.sort()
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                problems.append(f"{rel} is not a plain file")
                continue
            files[rel] = path.read_bytes()
    return _Read(files, tuple(problems))


# ── checking ─────────────────────────────────────────────────────────────


def verify_evidence(
    path: Path,
    *,
    build: Path | None = None,
    require: Sequence[str] = (),
    allowed_signers: Path | None = None,
    signer: str | None = None,
) -> EvidenceVerification:
    """Check the evidence at *path* (a bundle, the directory, or its ``evidence.json``).

    Needs no recipe, checkout or bake directory. *build* is a build directory (or a
    release directory) holding the artifacts, laid out as the bake was
    (``<variant>/<file>``) or by target or file name, re-hashed against the index.
    *require* names checks from :data:`~tundravm.evidence.EVIDENCE_REQUIREMENTS`, as
    ``evidence --require``.
    *allowed_signers* (an OpenSSH allowed-signers file) checks ``evidence.json.sig``
    with ``ssh-keygen -Y verify``; *signer* is the principal, else the file's
    principals for the signing key are tried. Raises ``ValidationError`` for a path
    that is not evidence or an unknown requirement, ``StateError`` when
    *allowed_signers* is given and ``ssh-keygen`` is missing; everything else is a check.
    """
    required = _requirements(require)
    files, problems = read_evidence(Path(path))
    checks: list[VerifyCheck] = []
    notes: list[dict[str, str]] = []
    raw = files[INDEX_NAME]
    try:
        index: Any = json.loads(raw)
        if not isinstance(index, dict) or not isinstance(index.get("members"), dict):
            raise ValueError("no members mapping")
        listed: dict[str, Any] = index["members"]
    except ValueError as exc:
        checks.append(
            _check(
                "index",
                INDEX_NAME,
                "fail",
                f"{INDEX_NAME} is not an index: {exc}",
                "index-inconsistent",
            )
        )
        return EvidenceVerification(str(path), tuple(checks), {}, required)
    checks.extend(_member_checks(files, listed, problems))
    present = {
        name: files[name]
        for name in listed
        if name in files and _matches(files[name], listed[name])
    }
    checks.extend(_guarded("index", lambda: _index_checks(index, present)))
    signature, signed_by = _signature_check(raw, files.get(SIGNATURE_NAME), allowed_signers, signer)
    if signature is not None:
        checks.append(signature)
    elif SIGNATURE_NAME in files:
        message = f"{SIGNATURE_NAME} is present but not checked: pass --allowed-signers FILE"
        notes.append({"code": "signature-unchecked", "message": message})
    else:
        notes.append({"code": "unsigned", "message": f"the evidence carries no {SIGNATURE_NAME}"})
    if build is not None:
        directory = Path(build)
        checks.extend(_guarded("artifact", lambda: _artifact_checks(index, present, directory)))
    else:
        message = "artifacts were not re-hashed: pass --build DIR"
        notes.append({"code": "artifacts-unchecked", "message": message})
    checks.extend(_verdict_checks(index, required))
    order = {kind: n for n, kind in enumerate(CHECK_KINDS)}
    ordered = sorted(checks, key=lambda c: order[c.kind])
    return EvidenceVerification(
        str(path), tuple(ordered), index, required, signer=signed_by, notes=tuple(notes)
    )


def _guarded(kind: str, run: Callable[[], list[VerifyCheck]]) -> list[VerifyCheck]:
    """*run*'s checks; an index whose shape they cannot read is one ``index-inconsistent`` fail."""
    try:
        return run()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        message = f"the index does not have the shape evidence writes: {exc!r}"
        return [_check(kind, INDEX_NAME, "fail", message, "index-inconsistent")]


def _check(
    kind: str, subject: str | None, state: VerifyState, message: str, code: str | None = None
) -> VerifyCheck:
    return VerifyCheck(kind=kind, subject=subject, state=state, message=message, code=code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _matches(data: bytes, entry: object) -> bool:
    return isinstance(entry, Mapping) and entry.get("sha256") == _sha256(data)


def _member_checks(
    files: Mapping[str, bytes], listed: Mapping[str, Any], problems: Iterable[str]
) -> list[VerifyCheck]:
    checks: list[VerifyCheck] = []
    for name in sorted(listed):
        entry = listed[name]
        expected = entry.get("sha256") if isinstance(entry, Mapping) else None
        if name not in files:
            checks.append(
                _check(
                    "member",
                    name,
                    "missing",
                    "listed in the index, not in the evidence",
                    "member-missing",
                )
            )
            continue
        actual = _sha256(files[name])
        if actual != expected:
            message = f"sha256 {actual[:12]}, the index lists {str(expected)[:12]}"
            checks.append(_check("member", name, "fail", message, "member-hash-mismatch"))
        elif entry.get("size") != len(files[name]):
            message = f"{len(files[name])} bytes, the index lists {entry.get('size')}"
            checks.append(_check("member", name, "fail", message, "member-hash-mismatch"))
        else:
            checks.append(_check("member", name, "pass", f"sha256 {actual[:12]}"))
    unlisted = sorted(set(files) - set(listed) - {INDEX_NAME, SIGNATURE_NAME})
    for name in unlisted:
        checks.append(
            _check(
                "member",
                name,
                "fail",
                "in the evidence, not listed in the index",
                "index-inconsistent",
            )
        )
    for problem in problems:
        checks.append(_check("member", None, "fail", problem, "index-inconsistent"))
    return checks


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _index_checks(index: Mapping[str, Any], present: Mapping[str, bytes]) -> list[VerifyCheck]:
    """The index against its own (hash-verified) members; a member that failed is not read."""
    checks: list[VerifyCheck] = []

    def agree(subject: str, ok: bool, message: str, problem: str) -> None:
        if ok:
            checks.append(_check("index", subject, "pass", message))
        else:
            checks.append(_check("index", subject, "fail", problem, "index-inconsistent"))

    bake = _json(present.get("bake-result.json"))
    if not isinstance(bake, dict):
        return checks
    checks.extend(_lock_check(index, present, bake))
    variants = _mapping(index.get("variants"))
    baked = _mapping(bake.get("variants"))
    for name, record in sorted(variants.items()):
        rows = {
            row.get("target"): row for row in record.get("artifacts", []) if isinstance(row, dict)
        }
        recorded = (baked.get(name) or {}).get("artifact_digests") or {}
        for target, row in sorted(rows.items()):
            subject = f"{name}/{target}"
            agree(
                subject,
                recorded.get(target) == row.get("sha256"),
                f"artifact sha256 {str(row.get('sha256'))[:12]} is bake-result.json's",
                f"the index lists artifact sha256 {str(row.get('sha256'))[:12]}, bake-result.json "
                f"records {str(recorded.get(target))[:12]}",
            )
        for entry in record.get("sbom", []):
            document = _json(present.get(str(entry.get("member"))))
            if document is None:
                continue
            row = rows.get(entry.get("target")) or {}
            described = _spdx_artifact(document)
            agree(
                str(entry.get("member")),
                described == row.get("sha256"),
                f"describes artifact sha256 {str(described)[:12]}",
                f"describes artifact sha256 {str(described)[:12]}, the index lists "
                f"{str(row.get('sha256'))[:12]}",
            )
        checks.extend(_policy_check(name, record, rows, present))
        checks.extend(_tree_check(name, record, variants, present))
    return checks


def _lock_check(
    index: Mapping[str, Any], present: Mapping[str, bytes], bake: Mapping[str, Any]
) -> list[VerifyCheck]:
    lockfile = index.get("lockfile")
    if not isinstance(lockfile, dict):
        return []
    data = present.get(str(lockfile.get("member")))
    if data is None:
        return []
    actual = _sha256(data)
    recorded = bake.get("lock_sha256")
    consistent = (
        lockfile.get("sha256") == actual
        and lockfile.get("recorded_sha256") == recorded
        and lockfile.get("bound") is (actual == recorded)
    )
    if consistent and actual == recorded:
        message = f"sha256 {actual[:12]} is the lock_sha256 bake-result.json records"
        return [_check("index", "tundravm.lock", "pass", message)]
    if consistent:
        message = f"sha256 {actual[:12]} is not the bake's {str(recorded)[:12]}, as the index says"
        return [_check("index", "tundravm.lock", "pass", message)]
    message = (
        f"the lockfile hashes to {actual[:12]} and bake-result.json records "
        f"{str(recorded)[:12]}, but the index says sha256 {str(lockfile.get('sha256'))[:12]}, "
        f"bound {lockfile.get('bound')}"
    )
    return [_check("index", "tundravm.lock", "fail", message, "index-inconsistent")]


def _spdx_artifact(document: Any) -> str | None:
    """The SHA256 checksum of the image package an SPDX document describes."""
    if not isinstance(document, dict):
        return None
    described = {
        r.get("relatedSpdxElement")
        for r in document.get("relationships", [])
        if isinstance(r, dict) and r.get("relationshipType") == "DESCRIBES"
    }
    for package in document.get("packages", []):
        if isinstance(package, dict) and package.get("SPDXID") in described:
            for checksum in package.get("checksums", []):
                if isinstance(checksum, dict) and checksum.get("algorithm") == "SHA256":
                    return str(checksum.get("checksumValue"))
    return None


def _policy_check(
    name: str,
    record: Mapping[str, Any],
    rows: Mapping[Any, Mapping[str, Any]],
    present: Mapping[str, bytes],
) -> list[VerifyCheck]:
    policy = record.get("policy")
    if not isinstance(policy, dict):
        return []
    member = str(policy.get("member"))
    raw = _json(present.get(member))
    if raw is None and member not in present:
        return []
    try:
        payload = read_policy(raw if isinstance(raw, dict) else {})
    except TdxError:
        ok = policy.get("valid") is False
        message = "not a valid policy, as the index says"
        problem = "not a valid policy, but the index says it is"
        return [_policy_result(member, ok, message, problem)]
    measured = payload.get("artifact")
    sha = measured.get("sha256") if isinstance(measured, dict) else None
    shas = {row.get("sha256") for row in rows.values()}
    matches = (sha in shas) if sha else None
    registers = sorted(dict(payload.get("registers") or {}))  # type: ignore[call-overload]
    ok = (
        policy.get("valid") is True
        and policy.get("artifact_matches") == matches
        and policy.get("placeholder") == is_placeholder(payload)
        and policy.get("registers") == registers
    )
    message = f"measured artifact sha256 {str(sha)[:12]}" + (
        ", the variant's" if matches else ", not the variant's (as the index says)"
    )
    problem = (
        f"measured artifact sha256 {str(sha)[:12]} (matches: {matches}), registers "
        f"{', '.join(registers)}; the index says matches {policy.get('artifact_matches')}, "
        f"registers {', '.join(policy.get('registers') or [])}"
    )
    return [_policy_result(member, ok, message, problem)]


def _policy_result(member: str, ok: bool, message: str, problem: str) -> VerifyCheck:
    if ok:
        return _check("index", member, "pass", message)
    return _check("index", member, "fail", problem, "index-inconsistent")


def _tree_check(
    name: str,
    record: Mapping[str, Any],
    variants: Mapping[str, Any],
    present: Mapping[str, bytes],
) -> list[VerifyCheck]:
    member = f"variants/{name}/tree.json"
    manifest = _json(present.get(member))
    if not isinstance(manifest, dict) or manifest.get("schema") != TREE_SCHEMA:
        return []
    rows = manifest.get("files") or []
    try:
        own = tree_rows_digest((name, row) for row in rows)
    except (KeyError, TypeError, ValueError):
        own = None
    problems: list[str] = []
    if own != manifest.get("variant_digest"):
        problems.append(
            f"its rows hash to {str(own)[:12]}, it says {str(manifest.get('variant_digest'))[:12]}"
        )
    whole = manifest.get("tree_digest")
    matches = record.get("tree_matches")
    if matches is not None and (whole == record.get("tree_digest")) != matches:
        problems.append(
            f"its tree digest {str(whole)[:12]} and the baked "
            f"{str(record.get('tree_digest'))[:12]} disagree with tree_matches {matches}"
        )
    compiled = [str(v) for v in manifest.get("compiled_variants") or []]
    together = [_json(present.get(f"variants/{v}/tree.json")) for v in compiled]
    if compiled and all(isinstance(m, dict) for m in together):
        pairs = sorted(
            (
                (v, row)
                for v, m in zip(compiled, together, strict=True)
                for row in m.get("files") or []
            ),
            key=lambda pair: f"{pair[0]}/{pair[1].get('path')}",
        )
        try:
            union = tree_rows_digest(pairs)
        except (KeyError, TypeError, ValueError):
            union = None
        if union != whole:
            problems.append(f"the variants' rows hash to {str(union)[:12]}, not its tree digest")
    if problems:
        return [_check("index", member, "fail", "; ".join(problems), "index-inconsistent")]
    message = f"{len(rows)} entries hash to its variant digest {str(own)[:12]}"
    return [_check("index", member, "pass", message)]


def _signature_check(
    data: bytes, signature: bytes | None, allowed: Path | None, signer: str | None
) -> tuple[VerifyCheck | None, str | None]:
    if allowed is None:
        return None, None
    if signature is None:
        message = f"no {SIGNATURE_NAME} in the evidence (sign it with `evidence --sign KEY`)"
        return _check("signature", SIGNATURE_NAME, "missing", message, "signature-missing"), None
    result = _ssh_sig.verify(data, signature, allowed, signer=signer)
    if result.valid:
        return _check("signature", SIGNATURE_NAME, "pass", result.message), result.signer
    message = f"does not verify against {allowed}: {result.message}"
    return _check("signature", SIGNATURE_NAME, "fail", message, "signature-invalid"), None


def _artifact_checks(
    index: Mapping[str, Any], present: Mapping[str, bytes], directory: Path
) -> list[VerifyCheck]:
    bake = _json(present.get("bake-result.json"))
    baked = bake.get("variants", {}) if isinstance(bake, dict) else {}
    variants = _mapping(index.get("variants"))
    rows = [
        (name, row)
        for name, record in sorted(variants.items())
        for row in record.get("artifacts", [])
        if isinstance(row, dict)
    ]
    filenames = [Path(str(row.get("path"))).name for _, row in rows]
    checks: list[VerifyCheck] = []
    for name, row in rows:
        target = str(row.get("target"))
        subject = f"{name}/{target}"
        relative = ((baked.get(name) or {}).get("artifacts") or {}).get(target)
        filename = Path(str(row.get("path"))).name
        candidates = [
            *(
                [directory / str(relative)]
                if isinstance(relative, str) and not Path(relative).is_absolute()
                else []
            ),
            directory / name / target / filename,
            directory / name / filename,
            *([directory / filename] if filenames.count(filename) == 1 else []),
        ]
        found = next((c for c in candidates if c.is_file()), None)
        if found is None:
            message = f"no {filename} for {subject} under {directory}"
            checks.append(_check("artifact", subject, "missing", message, "artifact-missing"))
            continue
        actual = _file_sha256(found)
        if actual == row.get("sha256"):
            checks.append(_check("artifact", subject, "pass", f"{found} sha256 {actual[:12]}"))
        else:
            message = (
                f"{found} hashes to {actual[:12]}, the index lists {str(row.get('sha256'))[:12]}"
            )
            checks.append(_check("artifact", subject, "fail", message, "artifact-hash-mismatch"))
    return checks


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verdict_checks(index: Mapping[str, Any], required: Sequence[str]) -> list[VerifyCheck]:
    """The stored verdict against one recomputed from the recorded facts, then the verdict
    those facts give under the recorded and the given requirements."""
    stored = index.get("verdict")
    recorded_required = stored.get("required", []) if isinstance(stored, dict) else []
    try:
        recomputed = _verdict(index, tuple(recorded_required), [])
        wanted = _requirements(tuple(dict.fromkeys([*recorded_required, *required])))
        demanded = _verdict(index, wanted, [])
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        message = f"the recorded facts do not give a verdict: {exc!r}"
        return [_check("verdict", None, "fail", message, "index-inconsistent")]
    checks: list[VerifyCheck] = []
    if recomputed != stored:
        message = "the recorded verdict disagrees with the recorded facts: " + _verdict_diff(
            stored if isinstance(stored, dict) else {}, recomputed
        )
        checks.append(_check("verdict", "recorded", "fail", message, "index-inconsistent"))
    else:
        message = f"the recorded verdict {recomputed['overall']} follows from the recorded facts"
        checks.append(_check("verdict", "recorded", "pass", message))
    for reason in demanded["reasons"]:
        state: VerifyState = "fail" if reason["severity"] == "fail" else "missing"
        checks.append(
            _check("verdict", reason["subject"], state, reason["message"], reason["code"])
        )
    if not demanded["reasons"]:
        shown = ", ".join(wanted) or "none"
        checks.append(_check("verdict", None, "pass", f"pass (required: {shown})"))
    return checks


def _verdict_diff(stored: Mapping[str, Any], recomputed: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted({*stored, *recomputed}):
        if stored.get(key) == recomputed.get(key):
            continue
        if key == "reasons":
            mine = {r.get("code") for r in stored.get(key) or [] if isinstance(r, Mapping)}
            theirs = {r.get("code") for r in recomputed.get(key) or []}
            shown = ", ".join(sorted(str(c) for c in mine ^ theirs)) or "their text"
            parts.append(f"reasons differ ({shown})")
        else:
            parts.append(f"{key} {stored.get(key)} (facts: {recomputed.get(key)})")
    return "; ".join(parts)


# ── rendering ────────────────────────────────────────────────────────────


def _text(result: EvidenceVerification) -> str:
    counts = result.counts()
    lines = [
        f"verify {result.path}  verdict {result.verdict}  "
        f"({counts['pass']} pass, {counts['fail']} fail, {counts['missing']} missing)"
    ]
    index = result.index
    stored = index.get("verdict") if isinstance(index, Mapping) else None
    recipe = index.get("recipe") if isinstance(index, Mapping) else None
    if isinstance(stored, Mapping):
        name = recipe.get("name") if isinstance(recipe, Mapping) else None
        lines.append(
            f"  evidence   {index.get('created')}  recipe {name or '-'}  "
            f"recorded verdict {stored.get('overall')}"
        )
    for kind in CHECK_KINDS:
        found = [c for c in result.checks if c.kind == kind]
        if not found:
            continue
        good = sum(c.state == "pass" for c in found)
        summary = f"{good}/{len(found)} pass"
        if kind == "signature" and result.signer is not None:
            summary += f", signed by {result.signer}"
        lines.append(f"  {kind:<10} {summary}")
    if result.required:
        lines.append(f"  required   {', '.join(result.required)}")
    if result.problems:
        lines.append("problems:")
        lines.extend(
            f"  {c.state:<8} {c.code}  {c.subject or '-'}: {c.message}" for c in result.problems
        )
    lines.extend(f"note: {note['code']}: {note['message']}" for note in result.notes)
    return "\n".join(lines) + "\n"


_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verification: $title</title>
<style>
body { font: 14px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 64rem;
  padding: 1rem; color: #1b1f24; background: #fff; }
h1, h2 { line-height: 1.25; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; display: block;
  overflow-x: auto; }
th, td { border: 1px solid #d0d7de; padding: .3rem .5rem; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
code { font-family: ui-monospace, monospace; font-size: 12px; word-break: break-all; }
.banner { display: flex; flex-wrap: wrap; gap: .5rem; padding: .75rem; border-radius: 6px; }
.banner span { padding: .2rem .6rem; border-radius: 4px; background: #fff; }
.pass { background: #dafbe1; border: 1px solid #1a7f37; }
.fail { background: #ffebe9; border: 1px solid #cf222e; }
.incomplete { background: #fff8c5; border: 1px solid #9a6700; }
</style>
</head>
<body>
<h1>Verification: $title</h1>
<div class="banner $verdict" id="verdict"><strong>verdict: $verdict</strong>$counts</div>
<h2>Problems</h2>
$problems
<h2>Checks</h2>
$checks
<h2>Notes</h2>
$notes
</body>
</html>
"""
)


def _html(result: EvidenceVerification) -> str:
    def rows(checks: Iterable[VerifyCheck]) -> list[tuple[object, ...]]:
        return [(c.kind, c.subject, c.state, c.code, c.message) for c in checks]

    header = ("kind", "subject", "state", "code", "message")
    counts = "".join(
        f"<span>{html.escape(state)}: {count}</span>" for state, count in result.counts().items()
    )
    if result.signer is not None:
        counts += f"<span>signed by {html.escape(result.signer)}</span>"
    notes = "".join(
        f"<li><code>{html.escape(n['code'])}</code> {html.escape(n['message'])}</li>"
        for n in result.notes
    )
    return _PAGE.substitute(
        title=html.escape(result.path),
        verdict=html.escape(result.verdict),
        counts=counts,
        problems=_table(header, rows(result.problems)) if result.problems else "<p>none</p>",
        checks=_table(header, rows(result.checks)),
        notes=f"<ul>{notes}</ul>" if notes else "<p>none</p>",
    )


__all__ = [
    "CHECK_KINDS",
    "EVIDENCE_REQUIREMENTS",
    "VERIFY_FORMATS",
    "VERIFY_SCHEMA",
    "EvidenceVerification",
    "VerifyCheck",
    "read_evidence",
    "verify_evidence",
]
