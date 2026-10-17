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

import copy
import hashlib
import html
import json
import os
import posixpath
import tarfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from string import Template
from typing import Any, Literal

from . import _ssh_sig
from .declarative.bom import parse_manifest, spdx_packages
from .declarative.lifecycle import MANIFEST_KEY, recorded_reproducibility, reproducibility_of
from .errors import TdxError, ValidationError
from .evidence import (
    BUNDLE_ROOT,
    EVIDENCE_REQUIREMENTS,
    EVIDENCE_SCHEMA,
    INDEX_NAME,
    SIGNATURE_NAME,
    TREE_SCHEMA,
    _requirements,
    _table,
    _verdict,
    attestation_entry,
    bake_simulated,
    coverage,
    lint_entry,
    policy_facts,
    tree_rows_digest,
)
from .measure.policy import read_policy
from .models import BAKE_RESULT_FILENAME

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
    max_age: float | timedelta | None = None,
) -> EvidenceVerification:
    """Check the evidence at *path* (a bundle, the directory, or its ``evidence.json``).

    Needs no recipe, checkout or bake directory. The index must carry the ``schema``
    this tundravm writes (:data:`~tundravm.evidence.EVIDENCE_SCHEMA`) and list every
    core member (``bake-result.json``, ``lint.json``, the lockfile when it names one,
    each SBOM, manifest, policy and ``tree.json``). Every fact the verdict uses that a
    member records (reproducibility, simulated, artifact sha256s, lint counts, the
    lockfile sha256, SBOM and policy facts) is derived from that member, an index
    that says otherwise is ``index-inconsistent``, and the verdict is recomputed
    from the derived facts.

    *build* is a build directory (or a
    release directory) holding the artifacts, laid out as the bake was
    (``<variant>/<file>``) or by target or file name, re-hashed against the index.
    *require* names checks from :data:`~tundravm.evidence.EVIDENCE_REQUIREMENTS`, as
    ``evidence --require``.
    Each bundled attestation record is re-derived offline
    (:func:`~tundravm.read_attestation`) against its variant's bundled policy: the
    quote parsed again, the nonce binding and register verdicts recomputed, the
    signature status read from the recorded validator reply (which is trusted as
    recorded: the quote's signature and collateral cannot be checked offline).
    ``attested`` judges freshness against the evidence's ``created`` with the
    ``max_age`` the evidence recorded, or *max_age* (seconds or a ``timedelta``).
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
    schema = _schema_check(index)
    if schema is not None:
        return EvidenceVerification(str(path), (schema,), index, required)
    checks.extend(_member_checks(files, listed, problems))
    checks.extend(_guarded("member", lambda: _core_checks(index, listed)))
    present = {
        name: files[name]
        for name in listed
        if name in files and _matches(files[name], listed[name])
    }
    derived: Mapping[str, Any] = index
    try:
        found, derived = _index_checks(index, present)
        checks.extend(found)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        message = f"the index does not have the shape evidence writes: {exc!r}"
        checks.append(_check("index", INDEX_NAME, "fail", message, "index-inconsistent"))
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
    checks.extend(_verdict_checks(index, derived, required, max_age))
    order = {kind: n for n, kind in enumerate(CHECK_KINDS)}
    ordered = sorted(checks, key=lambda c: order[c.kind])
    return EvidenceVerification(
        str(path), tuple(ordered), index, required, signer=signed_by, notes=tuple(notes)
    )


def _schema_check(index: Mapping[str, Any]) -> VerifyCheck | None:
    """A failed check unless ``schema`` is the one this tundravm reads, and the only one."""
    schema = index.get("schema")
    if schema != EVIDENCE_SCHEMA:
        message = (
            f"{INDEX_NAME} has schema {schema!r}; this tundravm reads {EVIDENCE_SCHEMA} "
            "(make the evidence again with this tundravm)"
        )
        return _check("index", "schema", "fail", message, "schema-unsupported")
    if "schema_version" in index:
        message = (
            f"{INDEX_NAME} carries schema_version {index['schema_version']!r} besides "
            f"schema {schema}: schema alone names the structure"
        )
        return _check("index", "schema_version", "fail", message, "index-inconsistent")
    return None


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


def core_members(index: Mapping[str, Any]) -> dict[str, str]:
    """Each member the evidence *index* cannot be checked without, by name, with the field
    that requires it: ``bake-result.json`` and ``lint.json`` always, the lockfile when
    ``lockfile`` names one, and each SBOM, manifest, policy and ``tree.json`` a variant names."""
    found = {BAKE_RESULT_FILENAME: "the bake record", "lint.json": "lint.member"}
    lockfile = index.get("lockfile")
    if lockfile is not None:
        found[str(lockfile["member"])] = "lockfile.member"
    for name, record in sorted(_mapping(index.get("variants")).items()):
        for entry in record.get("sbom") or []:
            target = entry.get("target")
            found[str(entry["member"])] = f"variants.{name}.sbom.{target}.member"
            if entry.get("manifest_member"):
                found[str(entry["manifest_member"])] = (
                    f"variants.{name}.sbom.{target}.manifest_member"
                )
        policy = record.get("policy")
        if policy is not None:
            found[str(policy["member"])] = f"variants.{name}.policy.member"
        for n, entry in enumerate(record.get("attestations") or []):
            found[str(entry["member"])] = f"variants.{name}.attestations.{n}.member"
        if record.get("tree"):
            found[str(record["tree"])] = f"variants.{name}.tree"
    return found


def _core_checks(index: Mapping[str, Any], listed: Mapping[str, Any]) -> list[VerifyCheck]:
    """A ``core-member-missing`` fail for each core member the index does not list."""
    return [
        _check(
            "member",
            name,
            "fail",
            f"required by {field}, but the index does not list it: the facts it records "
            "cannot be checked",
            "core-member-missing",
        )
        for name, field in sorted(core_members(index).items())
        if name not in listed
    ]


def _inconsistent(subject: str, message: str) -> VerifyCheck:
    return _check("index", subject, "fail", message, "index-inconsistent")


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _shown(value: object) -> str:
    """*value* as a check message shows it: a sha256 by its first 12 digits, else as JSON."""
    if isinstance(value, str) and len(value) == 64:
        return value[:12]
    return json.dumps(value, sort_keys=True)


def _facts(
    subject: str, asserted: Mapping[str, Any], actual: Mapping[str, Any], source: str
) -> list[VerifyCheck]:
    """One pass check when *asserted* holds each of *actual*'s facts, else an
    ``index-inconsistent`` fail per field (``subject.field``) that disagrees with *source*."""
    wrong = [key for key in actual if asserted.get(key) != actual[key]]
    if not wrong:
        message = f"{', '.join(actual)} as {source} records"
        return [_check("index", subject, "pass", message)]
    return [
        _check(
            "index",
            f"{subject}.{key}",
            "fail",
            f"the index says {_shown(asserted.get(key))}, {source} gives {_shown(actual[key])}",
            "index-inconsistent",
        )
        for key in wrong
    ]


def _index_checks(
    index: Mapping[str, Any], present: Mapping[str, bytes]
) -> tuple[list[VerifyCheck], dict[str, Any]]:
    """The index against its own (hash-verified) members, and the index with every fact the
    verdict uses replaced by the one its member records; a member that failed is not read."""
    checks: list[VerifyCheck] = []
    derived: dict[str, Any] = copy.deepcopy(dict(index))
    rows = _json(present.get("lint.json"))
    if isinstance(rows, list):
        lint = lint_entry([row for row in rows if isinstance(row, dict)])
        facts = {key: lint[key] for key in ("errors", "warnings", "infos", "codes")}
        checks.extend(_facts("lint", index["lint"], facts, "lint.json"))
        derived["lint"] = {**index["lint"], **facts}
    bake = _json(present.get(BAKE_RESULT_FILENAME))
    if not isinstance(bake, dict):
        return checks, derived
    extra = _mapping(bake.get(MANIFEST_KEY))
    recorded = recorded_reproducibility(extra)
    simulated = bake_simulated(bake)
    facts = {"backend": bake.get("backend"), "simulated": simulated, "reproducibility": recorded}
    checks.extend(_facts("bake", index["bake"], facts, BAKE_RESULT_FILENAME))
    derived["bake"] = {**index["bake"], **facts}
    baked_digest = extra.get("recipe_digest") or None
    checks.extend(
        _facts("recipe", index["recipe"], {"baked_digest": baked_digest}, BAKE_RESULT_FILENAME)
    )
    derived["recipe"] = {**index["recipe"], "baked_digest": baked_digest}
    lock = _lock_facts(index, present, bake)
    if lock is not None:
        checks.extend(_facts("lockfile", index["lockfile"], lock, "the lockfile member"))
        derived["lockfile"] = {**index["lockfile"], **lock}
    baked = _mapping(bake.get("variants"))
    for name, record in sorted(_mapping(index.get("variants")).items()):
        entry = baked.get(name)
        if not isinstance(entry, dict):
            message = f"the index lists variant {name}, which {BAKE_RESULT_FILENAME} lacks"
            checks.append(_inconsistent(f"variants.{name}", message))
            continue
        own = _mapping(entry.get(MANIFEST_KEY)) or extra  # schema 2: one record for the bake
        reproduced = recorded_reproducibility(own)
        made = bool(own.get("simulated", simulated))
        checks.extend(_variant_facts(name, record, entry, own, reproduced, made, derived))
        artifacts = {
            row.get("target"): row for row in record.get("artifacts", []) if isinstance(row, dict)
        }
        lock_bound = derived["lockfile"]["bound"] if derived.get("lockfile") else True
        manifests = _mapping(entry.get("manifest_digests"))
        for n, sbom_entry in enumerate(record.get("sbom", [])):
            checks.extend(_sbom_check(sbom_entry, artifacts, present))
            bound = _sbom_facts(sbom_entry, manifests, present, lock_bound=lock_bound)
            if bound is not None:
                subject = f"variants.{name}.sbom.{sbom_entry.get('target')}"
                source = (
                    "the bundled manifest"
                    if sbom_entry.get("manifest_member")
                    else f"{BAKE_RESULT_FILENAME} (no manifest bundled)"
                )
                checks.extend(_facts(subject, sbom_entry, bound, source))
                derived["variants"][name]["sbom"][n] = {**sbom_entry, **bound}
            checks.extend(_sbom_packages(sbom_entry, present))
        measured = {
            row.get("target"): row
            for row in derived["variants"][name].get("artifacts", [])
            if isinstance(row, dict)
        }
        checks.extend(_policy_check(name, record, measured, present, derived))
        checks.extend(_attestation_checks(name, record, present, derived))
        covered = coverage(list(measured.values()), derived["variants"][name].get("policy"))
        source = "the policy and bake-result.json"
        checks.extend(_facts(f"variants.{name}", record, {"coverage": covered}, source))
        derived["variants"][name]["coverage"] = covered
        checks.extend(_tree_check(name, record, index["variants"], present, derived))
    return checks, derived


def _variant_facts(
    name: str,
    record: Mapping[str, Any],
    entry: Mapping[str, Any],
    extra: Mapping[str, Any],
    recorded: Mapping[str, object] | None,
    simulated: bool,
    derived: dict[str, Any],
) -> list[VerifyCheck]:
    """*name*'s reproducibility, tree digest and artifacts as ``bake-result.json`` records them."""
    subject = f"variants.{name}"
    facts = {
        "reproducibility": reproducibility_of(recorded, name),
        "tree_digest": extra.get("tree_digest") or None,
    }
    checks = _facts(subject, record, facts, BAKE_RESULT_FILENAME)
    out = derived["variants"][name]
    out.update(facts)
    rows = [(n, row) for n, row in enumerate(record.get("artifacts", [])) if isinstance(row, dict)]
    digests = _mapping(entry.get("artifact_digests"))
    payloads = _mapping(entry.get("boot_payloads"))
    targets = sorted(str(target) for target in _mapping(entry.get("artifacts")))
    listed = sorted(str(row.get("target")) for _, row in rows)
    if listed != targets:
        message = (
            f"the index lists artifacts {', '.join(listed) or '(none)'}, "
            f"{BAKE_RESULT_FILENAME} records {', '.join(targets) or '(none)'}"
        )
        checks.append(_inconsistent(f"{subject}.artifacts", message))
    for n, row in rows:
        target = str(row.get("target"))
        actual = {
            "sha256": digests.get(target),
            "simulated": simulated,
            "boot_payload": _boot_payload(payloads.get(target), row.get("boot_payload")),
        }
        checks.extend(_facts(f"{subject}.artifacts.{target}", row, actual, BAKE_RESULT_FILENAME))
        out["artifacts"][n] = {**row, **actual}
    return checks


