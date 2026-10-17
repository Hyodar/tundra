"""Auditor-facing evidence of a bake: what was built, from what, and whether it still holds.

:func:`evidence` reads a bake output directory (``bake-result.json``) and, for
each selected baked variant, gathers the recipe and tree digests, the lockfile
(verbatim, with its version and a drift verdict), every artifact with a fresh
integrity check, the reproducibility verdict ``bake --verify-reproducible``
recorded, the measurements policy when one exists, an SPDX SBOM, the lint
summary, a provenance summary and the tool versions. The result is an
:class:`Evidence`: an ``evidence.json`` index holding the sha256 of every
member, which :meth:`Evidence.write` lays out as a directory,
:meth:`Evidence.bundle` packs as a deterministic ``tar.gz`` and
:meth:`Evidence.html` renders as one self-contained page.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import os
import platform
import tarfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from typing import Any, Literal

from . import _ssh_sig
from .attestation import parse_time, read_attestation
from .backends.base import Requirement
from .declarative._lowered import Lowered
from .declarative.bom import manifest_path, sbom
from .declarative.lifecycle import (
    INPROCESS,
    MANIFEST_KEY,
    NO_LOCK,
    Artifact,
    Lock,
    ProbeRunner,
    Tree,
    _digest_mode,
    check_report,
    compile_image,
    image_lock_status,
    lock_status,
    probe,
    read_artifacts,
    read_lock,
    recorded_reproducibility,
    reproducibility_of,
    run_probe,
    verify_artifact,
)
from .declarative.lower import lower
from .declarative.model import Recipe
from .declarative.resolve import provenance
from .errors import ArtifactError, StateError, TdxError, ValidationError
from .lockfile import lock_variants
from .measure.policy import is_placeholder, read_policy
from .measure.rtmr import MEASURABLE_SUFFIXES, measurable
from .models import BAKE_RESULT_FILENAME

INDEX_NAME = "evidence.json"
SIGNATURE_NAME = "evidence.json.sig"
"""The SSH signature of ``evidence.json`` (``ssh-keygen -Y sign -n tundravm-evidence``)."""
EVIDENCE_SCHEMA = "tundravm.evidence/3"
"""The ``schema`` of ``evidence.json``, which ``evidence --format json`` prints verbatim: the one
identifier of the index's structure, which ``verify`` reads and refuses any other of."""
TREE_SCHEMA = "tundravm.tree/1"
"""The ``schema`` of each ``variants/<variant>/tree.json`` member."""
BUNDLE_ROOT = "evidence"
"""The top-level directory every member of a bundle sits under."""
EvidenceFormat = Literal["text", "json"]
EVIDENCE_FORMATS: tuple[EvidenceFormat, ...] = ("text", "json")
EvidenceRequirement = Literal["reproducible", "measured", "real", "lock", "installed", "attested"]
EVIDENCE_REQUIREMENTS: tuple[EvidenceRequirement, ...] = (
    "reproducible",
    "measured",
    "real",
    "lock",
    "installed",
    "attested",
)
"""What ``require=`` (``evidence --require``) can demand; a missing one makes the verdict
``incomplete`` instead of a note: a recorded ``matched`` second build, a valid measurements
policy bound to each artifact, real (not simulated) artifacts, a lockfile, an SBOM with
the installed inventory (mkosi's manifest) for each artifact, and for each artifact a
policy measures a ``trusted``, fresh attestation record checked against that policy."""
DEFAULT_MAX_AGE = 24 * 3600
"""How far (seconds) an attestation record's ``observed_at`` may be from the evidence's
``created`` for ``attested``: a day."""
_MKOSI = Requirement(
    tool="mkosi",
    probe=("mkosi", "--version"),
    hint="Install mkosi to record its version.",
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """The evidence of one bake: ``index`` is ``evidence.json``, ``members`` the files it lists.

    ``members`` maps each relative path to its bytes; ``index["members"]`` holds
    each one's ``sha256`` and ``size``. ``notes`` say what could not be included.
    """

    index: Mapping[str, Any]
    members: Mapping[str, bytes] = field(repr=False)
    notes: tuple[str, ...] = ()
    signature: bytes | None = field(default=None, repr=False)
    """``evidence.json.sig``, set by :meth:`sign`; ``None`` when unsigned."""

    @property
    def verdict(self) -> str:
        """``pass``, ``fail`` (a recorded contradiction) or ``incomplete`` (a required check
        is missing); ``index["verdict"]["reasons"]`` says why."""
        return str(self.index["verdict"]["overall"])

    @property
    def reasons(self) -> tuple[Mapping[str, Any], ...]:
        """Why the verdict is not ``pass``: ``{"severity", "code", "subject", "message"}`` each."""
        return tuple(self.index["verdict"]["reasons"])

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def index_json(self) -> str:
        """``evidence.json``: sorted, indented JSON with a trailing newline."""
        return json.dumps(self.index, indent=2, sort_keys=True) + "\n"

    def files(self) -> dict[str, bytes]:
        """Every file of the evidence, ``evidence.json`` included, by sorted relative path."""
        found = {**self.members, INDEX_NAME: self.index_json().encode()}
        if self.signature is not None:
            found[SIGNATURE_NAME] = self.signature
        return dict(sorted(found.items()))

    def sign(self, key: Path) -> Evidence:
        """This evidence with ``evidence.json.sig``: *key*, an SSH private key, signs the exact
        bytes of ``evidence.json`` with ``ssh-keygen -Y sign -n tundravm-evidence``.

        The index holds every member's sha256, so the signature covers them all;
        :meth:`write` and :meth:`bundle` include it. ``StateError`` when ``ssh-keygen``
        is not on PATH, ``ValidationError`` when it cannot sign with *key*.
        """
        signature = _ssh_sig.sign(self.index_json().encode(), Path(key))
        return replace(self, signature=signature)

    def write(self, directory: Path) -> Path:
        """Write every file under *directory* (created; files of a previous run replaced, the
        members its index lists that this evidence does not hold removed, as is its
        ``evidence.json.sig`` when this evidence is unsigned)."""
        root = Path(directory)
        files = self.files()
        for name in _previous_members(root):
            if name not in files:
                (root / name).unlink(missing_ok=True)
                _prune(root, (root / name).parent)
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if self.signature is None:
            (root / SIGNATURE_NAME).unlink(missing_ok=True)
        return root

    def bundle(self, path: Path) -> Path:
        """Write a deterministic ``tar.gz`` of every file to *path*.

        Members are sorted under ``evidence/``, owned by 0:0 with mode 0644, and
        every mtime (the gzip header's too) is ``SOURCE_DATE_EPOCH``, else 0.
        """
        mtime = _epoch() or 0
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for name, data in self.files().items():
                info = tarfile.TarInfo(f"{BUNDLE_ROOT}/{name}")
                info.size = len(data)
                info.mtime = mtime
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=mtime) as packed:
                packed.write(raw.getvalue())
        return target

    def html(self) -> str:
        """One self-contained HTML page: a verdict banner, then a section per variant."""
        return render_html(self.index)

    def render(self, format: EvidenceFormat = "text") -> str:
        """The index as ``json`` (``evidence.json``) or ``text`` (a summary)."""
        if format == "json":
            return self.index_json()
        return _text(self.index)


