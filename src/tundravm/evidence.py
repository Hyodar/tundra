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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Any, Literal

from . import check as _check
from .backends.base import Requirement
from .declarative._lowered import Lowered
from .declarative.bom import sbom
from .declarative.lifecycle import (
    MANIFEST_KEY,
    NO_LOCK,
    Artifact,
    Lock,
    ProbeRunner,
    check_report,
    compile_image,
    image_lock_status,
    lock_status,
    probe,
    read_artifacts,
    read_lock,
    run_probe,
    verify_artifact,
)
from .declarative.lower import lower
from .declarative.model import Recipe
from .declarative.resolve import provenance
from .errors import ArtifactError, StateError, TdxError
from .lockfile import lock_variants
from .measure.policy import is_placeholder, read_policy
from .models import BAKE_RESULT_FILENAME

EVIDENCE_SCHEMA_VERSION = 1
INDEX_NAME = "evidence.json"
BUNDLE_ROOT = "evidence"
"""The top-level directory every member of a bundle sits under."""
EvidenceFormat = Literal["text", "json"]
EVIDENCE_FORMATS: tuple[EvidenceFormat, ...] = ("text", "json")
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

    @property
    def verdict(self) -> str:
        """``pass`` when integrity, lock, reproducibility and lint all hold, else ``fail``."""
        return str(self.index["verdict"]["overall"])

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def index_json(self) -> str:
        """``evidence.json``: sorted, indented JSON with a trailing newline."""
        return json.dumps(self.index, indent=2, sort_keys=True) + "\n"

    def files(self) -> dict[str, bytes]:
        """Every file of the evidence, ``evidence.json`` included, by sorted relative path."""
        found = {**self.members, INDEX_NAME: self.index_json().encode()}
        return dict(sorted(found.items()))

    def write(self, directory: Path) -> Path:
        """Write every file under *directory* (created; files of a previous run replaced)."""
        root = Path(directory)
        for name, data in self.files().items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
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
    out: Path,
    variants: Sequence[str] | None = None,
    lock: Lock | Path | None = None,
    policy: Path | None = None,
    recipe_path: Path | None = None,
    runner: ProbeRunner | None = None,
) -> Evidence:
    """The evidence of the bake in *out* for *variants* (default: every baked variant).

    *lock* (a :class:`Lock` or a lockfile path) defaults to the lockfile the bake
    recorded, else ``out/tundravm.lock``. *policy* is a ``measure --export-policy``
    file used for every selected variant instead of ``out/<variant>/policy.json``.
    *recipe_path* records the recipe file and its sha256. *runner* replaces the
    ``mkosi --version`` probe (tests). Nothing is written; see :meth:`Evidence.write`.
    """
    recipe = lowered_or_recipe if isinstance(lowered_or_recipe, Recipe) else None
    img = lower(recipe) if recipe is not None else lowered_or_recipe
    assert isinstance(img, Lowered)
    base = Path(out)
    manifest = base / BAKE_RESULT_FILENAME
    artifacts = read_artifacts(base)
    extra = _manifest_extra(manifest)
    names = _selected(artifacts, variants, base)
    notes: list[str] = []
    members: dict[str, bytes] = {"bake-result.json": manifest.read_bytes()}

    locked, lock_bytes, lock_from = _lock(lock, extra, base)
    lock_entry = _lock_entry(recipe, img, locked, lock_bytes, lock_from, names, notes)
    if lock_bytes is not None:
        members["tundravm.lock"] = lock_bytes

    diagnostics = check_report(
        recipe,
        None if recipe is not None else img,
        variants=names,
        lock=NO_LOCK if locked is None else locked,
    )
    members["lint.json"] = _json_bytes([d.to_dict() for d in diagnostics])
    tree = _current_tree(img, artifacts, locked)
    digests = _recipe_digests(img, names, locked)
    reproducible = extra.get("reproducible")
    records = {
        name: _variant(
            name,
            [a for a in artifacts if a.variant == name],
            base=base,
            recipe=recipe,
            locked=locked,
            policy=policy,
            tree=tree,
            reproducible=reproducible if isinstance(reproducible, bool) else None,
            members=members,
            notes=notes,
        )
        for name in names
    }
    if recipe is None:
        notes.append("no recipe given (a lowered recipe): the provenance summary is left out")
    index: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "created": _created(),
        "recipe": _recipe_entry(recipe, img, recipe_path, digests, artifacts, names),
        "bake": {
            "path": str(manifest),
            "backend": _manifest_field(manifest, "backend"),
            "simulated": bool(extra.get("simulated", False)),
        },
        "lockfile": lock_entry,
        "lint": _lint_entry(diagnostics),
        "tools": _tools(runner, notes),
        "variants": records,
    }
    index["verdict"] = _verdict(index)
    index["notes"] = list(dict.fromkeys(notes))
    index["members"] = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in sorted(members.items())
    }
    return Evidence(index=index, members=dict(sorted(members.items())), notes=tuple(index["notes"]))


# ── gathering ────────────────────────────────────────────────────────────


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
    return list(dict.fromkeys(variants))


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


def _current_tree(img: Lowered, artifacts: Sequence[Artifact], locked: Lock | None) -> str | None:
    baked = tuple(sorted({artifact.variant for artifact in artifacts}))
    if not baked or not set(baked) <= set(img.state.profiles):
        return None
    try:
        return compile_image(img, baked, locked=locked).digest
    except TdxError:
        return None


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