def _boot_payload(recorded: object, listed: object) -> dict[str, str] | None:
    """The boot payload ``bake-result.json`` records for an artifact, its path as the index
    shows it (the bundle leaves the path relative to another machine's build directory)."""
    if not isinstance(recorded, dict) or not recorded.get("sha256"):
        return None
    shown = _mapping(listed).get("path") or recorded.get("path")
    return {"path": str(shown), "sha256": str(recorded["sha256"])}


def _lock_facts(
    index: Mapping[str, Any], present: Mapping[str, bytes], bake: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The lockfile's sha256, the one the bake recorded and whether they agree, or ``None``
    when the index names no lockfile or its member is not readable."""
    lockfile = index.get("lockfile")
    if not isinstance(lockfile, dict):
        return None
    data = present.get(str(lockfile.get("member")))
    if data is None:
        return None
    actual = _sha256(data)
    recorded = bake.get("lock_sha256")
    return {"sha256": actual, "recorded_sha256": recorded, "bound": actual == recorded}


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


def _sbom_check(
    entry: Mapping[str, Any],
    artifacts: Mapping[Any, Mapping[str, Any]],
    present: Mapping[str, bytes],
) -> list[VerifyCheck]:
    """The SBOM member describes the artifact sha256 the index lists for its target."""
    member = str(entry.get("member"))
    document = _json(present.get(member))
    if document is None:
        return []
    row = artifacts.get(entry.get("target")) or {}
    described = _spdx_artifact(document)
    if described == row.get("sha256"):
        return [_check("index", member, "pass", f"describes artifact sha256 {_shown(described)}")]
    listed = _shown(row.get("sha256"))
    message = f"describes artifact sha256 {_shown(described)}, the index lists {listed}"
    return [_inconsistent(member, message)]


def _sbom_facts(
    entry: Mapping[str, Any],
    manifests: Mapping[str, Any],
    present: Mapping[str, bytes],
    *,
    lock_bound: bool,
) -> dict[str, Any] | None:
    """Whether the SBOM is ``complete`` (its manifest is bundled), what the bake recorded that
    is ``missing`` and whether it is ``bound`` (the bundled manifest is the one
    ``bake-result.json`` records, and the lockfile is bound); ``None`` when the manifest
    member is listed but not readable (its member check says why)."""
    member = entry.get("manifest_member")
    data = present.get(str(member)) if member else None
    if member and data is None:
        return None
    recorded = manifests.get(str(entry.get("target")))
    missing = ["manifest"] if recorded is not None and data is None else []
    matches = data is None if recorded is None else data is not None and _sha256(data) == recorded
    return {"complete": data is not None, "missing": missing, "bound": matches and lock_bound}


def _sbom_packages(entry: Mapping[str, Any], present: Mapping[str, bytes]) -> list[VerifyCheck]:
    """The SBOM's packages against the ones its bundled manifest lists, name and version."""
    member = str(entry.get("member"))
    document = _json(present.get(member))
    manifest = _json(present.get(str(entry.get("manifest_member"))))
    if not isinstance(document, dict) or manifest is None:
        return []
    listed = spdx_packages(document)
    try:
        _, packages = parse_manifest(manifest, str(entry.get("manifest_member")))
    except TdxError as exc:
        return [_inconsistent(member, f"its bundled manifest does not parse: {exc}")]
    installed = {package.name: package.version for package in packages}
    if listed == installed:
        return [_check("index", member, "pass", f"{len(listed)} packages, as its manifest lists")]
    differ = sorted(
        name for name in {*listed, *installed} if listed.get(name) != installed.get(name)
    )
    shown = ", ".join(differ[:5]) + (", ..." if len(differ) > 5 else "")
    message = f"its packages are not the ones its bundled manifest lists: {shown}"
    return [_inconsistent(member, message)]


def _policy_check(
    name: str,
    record: Mapping[str, Any],
    rows: Mapping[Any, Mapping[str, Any]],
    present: Mapping[str, bytes],
    derived: dict[str, Any],
) -> list[VerifyCheck]:
    policy = record.get("policy")
    if not isinstance(policy, dict):
        return []
    member = str(policy.get("member"))
    raw = _json(present.get(member))
    if raw is None and member not in present:
        return []
    subject = f"variants.{name}.policy"
    try:
        payload = read_policy(raw if isinstance(raw, dict) else {})
    except TdxError:
        facts: dict[str, Any] = {"valid": False}
    else:
        facts = {"valid": True, **policy_facts(payload, list(rows.values()))}
    derived["variants"][name]["policy"] = {**policy, **facts}
    return _facts(subject, policy, facts, member)


def _attestation_checks(
    name: str, record: Mapping[str, Any], present: Mapping[str, bytes], derived: dict[str, Any]
) -> list[VerifyCheck]:
    """Each bundled attestation record re-derived offline against the variant's bundled
    policy, and bound to an artifact the variant holds; a member that failed is not read."""
    entries = record.get("attestations")
    if not isinstance(entries, list):
        return []
    policy = derived["variants"][name].get("policy")
    valid = isinstance(policy, dict) and policy.get("valid")
    bundled = present.get(str(policy.get("member"))) if valid and policy else None
    held = {
        row.get("sha256")
        for row in derived["variants"][name].get("artifacts", [])
        if isinstance(row, dict)
    }
    checks: list[VerifyCheck] = []
    for n, entry in enumerate(entries):
        member = str(entry.get("member"))
        data = present.get(member)
        if data is None:
            continue
        facts = {k: v for k, v in attestation_entry(member, data, bundled).items() if k != "member"}
        checks.extend(_facts(f"variants.{name}.attestations.{n}", entry, facts, member))
        derived["variants"][name]["attestations"][n] = {**entry, **facts}
        if facts["artifact_sha256"] not in held:
            message = (
                f"attests artifact sha256 {_shown(facts['artifact_sha256'])}, which variant "
                f"{name} does not hold"
            )
            checks.append(_inconsistent(member, message))
    return checks


def _tree_check(
    name: str,
    record: Mapping[str, Any],
    variants: Mapping[str, Any],
    present: Mapping[str, bytes],
    derived: dict[str, Any],
) -> list[VerifyCheck]:
    member = record.get("tree")
    if not member:
        derived["variants"][name]["tree_matches"] = None
        if record.get("tree_matches") is None:
            return []
        message = (
            f"the index says {_shown(record.get('tree_matches'))}, but names no tree.json "
            "to derive it from"
        )
        subject = f"variants.{name}.tree_matches"
        return [_check("index", subject, "fail", message, "index-inconsistent")]
    manifest = _json(present.get(str(member)))
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
    baked = derived["variants"][name].get("tree_digest")
    matches = None if not baked else baked in (own, whole)  # whole: a schema 2 bake's
    derived["variants"][name]["tree_matches"] = matches
    checks = _facts(f"variants.{name}", record, {"tree_matches": matches}, str(member))
    if problems:
        checks.append(
            _check("index", str(member), "fail", "; ".join(problems), "index-inconsistent")
        )
    else:
        message = f"{len(rows)} entries hash to its variant digest {str(own)[:12]}"
        checks.append(_check("index", str(member), "pass", message))
    return checks


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


def _verdict_checks(
    index: Mapping[str, Any],
    derived: Mapping[str, Any],
    required: Sequence[str],
    max_age: float | timedelta | None = None,
) -> list[VerifyCheck]:
    """The stored verdict against one recomputed from the facts *derived* from the members
    (the index's own where no member records them), then the verdict those facts give
    under the recorded and the given requirements (and *max_age*, when given)."""
    stored = index.get("verdict")
    recorded_required = stored.get("required", []) if isinstance(stored, dict) else []
    judged = derived
    if max_age is not None:
        seconds = max_age.total_seconds() if isinstance(max_age, timedelta) else max_age
        judged = {**derived, "attestation": {"max_age_seconds": int(seconds)}}
    try:
        recomputed = _verdict(derived, tuple(recorded_required), [])
        wanted = _requirements(tuple(dict.fromkeys([*recorded_required, *required])))
        demanded = _verdict(judged, wanted, [])
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        message = f"the recorded facts do not give a verdict: {exc!r}"
        return [_check("verdict", None, "fail", message, "index-inconsistent")]
    checks: list[VerifyCheck] = []
    if recomputed != stored:
        message = "the recorded verdict disagrees with the facts its members give: " + (
            _verdict_diff(stored if isinstance(stored, dict) else {}, recomputed)
        )
        checks.append(_check("verdict", "recorded", "fail", message, "index-inconsistent"))
    else:
        message = f"the recorded verdict {recomputed['overall']} follows from the members' facts"
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