def evidence(
    lowered_or_recipe: Recipe | Lowered,
    *,
    build: Path,
    variants: Sequence[str] | None = None,
    lock: Lock | Path | None = None,
    policy: Path | None = None,
    recipe_path: Path | None = None,
    runner: ProbeRunner | None = None,
    require: Sequence[str] = (),
    allow_unbound: bool = False,
    attestations: Sequence[str | Path] = (),
    max_age: float | timedelta = DEFAULT_MAX_AGE,
) -> Evidence:
    """The evidence of the bake in the build directory *build* for *variants* (default: all baked).

    *lock* (a :class:`Lock` or a lockfile path) defaults to the lockfile the bake
    recorded, else ``build/tundravm.lock``. *policy* is a ``measure --export-policy``
    file used for every selected variant instead of ``build/<variant>/policy.json``.
    *recipe_path* records the recipe file and its sha256. *runner* replaces the
    ``mkosi --version`` probe (tests). Nothing is written; see :meth:`Evidence.write`.

    The verdict is ``fail`` when something recorded contradicts the bake (an
    artifact changed or is gone, the recipe or tree changed, the lockfile
    drifted, lint errors, a ``different`` second build, a placeholder or
    mismatched policy). *require* names checks from :data:`EVIDENCE_REQUIREMENTS`
    whose absence makes it ``incomplete``; checks not required that are missing
    are notes.

    The lockfile read must hash to the sha256 ``bake-result.json`` records, and each
    SBOM is bound the same way (:func:`~tundravm.declarative.bom.sbom`), else
    ``ArtifactError``; *allow_unbound* includes them with a note and makes the
    verdict ``incomplete``.

    *attestations* are ``tundravm.attestation/1`` records (``attest --record``), each
    bundled verbatim as ``variants/<variant>/attestation-<n>.json`` of the variant
    holding the artifact its policy measured (``ValidationError`` when no selected
    variant holds it) and re-derived against that variant's policy
    (:func:`~tundravm.read_attestation`). ``attested`` is satisfied when every artifact a
    valid policy measures has a record whose re-derived verdict is ``trusted``, whose
    policy is that policy (its sha256) and whose ``observed_at`` is within *max_age*
    (seconds, or a ``timedelta``; default a day) of the evidence's ``created``. A
    record that disagrees with its own document fails the evidence
    (``attestation-inconsistent``).
    """
    required = _requirements(require)
    window = int(max_age.total_seconds() if isinstance(max_age, timedelta) else max_age)
    if window <= 0:
        raise ValidationError(
            f"max_age must be a positive duration, got {max_age!r}.",
            hint="Pass --max-age as a duration such as 24h, 30m or 7d.",
        )
    recipe = lowered_or_recipe if isinstance(lowered_or_recipe, Recipe) else None
    img = lower(recipe) if recipe is not None else lowered_or_recipe
    assert isinstance(img, Lowered)
    base = Path(build)
    manifest = base / BAKE_RESULT_FILENAME
    artifacts = read_artifacts(base)
    extra = _manifest_extra(manifest)
    names = _selected(artifacts, variants, base)
    notes: list[str] = []
    members: dict[str, bytes] = {"bake-result.json": manifest.read_bytes()}

    locked, lock_bytes, lock_from = _lock(lock, extra, base)
    recorded_lock = _manifest_field(manifest, "lock_sha256")
    lock_entry = _lock_entry(recipe, img, locked, lock_bytes, lock_from, names, notes)
    if lock_entry is not None:
        lock_entry["recorded_sha256"] = recorded_lock
        lock_entry["bound"] = lock_entry["sha256"] == recorded_lock
        if not lock_entry["bound"]:
            problem = (
                f"the lockfile {lock_from or '(given)'} hashes to {lock_entry['sha256'][:12]}, "
                + (
                    "but the bake read no lockfile"
                    if recorded_lock is None
                    else f"the bake recorded {str(recorded_lock)[:12]}"
                )
            )
            if not allow_unbound:
                raise ArtifactError(
                    f"Evidence would not describe the bake: {problem}.",
                    hint=(
                        "Pass the lockfile the bake used (--lockfile), or bake again. "
                        "--allow-unbound (allow_unbound=True) records it anyway and makes "
                        "the verdict incomplete."
                    ),
                    context={"out": str(base)},
                )
            notes.append(f"unbound: {problem}")
    if lock_bytes is not None:
        members["tundravm.lock"] = lock_bytes

    diagnostics = check_report(
        recipe,
        None if recipe is not None else img,
        variants=names,
        lock=NO_LOCK if locked is None else locked,
    )
    lint_rows = [d.to_dict() for d in diagnostics]
    members["lint.json"] = _json_bytes(lint_rows)
    compiled = _compiled_tree(img, artifacts, locked)
    tree = None if compiled is None else compiled.digest
    digests = _recipe_digests(img, names, locked)
    reproduced = recorded_reproducibility(extra)
    records = {
        name: _variant(
            name,
            img,
            [a for a in artifacts if a.variant == name],
            base=base,
            recipe=recipe,
            locked=locked,
            policy=policy,
            tree=tree,
            reproducibility=reproducibility_of(reproduced, name),
            members=members,
            notes=notes,
            allow_unbound=allow_unbound,
        )
        for name in names
    }
    for name in names:
        records[name]["tree"] = None
        if compiled is not None and name in compiled.variants:
            records[name]["tree"] = f"variants/{name}/tree.json"
            members[f"variants/{name}/tree.json"] = _tree_manifest(name, compiled)
        else:
            notes.append(
                f"{name} tree.json: left out, the recipe does not compile the bake's variants"
            )
    _bundle_attestations(attestations, records, members)
    if recipe is None:
        notes.append("no recipe given (a lowered recipe): the provenance summary is left out")
    index: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "created": _created(),
        "recipe": _recipe_entry(recipe, img, recipe_path, digests, artifacts, names),
        "bake": {
            "path": str(manifest),
            "backend": _manifest_field(manifest, "backend"),
            "simulated": bake_simulated(_manifest_payload(manifest)),
            "reproducibility": reproduced,
        },
        "lockfile": lock_entry,
        "lint": lint_entry(lint_rows),
        "tools": _tools(runner, notes),
        "attestation": {"max_age_seconds": window},
        "variants": records,
    }
    index["verdict"] = _verdict(index, required, notes)
    index["notes"] = list(dict.fromkeys(notes))
    index["members"] = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in sorted(members.items())
    }
    return Evidence(index=index, members=dict(sorted(members.items())), notes=tuple(index["notes"]))