def _variant(
    name: str,
    artifacts: Sequence[Artifact],
    *,
    base: Path,
    recipe: Recipe | None,
    locked: Lock | None,
    policy: Path | None,
    tree: str | None,
    reproducible: bool | None,
    members: dict[str, bytes],
    notes: list[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    sboms: list[dict[str, Any]] = []
    for artifact in artifacts:
        size = artifact.path.stat().st_size if artifact.path.is_file() else None
        rows.append(
            {
                "target": artifact.target,
                "path": str(artifact.path),
                "size": size,
                "sha256": artifact.sha256,
                "integrity": _integrity(artifact),
                "simulated": artifact.simulated,
            }
        )
        document = sbom(artifact, lock=locked)
        member = f"variants/{name}/sbom-{artifact.target}.spdx.json"
        members[member] = document.render("spdx-json").encode()
        notes.extend(f"{name}/{artifact.target} sbom: {note}" for note in document.notes)
        sboms.append(
            {
                "target": artifact.target,
                "member": member,
                "packages": len(document.packages),
                "sources": len(document.sources),
                "manifest_found": document.manifest_found,
            }
        )
    recorded_tree = next((a.tree_digest for a in artifacts if a.tree_digest), "")
    return {
        "artifacts": rows,
        "tree_digest": recorded_tree or None,
        "tree_matches": None if not recorded_tree or tree is None else recorded_tree == tree,
        "reproducible": reproducible,
        "policy": _policy(name, artifacts, base, policy, members, notes),
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
            f"with `tundravm measure ... --variant {name} --export-policy {source}`"
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


def _lint_entry(diagnostics: Sequence[_check.Diagnostic]) -> dict[str, Any]:
    counts = _check.summarize(diagnostics)
    codes = Counter(d.code for d in diagnostics)
    return {"member": "lint.json", **counts, "codes": dict(sorted(codes.items()))}


def _tools(runner: ProbeRunner | None, notes: list[str]) -> dict[str, str | None]:
    from tundravm import __version__

    ok, line = probe(_MKOSI, runner if runner is not None else run_probe)
    mkosi = (line.removeprefix("ok mkosi").strip() or None) if ok else None
    if mkosi is None:
        notes.append("mkosi is not on this host: its version is not recorded")
    return {"tundravm": __version__, "python": platform.python_version(), "mkosi": mkosi}


def _verdict(index: Mapping[str, Any]) -> dict[str, str]:
    rows = [row for record in index["variants"].values() for row in record["artifacts"]]
    states = {row["integrity"] for row in rows}
    integrity = (
        "verified"
        if states == {"verified"}
        else ("mismatch" if "mismatch" in states else "missing")
    )
    lockfile = index["lockfile"]
    lock = "missing" if lockfile is None else str(lockfile["verdict"])
    flags = {record["reproducible"] for record in index["variants"].values()}
    reproducible = (
        "not reproducible"
        if False in flags
        else "reproducible"
        if flags == {True}
        else "not checked"
    )
    lint = index["lint"]
    linted = "errors" if lint["errors"] else ("warnings" if lint["warnings"] else "clean")
    passed = (
        integrity == "verified"
        and lock == "current"
        and reproducible != "not reproducible"
        and linted != "errors"
    )
    return {
        "integrity": integrity,
        "lock": lock,
        "reproducible": reproducible,
        "lint": linted,
        "overall": "pass" if passed else "fail",
    }


# ── rendering ────────────────────────────────────────────────────────────


def _text(index: Mapping[str, Any]) -> str:
    verdict = index["verdict"]
    recipe = index["recipe"]
    lines = [
        f"evidence {index['created']}  verdict {verdict['overall']}",
        f"  recipe     {recipe['path'] or recipe['name'] or '-'}  digest {recipe['digest'][:12]}",
        f"  integrity  {verdict['integrity']}",
        f"  lock       {verdict['lock']}",
        f"  reproduce  {verdict['reproducible']}",
        f"  lint       {verdict['lint']}",
    ]
    for name, record in index["variants"].items():
        for row in record["artifacts"]:
            lines.append(
                f"  {name}/{row['target']}  {row['path']}  sha256 {row['sha256'][:12]}  "
                f"{row['integrity']}"
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
.pass { background: #dafbe1; border: 1px solid #1a7f37; }
.fail { background: #ffebe9; border: 1px solid #cf222e; }
</style>
</head>
<body>
<h1>Evidence: $title</h1>
<div class="banner $overall" id="verdict"><strong>verdict: $overall</strong>$checks</div>
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
                "reproducible": record["reproducible"],
            }
        )
    )
    policy = record["policy"]
    parts.append("<h3>Measurements policy</h3>")
    parts.append(_pairs(policy) if policy else "<p>none</p>")
    parts.append("<h3>SBOM</h3>")
    parts.append(
        _table(
            ("target", "member", "packages", "sources", "mkosi manifest"),
            [
                (s["target"], s["member"], s["packages"], s["sources"], s["manifest_found"])
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
        for key in ("integrity", "lock", "reproducible", "lint")
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


__all__ = ["EVIDENCE_FORMATS", "Evidence", "EvidenceFormat", "evidence", "render_html"]