# ── gathering ────────────────────────────────────────────────────────────


def _requirements(names: Sequence[str]) -> tuple[EvidenceRequirement, ...]:
    unknown = [name for name in names if name not in EVIDENCE_REQUIREMENTS]
    if unknown:
        raise ValidationError(
            f"Unknown evidence requirement(s): {', '.join(unknown)}.",
            hint=f"Require any of: {', '.join(EVIDENCE_REQUIREMENTS)}.",
        )
    return tuple(name for name in EVIDENCE_REQUIREMENTS if name in names)


def _epoch() -> int | None:
    value = os.environ.get("SOURCE_DATE_EPOCH", "")
    return int(value) if value.isdigit() else None


def _created() -> str:
    epoch = _epoch()
    moment = datetime.fromtimestamp(epoch, UTC) if epoch is not None else datetime.now(UTC)
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _manifest_payload(manifest: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _manifest_extra(manifest: Path) -> dict[str, Any]:
    extra = _manifest_payload(manifest).get(MANIFEST_KEY)
    return extra if isinstance(extra, dict) else {}


def _manifest_field(manifest: Path, key: str) -> object:
    return _manifest_payload(manifest).get(key)


def _selected(
    artifacts: Sequence[Artifact], variants: Sequence[str] | None, out: Path
) -> list[str]:
    baked = sorted({artifact.variant for artifact in artifacts})
    if variants is None:
        if not baked:
            raise StateError(
                f"No baked artifact in {out}.",
                hint="Run `tundravm bake RECIPE` first; evidence describes a bake.",
            )
        return baked
    missing = [name for name in variants if name not in baked]
    if missing:
        raise StateError(
            f"No baked artifact for variant(s) {', '.join(missing)} in {out}.",
            hint=f"Baked: {', '.join(baked) or '(none)'}. Bake those variants first.",
        )
    return sorted(dict.fromkeys(variants))  # the index's order, which verify recomputes in


def _lock(
    given: Lock | Path | None, extra: Mapping[str, Any], out: Path
) -> tuple[Lock | None, bytes | None, str | None]:
    """The lockfile, its bytes as written and where it came from."""
    if isinstance(given, Lock):
        return given, given.text().encode(), None
    candidates: list[Path] = []
    if given is not None:
        candidates.append(Path(given))
    else:
        recorded = extra.get("lockfile")
        if isinstance(recorded, str) and recorded:
            candidates.append(Path(recorded))
        candidates.append(out / "tundravm.lock")
    for candidate in candidates:
        if candidate.is_file() or given is not None:
            return read_lock(candidate), candidate.read_bytes(), str(candidate)
    return None, None, None


def _lock_entry(
    recipe: Recipe | None,
    img: Lowered,
    locked: Lock | None,
    data: bytes | None,
    source: str | None,
    names: Sequence[str],
    notes: list[str],
) -> dict[str, Any] | None:
    if locked is None or data is None:
        notes.append("no lockfile: the bake was not frozen, or its lockfile is gone")
        return None
    partial = set(names) != set(img.state.profiles)
    try:
        drift = (
            lock_status(recipe, locked, variants=names)
            if recipe is not None
            else image_lock_status(img, locked, names, partial=partial)
        )
    except TdxError as exc:
        notes.append(f"lockfile drift could not be checked: {exc}")
        drift = ()
    return {
        "member": "tundravm.lock",
        "source": source,
        "version": locked.lockfile.version,
        "compiler_version": locked.compiler_version,
        "recipe_digest": locked.recipe_digest,
        "sha256": hashlib.sha256(data).hexdigest(),
        "drift": [{"code": d.code, "subject": d.subject, "message": d.message} for d in drift],
        "verdict": "drifted" if drift else "current",
    }


def _compiled_tree(img: Lowered, artifacts: Sequence[Artifact], locked: Lock | None) -> Tree | None:
    """The tree of every baked variant as the recipe compiles now, or ``None`` when it cannot."""
    baked = tuple(sorted({artifact.variant for artifact in artifacts}))
    if not baked or not set(baked) <= set(img.state.profiles):
        return None
    try:
        return compile_image(img, baked, locked=locked)
    except TdxError:
        return None


def tree_rows(tree: Tree, variant: str) -> list[dict[str, Any]]:
    """*variant*'s entries of *tree*, relative to its directory: ``path``, ``type`` (``file``,
    ``symlink``, ``directory``), ``mode`` (as the tree digest sees it, octal) and ``sha256``
    and ``size`` for a file, ``target`` for a symlink."""
    prefix = f"{variant}/"
    rows: list[dict[str, Any]] = []
    for entry in tree.entries:
        if not entry.path.startswith(prefix):
            continue
        row: dict[str, Any] = {
            "path": entry.path[len(prefix) :],
            "mode": f"{_digest_mode(entry):04o}",
        }
        if entry.symlink is not None:
            row.update(type="symlink", target=entry.symlink)
        elif entry.content is None:
            row.update(type="directory")
        else:
            row.update(
                type="file",
                size=len(entry.content),
                sha256=hashlib.sha256(entry.content).hexdigest(),
            )
        rows.append(row)
    return rows


def tree_rows_digest(rows: Iterable[tuple[str, Mapping[str, Any]]]) -> str:
    """The :attr:`Tree.digest <tundravm.Tree>` of ``(variant, row)`` pairs from :func:`tree_rows`.

    Rows must come in tree order (sorted by ``variant/path``), as ``tree.json`` holds them.
    """
    digest = hashlib.sha256()
    for variant, row in rows:
        mode = int(str(row["mode"]), 8)
        digest.update(f"{variant}/{row['path']}".encode() + b"\0" + f"{mode:o}".encode() + b"\0")
        if row["type"] == "symlink":
            digest.update(b"L" + str(row["target"]).encode())
        elif row["type"] == "file":
            digest.update(b"F" + bytes.fromhex(str(row["sha256"])))
        else:
            digest.update(b"D")
        digest.update(b"\n")
    return digest.hexdigest()


def _tree_manifest(variant: str, tree: Tree) -> bytes:
    """``variants/<variant>/tree.json``: every file the variant's compiled tree emits.

    ``tree_digest`` is the digest of the whole tree compiled for ``compiled_variants``
    (what a bake of them records); ``variant_digest`` covers this variant's rows alone.
    """
    rows = tree_rows(tree, variant)
    return _json_bytes(
        {
            "schema": TREE_SCHEMA,
            "variant": variant,
            "compiled_variants": list(tree.variants),
            "tree_digest": tree.digest,
            "variant_digest": tree_rows_digest((variant, row) for row in rows),
            "files": rows,
        }
    )


def _recipe_digests(img: Lowered, names: Sequence[str], locked: Lock | None) -> set[str]:
    declared = set(img.state.profiles)
    scopes = {tuple(sorted(declared)), tuple(sorted(names))}
    if locked is not None and set(lock_variants(locked.lockfile)) <= declared:
        scopes.add(tuple(lock_variants(locked.lockfile)))
    return {img.select(scope).digest() for scope in scopes}


def _recipe_entry(
    recipe: Recipe | None,
    img: Lowered,
    path: Path | None,
    digests: set[str],
    artifacts: Sequence[Artifact],
    names: Sequence[str],
) -> dict[str, Any]:
    recorded = sorted(
        {a.recipe_digest for a in artifacts if a.variant in names and a.recipe_digest}
    )
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
    return {
        "name": None if recipe is None else recipe.name,
        "path": None if path is None else str(path),
        "file_sha256": file_sha,
        "digest": img.select(tuple(sorted(img.state.profiles))).digest(),
        "baked_digest": recorded[0] if len(recorded) == 1 else (recorded or None),
        "matches_bake": None if not recorded else all(d in digests for d in recorded),
    }


def _integrity(artifact: Artifact) -> str:
    try:
        verify_artifact(artifact)
    except ArtifactError as exc:
        return "mismatch" if "actual" in exc.context else "missing"
    return "verified"


def _previous_members(root: Path) -> list[str]:
    """The members the ``evidence.json`` already in *root* lists, each a plain relative path
    inside *root*; none when there is no readable index."""
    try:
        listed = json.loads((root / INDEX_NAME).read_text(encoding="utf-8")).get("members")
    except (OSError, ValueError, AttributeError):
        return []
    names = [str(name) for name in listed] if isinstance(listed, dict) else []
    return [
        name
        for name in names
        if not Path(name).is_absolute() and ".." not in Path(name).parts and name != INDEX_NAME
    ]


def _prune(root: Path, directory: Path) -> None:
    """Remove *directory* and its parents up to *root* while they are empty."""
    while directory != root and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


def declared_packages(img: Lowered, variant: str) -> list[str]:
    """The packages *img* declares for *variant*, sorted: ``variants.<variant>.declared_packages``
    of the index, which ``changes`` compares with the recipe's later."""
    return sorted(str(name) for name in img.state.effective_profile(variant).packages)


def _variant(
    name: str,
    img: Lowered,
    artifacts: Sequence[Artifact],
    *,
    base: Path,
    recipe: Recipe | None,
    locked: Lock | None,
    policy: Path | None,
    tree: str | None,
    reproducibility: str | None,
    members: dict[str, bytes],
    notes: list[str],
    allow_unbound: bool,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    sboms: list[dict[str, Any]] = []
    for artifact in artifacts:
        size = artifact.path.stat().st_size if artifact.path.is_file() else None
        integrity = _integrity(artifact)
        rows.append(
            {
                "target": artifact.target,
                "path": str(artifact.path),
                "size": size,
                "sha256": artifact.sha256,
                "integrity": integrity,
                "simulated": artifact.simulated,
            }
        )
        if integrity != "verified":
            notes.append(f"{name}/{artifact.target} sbom: left out, the artifact is {integrity}")
            continue
        document = sbom(artifact, lock=locked, allow_unbound=allow_unbound)
        member = f"variants/{name}/sbom-{artifact.target}.spdx.json"
        members[member] = document.render("spdx-json").encode()
        notes.extend(f"{name}/{artifact.target} sbom: {note}" for note in document.notes)
        manifest: str | None = None
        if document.manifest_found:
            manifest = f"variants/{name}/manifest-{artifact.target}.json"
            members[manifest] = manifest_path(artifact).read_bytes()
        sboms.append(
            {
                "target": artifact.target,
                "member": member,
                "manifest_member": manifest,
                "packages": len(document.packages),
                "sources": len(document.sources),
                "complete": document.complete,
                "missing": list(document.missing),
                "bound": document.bound,
            }
        )
    recorded_tree = next((a.tree_digest for a in artifacts if a.tree_digest), "")
    measurements = _policy(name, artifacts, base, policy, members, notes)
    return {
        "artifacts": rows,
        "tree_digest": recorded_tree or None,
        "tree_matches": None if not recorded_tree or tree is None else recorded_tree == tree,
        "reproducibility": reproducibility,
        "policy": measurements,
        "coverage": coverage(rows, measurements),
        "declared_packages": declared_packages(img, name),
        "sbom": sboms,
        "provenance": None if recipe is None else _provenance(recipe, name),
    }


def _policy(
    name: str,
    artifacts: Sequence[Artifact],
    base: Path,
    given: Path | None,
    members: dict[str, bytes],
    notes: list[str],
) -> dict[str, Any] | None:
    source = given if given is not None else base / name / "policy.json"
    if not source.is_file():
        notes.append(
            f"no measurements policy for variant {name} ({source} does not exist); write one "
            f"with `tundravm measure --build {base} --variant {name} --export-policy {source}`"
        )
        return None
    data = source.read_bytes()
    member = f"variants/{name}/policy.json"
    members[member] = data
    try:
        payload = read_policy(source)
    except TdxError as exc:
        notes.append(f"measurements policy {source} is not a valid policy: {exc}")
        return {"member": member, "source": str(source), "valid": False}
    measured = payload.get("artifact")
    sha = measured.get("sha256") if isinstance(measured, dict) else None
    return {
        "member": member,
        "source": str(source),
        "valid": True,
        "placeholder": is_placeholder(payload),
        "tool": payload.get("tool"),
        "registers": sorted(dict(payload.get("registers") or {})),  # type: ignore[call-overload]
        "artifact_matches": sha in {a.sha256 for a in artifacts} if sha else None,
        "measures": {
            "target": next((a.target for a in artifacts if sha and a.sha256 == sha), None),
            "sha256": sha,
        },
    }


def coverage(
    rows: Sequence[Mapping[str, Any]], policy: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Per artifact row of a variant, whether its measurements *policy* measures it: ``target``,
    ``sha256``, ``state`` (``measured`` or ``unmeasured``) and the ``reason`` it is not (a
    format no tool reads, no usable policy, or the policy measures another artifact).

    An artifact counts as measured only when the policy names its own sha256: the bake
    does not record which UKI a disk image holds, so a disk is not covered by the
    measurement of the UKI inside it.
    """
    usable = policy is not None and policy.get("valid") and not policy.get("placeholder")
    measures: Mapping[str, Any] = (policy or {}).get("measures") or {}
    found: list[dict[str, Any]] = []
    for row in rows:
        entry = {"target": row["target"], "sha256": row["sha256"]}
        if usable and measures.get("sha256") == row["sha256"]:
            found.append({**entry, "state": "measured", "reason": None})
            continue
        suffix = Path(str(row["path"])).suffix
        if not measurable(str(row["path"])):
            readable = ", ".join(sorted(MEASURABLE_SUFFIXES))
            reason = (
                f"its format ({suffix or 'no suffix'}) is not measurable: tools read {readable}"
            )
        elif policy is None:
            reason = "no measurements policy"
        elif not usable:
            reason = "the policy is invalid or a placeholder"
        elif measures.get("target"):
            reason = f"the policy measures {measures['target']}"
        else:
            reason = "the policy measures another artifact"
        found.append({**entry, "state": "unmeasured", "reason": reason})
    return found


def _bundle_attestations(
    paths: Sequence[str | Path], records: dict[str, dict[str, Any]], members: dict[str, bytes]
) -> None:
    """Bundle each record of *paths* into the variant holding the artifact it attests."""
    counts: Counter[str] = Counter()
    for record in records.values():
        record["attestations"] = []
    for path in paths:
        source = Path(path)
        found = read_attestation(source)
        sha = found.artifact_sha256
        name = next(
            (
                variant
                for variant, record in records.items()
                if sha and sha in {row["sha256"] for row in record["artifacts"]}
            ),
            None,
        )
        if name is None:
            raise ValidationError(
                f"Attestation record {source} attests artifact sha256 {str(sha)[:12]}, which no "
                "selected variant of this bake holds.",
                hint="Attest the image this bake built, with its policy (`tundravm attest "
                "--policy BUILD/<variant>/policy.json --record FILE`), or select the variant "
                "it belongs to (--variant).",
                context={"attestation": str(source)},
            )
        counts[name] += 1
        member = f"variants/{name}/attestation-{counts[name]}.json"
        members[member] = source.read_bytes()
        policy = records[name]["policy"]
        bundled = members.get(str(policy["member"])) if policy and policy["valid"] else None
        entry = attestation_entry(member, members[member], bundled)
        records[name]["attestations"].append({**entry, "source": str(source)})


def attestation_entry(member: str, data: bytes, policy: bytes | None) -> dict[str, Any]:
    """The index's entry for the bundled attestation record *data*, re-derived offline
    (:func:`~tundravm.read_attestation`) against *policy*, the bytes of the variant's
    bundled policy (``None`` without a valid one): ``member``, the re-derived ``verdict``,
    ``observed_at``, ``policy_sha256``, ``artifact_sha256``, ``policy_matches`` and the
    ``inconsistencies`` (a record that cannot be read has one, and ``null`` facts)."""
    try:
        found = read_attestation(data, policy=policy)
    except TdxError as exc:
        return {
            "member": member,
            "verdict": None,
            "observed_at": None,
            "policy_sha256": None,
            "artifact_sha256": None,
            "policy_matches": None,
            "inconsistencies": [str(exc.args[0]) if exc.args else str(exc)],
        }
    return {
        "member": member,
        "verdict": found.verdict,
        "observed_at": found.observed_at,
        "policy_sha256": found.policy_sha256,
        "artifact_sha256": found.artifact_sha256,
        "policy_matches": found.policy_matches,
        "inconsistencies": list(found.inconsistencies),
    }


def _provenance(recipe: Recipe, variant: str) -> dict[str, dict[str, int]]:
    """How many declarations each fragment holds and each resolution action touched."""
    fragments: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    for origins in provenance(recipe, variant=variant).values():
        for origin in origins:
            actions[origin.action] += 1
            if origin.action in ("declared", "added"):
                holder = origin.fragments[-1][0] if origin.fragments else origin.variant
                fragments[holder] += 1
    return {"fragments": dict(sorted(fragments.items())), "actions": dict(sorted(actions.items()))}


def lint_entry(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The index's ``lint`` entry from ``lint.json``'s rows: the count per severity and per code."""
    severities = Counter(str(row.get("severity")) for row in rows)
    codes = Counter(str(row.get("code")) for row in rows)
    return {
        "member": "lint.json",
        "errors": severities["error"],
        "warnings": severities["warning"],
        "infos": severities["info"],
        "codes": dict(sorted(codes.items())),
    }


def bake_simulated(payload: Mapping[str, Any]) -> bool:
    """Whether the bake of a ``bake-result.json`` *payload* is simulated, as
    :func:`~tundravm.declarative.read_artifacts` reads it."""
    extra = payload.get(MANIFEST_KEY)
    recorded = extra.get("simulated") if isinstance(extra, Mapping) else None
    return bool(payload.get("backend") == INPROCESS if recorded is None else recorded)


def _tools(runner: ProbeRunner | None, notes: list[str]) -> dict[str, str | None]:
    from tundravm import __version__

    ok, line = probe(_MKOSI, runner if runner is not None else run_probe)
    mkosi = (line.removeprefix("ok mkosi").strip() or None) if ok else None
    if mkosi is None:
        notes.append("mkosi is not on this host: its version is not recorded")
    return {"tundravm": __version__, "python": platform.python_version(), "mkosi": mkosi}


def _verdict(
    index: Mapping[str, Any], required: Sequence[EvidenceRequirement], notes: list[str]
) -> dict[str, Any]:
    """Each check's state, the ``reasons`` the verdict is not ``pass``, and ``overall``.

    A contradiction is a ``fail`` reason. A missing check is an ``incomplete`` reason
    when *required* names it, else a note.
    """
    reasons: list[dict[str, Any]] = []
    variants: Mapping[str, Any] = index["variants"]

    def add(severity: str, code: str, subject: str | None, message: str) -> None:
        reasons.append({"severity": severity, "code": code, "subject": subject, "message": message})

    def absent(
        need: EvidenceRequirement,
        code: str,
        subject: str | None,
        message: str,
        *,
        noted: bool = False,
    ) -> None:
        """*noted*: gathering already said so in a note."""
        if need in required:
            add("incomplete", code, subject, f"{message} (required: {need})")
        elif not noted:
            notes.append(message)

    if index["recipe"]["matches_bake"] is False:
        add("fail", "recipe-mismatch", None, "the recipe changed since the bake")
    rows = [(n, row) for n, record in variants.items() for row in record["artifacts"]]
    for name, row in rows:
        subject = f"{name}/{row['target']}"
        if row["integrity"] == "mismatch":
            add("fail", "integrity-mismatch", subject, f"{row['path']} changed since the bake")
        elif row["integrity"] == "missing":
            add("fail", "artifact-missing", subject, f"{row['path']} is gone or unreadable")
    for name, record in variants.items():
        if record["tree_matches"] is False:
            add("fail", "tree-mismatch", name, "the compiled tree changed since the bake")
    lockfile = index["lockfile"]
    if lockfile is None:
        message = "no lockfile: the bake was not frozen, or it is gone"
        absent("lock", "lock-missing", None, message, noted=True)
    elif lockfile["verdict"] == "drifted":
        drifted = ", ".join(str(d["subject"] or d["code"]) for d in lockfile["drift"])
        add("fail", "lock-drifted", None, f"the recipe drifted from the lockfile: {drifted}")
    if lockfile is not None and not lockfile["bound"]:
        add("incomplete", "lock-unbound", None, "the lockfile is not the one the bake used")
    for name, record in variants.items():
        for entry in record["sbom"]:
            subject = f"{name}/{entry['target']}"
            if entry["missing"]:
                gone = ", ".join(entry["missing"])
                message = f"the {gone} the bake recorded is gone: the SBOM is not bound to the bake"
                add("incomplete", "sbom-material-missing", subject, message)
            elif not entry["bound"]:
                add("incomplete", "sbom-unbound", subject, "the SBOM is not bound to the bake")
            if not entry["complete"]:
                message = (
                    f"the SBOM of {subject} lists the declared packages: no installed inventory "
                    "(mkosi manifest)"
                )
                absent("installed", "sbom-incomplete", subject, message, noted=True)
    lint = index["lint"]
    if lint["errors"]:
        add("fail", "lint-errors", None, f"lint reports {lint['errors']} error(s)")
    reproduced = {name: _reproduced(record) for name, record in variants.items()}
    for name, state in reproduced.items():
        if state == "different":
            add("fail", "not-reproducible", name, "a second build gave different artifacts")
        elif state == "not checked":
            absent(
                "reproducible",
                "reproducibility-unchecked",
                name,
                f"reproducibility of {name} was not checked (bake --verify-reproducible)",
            )
        elif state != "matched":
            absent(
                "reproducible",
                "reproducibility-incomplete",
                name,
                f"the second build of {name} did not complete: it is {state}",
            )
    policies = {name: _policy_state(record["policy"]) for name, record in variants.items()}
    for name, state in policies.items():
        source = (variants[name]["policy"] or {}).get("source")
        if state == "invalid":
            add("fail", "policy-invalid", name, f"{source} is not a valid policy")
        elif state == "placeholder":
            add("fail", "policy-placeholder", name, f"{source} holds placeholder registers")
        elif state == "mismatch":
            add("fail", "policy-artifact-mismatch", name, f"{source} measured another artifact")
        elif state == "unbound":
            absent("measured", "policy-unbound", name, f"{source} names no artifact sha256")
        elif state == "missing":
            message = f"no measurements policy for {name}"
            absent("measured", "policy-missing", name, message, noted=True)
    for name, record in variants.items():
        if policies[name] != "verified":
            continue
        for entry in record["coverage"]:
            if entry["state"] != "measured":
                subject = f"{name}/{entry['target']}"
                message = f"{subject} is not measured: {entry['reason']}"
                absent("measured", "artifact-unmeasured", subject, message)
    attested = _attested_reasons(index, policies, required, add, absent)
    covered = [entry["state"] for r in variants.values() for entry in r["coverage"]]
    simulated = [f"{name}/{row['target']}" for name, row in rows if row["simulated"]]
    if simulated:
        absent(
            "real",
            "simulated-artifact",
            ", ".join(simulated),
            "the artifacts are simulated (in-process backend): nothing was built",
        )
    severities = {reason["severity"] for reason in reasons}
    overall = "fail" if "fail" in severities else "incomplete" if severities else "pass"
    states = {row["integrity"] for _, row in rows}
    return {
        "integrity": "verified"
        if states == {"verified"}
        else ("mismatch" if "mismatch" in states else "missing"),
        "lock": "missing" if lockfile is None else str(lockfile["verdict"]),
        "reproducible": _worst(reproduced.values(), _REPRODUCED_ORDER),
        "lint": "errors" if lint["errors"] else ("warnings" if lint["warnings"] else "clean"),
        "recipe": {True: "matches", False: "changed", None: "unknown"}[
            index["recipe"]["matches_bake"]
        ],
        "tree": _worst(
            (
                {True: "matches", False: "changed", None: "unknown"}[r["tree_matches"]]
                for r in variants.values()
            ),
            ("changed", "unknown", "matches"),
        ),
        "policy": _worst(policies.values(), _POLICY_ORDER),
        "measured": "none"
        if "measured" not in covered
        else ("full" if set(covered) == {"measured"} else "partial"),
        "sbom": _worst(
            (_sbom_state(entry) for record in variants.values() for entry in record["sbom"]),
            _SBOM_ORDER,
        ),
        "attested": attested,
        "artifacts": "simulated" if simulated else "real",
        "required": list(required),
        "reasons": reasons,
        "overall": overall,
    }


def _attested_reasons(
    index: Mapping[str, Any],
    policies: Mapping[str, str],
    required: Sequence[EvidenceRequirement],
    add: Callable[[str, str, str | None, str], None],
    absent: Callable[[EvidenceRequirement, str, str | None, str], None],
) -> str:
    """The ``attested`` reasons; returns the summary state (``none`` when no artifact has a
    valid policy and no record is bundled)."""
    variants: Mapping[str, Any] = index["variants"]
    window = int((index.get("attestation") or {}).get("max_age_seconds") or DEFAULT_MAX_AGE)
    created = parse_time(str(index.get("created")))
    states: list[str] = []
    for name, record in variants.items():
        entries = record.get("attestations") or []
        for entry in entries:
            if entry["inconsistencies"]:
                states.append("inconsistent")
                message = (
                    f"{entry['member']} disagrees with its own document: "
                    f"{entry['inconsistencies'][0]}"
                )
                add("fail", "attestation-inconsistent", name, message)
        if policies[name] != "verified" or (not entries and "attested" not in required):
            continue
        measures = record["policy"].get("measures") or {}
        subject = f"{name}/{measures['target']}" if measures.get("target") else name
        found = [_attestation_state(e, measures.get("sha256"), created, window) for e in entries]
        best = next((s for s in _ATTESTED_BEST if s in found), "missing")
        states.append(best)
        if best in ("trusted", "inconsistent"):
            continue
        verdicts = ", ".join(sorted({str(e["verdict"]) for e in entries}))
        message = {
            "missing": f"{subject} has no attestation record (evidence --attestation FILE, "
            "from `tundravm attest --record FILE`)",
            "policy-mismatch": f"no attestation record of {subject} was checked against its "
            "bundled policy (another policy sha256 or artifact)",
            "untrusted": f"no attestation record of {subject} is trusted (verdicts: {verdicts}); "
            "a trusted one needs `attest --validator`",
            "stale": f"no trusted attestation record of {subject} was observed within "
            f"{_duration(window)} of the evidence's creation ({index.get('created')})",
        }[best]
        absent("attested", f"attestation-{best}", subject, message)
    if "attested" in required and "verified" not in policies.values():
        message = "no artifact has a valid measurements policy an attestation could check"
        absent("attested", "attestation-missing", None, message)
        states.append("missing")
    return _worst(states, _ATTESTED_ORDER) if states else "none"


def _attestation_state(
    entry: Mapping[str, Any], measured: object, created: datetime | None, window: int
) -> str:
    """One record: ``inconsistent``, ``policy-mismatch``, ``untrusted``, ``stale`` or
    ``trusted``, the first that applies."""
    if entry["inconsistencies"]:
        return "inconsistent"
    if entry["artifact_sha256"] != measured or entry["policy_matches"] is not True:
        return "policy-mismatch"
    if entry["verdict"] != "trusted":
        return "untrusted"
    observed = parse_time(str(entry["observed_at"]))
    if created is None or observed is None:
        return "stale"
    return "stale" if abs((created - observed).total_seconds()) > window else "trusted"


def _duration(seconds: int) -> str:
    """*seconds* as ``--max-age`` would spell it: ``24h``, ``90m``, ``45s``, ``7d``."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def parse_duration(text: str) -> int:
    """``--max-age`` *text* (``45s``, ``30m``, ``24h``, ``7d``, or plain seconds) in seconds;
    ``ValidationError`` unless it is a positive duration."""
    value = text.strip().lower()
    size = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(value[-1:], 1)
    digits = value[:-1] if value[-1:] in ("s", "m", "h", "d") else value
    if not digits.isdigit() or int(digits) == 0:
        raise ValidationError(
            f"{text!r} is not a positive duration.",
            hint="Spell it as a number with s, m, h or d: 45s, 30m, 24h, 7d.",
        )
    return int(digits) * size


_ATTESTED_BEST = ("trusted", "stale", "untrusted", "policy-mismatch", "inconsistent")
"""A variant's attestation state is that of its best record, in this order."""
_ATTESTED_ORDER = ("inconsistent", "policy-mismatch", "untrusted", "stale", "missing", "trusted")
_REPRODUCED_ORDER = ("different", "error", "interrupted", "running", "not checked", "matched")
_POLICY_ORDER = ("invalid", "placeholder", "mismatch", "unbound", "missing", "verified")
_SBOM_ORDER = ("material-missing", "unbound", "declared", "installed")


def _sbom_state(entry: Mapping[str, Any]) -> str:
    if entry["missing"]:
        return "material-missing"
    if not entry["bound"]:
        return "unbound"
    return "installed" if entry["complete"] else "declared"


def _worst(states: Iterable[str], order: Sequence[str]) -> str:
    """The first of *order* among *states*; the last of *order* when there is none."""
    found = set(states)
    return next((state for state in order if state in found), order[-1])


def _reproduced(record: Mapping[str, Any]) -> str:
    """What ``bake --verify-reproducible`` recorded for a variant, or ``not checked``."""
    return str(record["reproducibility"] or "not checked")


def _policy_state(policy: Mapping[str, Any] | None) -> str:
    if policy is None:
        return "missing"
    if not policy["valid"]:
        return "invalid"
    if policy["placeholder"]:
        return "placeholder"
    matches = policy["artifact_matches"]
    return "unbound" if matches is None else ("verified" if matches else "mismatch")


# ── rendering ────────────────────────────────────────────────────────────


def _text(index: Mapping[str, Any]) -> str:
    verdict = index["verdict"]
    recipe = index["recipe"]
    lines = [
        f"evidence {index['created']}  verdict {verdict['overall']}",
        f"  recipe     {recipe['path'] or recipe['name'] or '-'}  digest {recipe['digest'][:12]}"
        f"  {verdict['recipe']}",
        f"  tree       {verdict['tree']}",
        f"  integrity  {verdict['integrity']}",
        f"  lock       {verdict['lock']}",
        f"  reproduce  {verdict['reproducible']}",
        f"  policy     {verdict['policy']}",
        f"  measured   {verdict['measured']}",
        f"  sbom       {verdict['sbom']}",
        f"  attested   {verdict['attested']}",
        f"  artifacts  {verdict['artifacts']}",
        f"  lint       {verdict['lint']}",
    ]
    if verdict["required"]:
        lines.append(f"  required   {', '.join(verdict['required'])}")
    for name, record in index["variants"].items():
        for row in record["artifacts"]:
            lines.append(
                f"  {name}/{row['target']}  {row['path']}  sha256 {row['sha256'][:12]}  "
                f"{row['integrity']}"
            )
    if verdict["reasons"]:
        lines.append("reasons:")
        lines.extend(
            f"  {r['severity']:<10}  {r['code']}  {r['subject'] or '-'}: {r['message']}"
            for r in verdict["reasons"]
        )
    lines.append("members:")
    lines.extend(f"  {entry['sha256']}  {name}" for name, entry in sorted(index["members"].items()))
    lines.extend(f"note: {note}" for note in index["notes"])
    return "\n".join(lines) + "\n"


_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evidence: $title</title>
<style>
body { font: 14px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 64rem;
  padding: 1rem; color: #1b1f24; background: #fff; }
h1, h2, h3 { line-height: 1.25; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; display: block;
  overflow-x: auto; }
th, td { border: 1px solid #d0d7de; padding: .3rem .5rem; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
code { font-family: ui-monospace, monospace; font-size: 12px; word-break: break-all; }
.banner { display: flex; flex-wrap: wrap; gap: .5rem; padding: .75rem; border-radius: 6px; }
.banner span { padding: .2rem .6rem; border-radius: 4px; background: #fff; }
.banner ul { flex-basis: 100%; margin: 0; padding-left: 1.25rem; }
.pass { background: #dafbe1; border: 1px solid #1a7f37; }
.fail { background: #ffebe9; border: 1px solid #cf222e; }
.incomplete { background: #fff8c5; border: 1px solid #9a6700; }
</style>
</head>
<body>
<h1>Evidence: $title</h1>
<div class="banner $overall" id="verdict"><strong>verdict: $overall</strong>$checks$reasons</div>
<h2>Recipe</h2>
$recipe
<h2>Lockfile</h2>
$lockfile
<h2>Lint</h2>
$lint
<h2>Tools</h2>
$tools
$variants
<h2>Notes</h2>
$notes
<h2>Members</h2>
$members
</body>
</html>
"""
)


def _cell(value: object) -> str:
    text = "-" if value is None else str(value)
    return f"<code>{html.escape(text)}</code>"


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _pairs(mapping: Mapping[str, object]) -> str:
    return _table(("key", "value"), [(key, value) for key, value in mapping.items()])


def _variant_html(name: str, record: Mapping[str, Any]) -> str:
    parts = [f"<h2>Variant {html.escape(name)}</h2>"]
    parts.append(
        _table(
            ("target", "path", "size", "sha256", "integrity", "simulated"),
            [
                (r["target"], r["path"], r["size"], r["sha256"], r["integrity"], r["simulated"])
                for r in record["artifacts"]
            ],
        )
    )
    parts.append(
        _pairs(
            {
                "tree digest": record["tree_digest"],
                "tree matches the recipe": record["tree_matches"],
                "reproducibility": record["reproducibility"],
            }
        )
    )
    policy = record["policy"]
    parts.append("<h3>Measurements policy</h3>")
    parts.append(_pairs(policy) if policy else "<p>none</p>")
    parts.append(
        _table(
            ("target", "sha256", "measured", "why not"),
            [(c["target"], c["sha256"], c["state"], c["reason"]) for c in record["coverage"]],
        )
    )
    parts.append("<h3>Attestations</h3>")
    parts.append(
        _table(
            ("member", "verdict", "observed at", "policy matches", "inconsistencies"),
            [
                (
                    a["member"],
                    a["verdict"],
                    a["observed_at"],
                    a["policy_matches"],
                    "; ".join(a["inconsistencies"]) or None,
                )
                for a in record.get("attestations") or ()
            ],
        )
    )
    parts.append("<h3>SBOM</h3>")
    parts.append(
        _table(
            ("target", "member", "packages", "sources", "installed", "bound"),
            [
                (s["target"], s["member"], s["packages"], s["sources"], s["complete"], s["bound"])
                for s in record["sbom"]
            ],
        )
    )
    summary = record["provenance"]
    if summary is not None:
        parts.append("<h3>Provenance</h3>")
        parts.append(_table(("fragment", "declarations"), list(summary["fragments"].items())))
        parts.append(_table(("action", "count"), list(summary["actions"].items())))
    return "\n".join(parts)


def render_html(index: Mapping[str, Any]) -> str:
    """The evidence *index* as one HTML page with no external assets; every value is escaped."""
    verdict = index["verdict"]
    recipe = index["recipe"]
    lockfile = index["lockfile"]
    lint = index["lint"]
    title = str(recipe["name"] or recipe["path"] or "bake")
    checks = "".join(
        f"<span>{html.escape(key)}: {html.escape(str(verdict[key]))}</span>"
        for key in (
            "integrity",
            "lock",
            "reproducible",
            "lint",
            "recipe",
            "tree",
            "policy",
            "measured",
            "sbom",
            "attested",
        )
    )
    reasons = "".join(
        f"<li><code>{html.escape(r['severity'])} {html.escape(r['code'])}</code> "
        f"{html.escape(r['subject'] or '-')}: {html.escape(r['message'])}</li>"
        for r in verdict["reasons"]
    )
    lock_html = "<p>none</p>"
    if lockfile is not None:
        drift = ", ".join(str(d["subject"] or d["code"]) for d in lockfile["drift"]) or None
        shown = {k: v for k, v in lockfile.items() if k != "drift"}
        lock_html = _pairs({**shown, "drift": drift})
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in index["notes"])
    return _PAGE.substitute(
        title=html.escape(title),
        overall=html.escape(str(verdict["overall"])),
        checks=checks,
        reasons=f'<ul id="reasons">{reasons}</ul>' if reasons else "",
        recipe=_pairs(recipe),
        lockfile=lock_html,
        lint=_pairs({k: v for k, v in lint.items() if k != "codes"})
        + _table(("code", "count"), list(lint["codes"].items())),
        tools=_pairs(index["tools"]),
        variants="\n".join(_variant_html(n, r) for n, r in index["variants"].items()),
        notes=f"<ul>{notes}</ul>" if notes else "<p>none</p>",
        members=_table(
            ("member", "size", "sha256"),
            [(name, e["size"], e["sha256"]) for name, e in index["members"].items()],
        ),
    )


__all__ = [
    "DEFAULT_MAX_AGE",
    "EVIDENCE_FORMATS",
    "EVIDENCE_REQUIREMENTS",
    "Evidence",
    "EvidenceFormat",
    "EvidenceRequirement",
    "attestation_entry",
    "evidence",
    "parse_duration",
    "render_html",
]
