"""Witness a release: rebuild it from another checkout and say whether the bytes agree.

A release directory (:mod:`tundravm.release`) is its publisher's claim: these
artifacts, with these sha256s, are what this recipe and this lockfile build.
:func:`witness` checks that claim from somewhere else. It runs from the witness's own
checkout of the recipe and refuses one whose digest is not the release's
``recipe_digest``, takes the lockfile from the release's evidence bundle (its sha256
must be the release's ``lock_sha256``), bakes the released variants with it into its
own build directory and compares every artifact's sha256 with the one the release
lists. The verdict (``matched``, ``different`` or ``error``), the builder's name and its
backend's fingerprint go to ``witness-<builder>.json`` (:data:`WITNESS_SCHEMA`), bound
to the release by the sha256 of its ``release-manifest.json``, and with a signing key
to ``witness-<builder>.json.sig`` (``ssh-keygen -Y sign``, namespace
:data:`NAMESPACE`, the builder as principal).

A release directory is immutable and lists every file in its manifest, so witnesses
live beside it: ``RELEASES/<name>.witnesses/`` (:func:`witnesses_dir`).
:func:`verify_witnesses` applies a trust policy to such a directory: at least N
distinct principals of an allowed-signers file, the publisher excluded, each with a
signed, matched witness of this very manifest.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import _ssh_sig
from .backends.base import BuildBackend, backend_fingerprint
from .declarative._lowered import Lowered
from .declarative.lifecycle import INPROCESS, Backend, Lock, bake_image, lock_status
from .declarative.lower import lower
from .declarative.model import Recipe
from .errors import LockfileError, StateError, TdxError, ValidationError
from .evidence import BUNDLE_ROOT
from .evidence import INDEX_NAME as EVIDENCE_INDEX
from .lockfile import parse_lockfile
from .observability import Reporter
from .release import (
    BUNDLE_NAME,
    INDEX_NAME,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    RELEASE_SCHEMA,
    SIGNER,
    STATE_NAME,
    is_release,
    recorded_keys,
    signing_key,
)

WITNESS_SCHEMA = "tundravm.witness/1"
"""The ``schema`` of a witness file, which ``witness --format json`` prints."""
WITNESS_VERIFY_SCHEMA = "tundravm.witness-verify/1"
"""The ``schema`` of :meth:`WitnessVerification.to_dict` (``verify``'s ``witnesses``)."""
NAMESPACE = "tundravm-witness"
"""The ``-n`` namespace a witness file is signed and checked in."""
WITNESSES_SUFFIX = ".witnesses"
"""``RELEASES/<name>.witnesses/``: where a release's witnesses live by default."""
WitnessVerdict = Literal["matched", "different", "error"]
WITNESS_VERDICTS: tuple[WitnessVerdict, ...] = ("matched", "different", "error")
WitnessFormat = Literal["text", "json"]
WITNESS_FORMATS: tuple[WitnessFormat, ...] = ("text", "json")
WITNESS_CODES: tuple[str, ...] = (
    "witness-insufficient",
    "witness-mismatch",
    "witness-wrong-release",
    "witness-signature-invalid",
    "witness-unsigned",
    "witness-self",
    "witness-simulated",
)
"""Every reason code of :func:`verify_witnesses`."""
_BUILDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
_MEMBER_LIMIT = 64 * 1024 * 1024
"""The largest lockfile member read from an evidence bundle."""


# ── the witness ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Witness:
    """One builder's rebuild of a release; :meth:`to_dict` is its witness file.

    ``artifacts`` holds ``{"variant", "target", "path", "expected", "actual",
    "match"}`` per released artifact: ``expected`` is the sha256 the release lists,
    ``actual`` the witness build's (``None`` when it built none). ``verdict`` is
    ``matched`` when every one matches, ``different`` when one does not, ``error`` when
    the rebuild failed (``error``: ``{"code", "message"}``). ``simulated`` when the
    witness built with the in-process backend: such a witness never counts toward a
    trust policy unless it allows simulated ones. ``path`` is the witness file written,
    ``signature`` its ``.sig``; ``build`` the witness build directory, ``kept`` whether it
    is still there (a matched witness removes its default build directory).
    """

    release: str
    recipe: str
    version: str
    manifest_sha256: str
    recipe_digest: str
    lock_sha256: str
    builder: str
    backend: Mapping[str, Any]
    simulated: bool
    artifacts: tuple[Mapping[str, Any], ...]
    verdict: WitnessVerdict
    created: str
    error: Mapping[str, str] | None = None
    signer: Mapping[str, str] | None = None
    release_path: Path = Path()
    path: Path | None = None
    signature: Path | None = None
    build: Path = Path()
    kept: bool = True
    built: Mapping[str, str] = field(default_factory=dict)
    """The witness build's file of each artifact, by ``<variant>/<target>``."""

    @property
    def matched(self) -> bool:
        return self.verdict == "matched"

    def to_dict(self) -> dict[str, object]:
        """The witness file: ``schema`` is ``tundravm.witness/1``."""
        from tundravm import __version__

        return {
            "schema": WITNESS_SCHEMA,
            "release": self.release,
            "recipe": self.recipe,
            "version": self.version,
            "manifest_sha256": self.manifest_sha256,
            "recipe_digest": self.recipe_digest,
            "lock_sha256": self.lock_sha256,
            "builder": self.builder,
            "signer": None if self.signer is None else dict(self.signer),
            "backend": dict(self.backend),
            "simulated": self.simulated,
            "artifacts": [dict(row) for row in self.artifacts],
            "verdict": self.verdict,
            "error": None if self.error is None else dict(self.error),
            "created": self.created,
            "tundravm": __version__,
        }

    def json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def render(self, format: WitnessFormat = "text") -> str:
        """``json`` (the witness file) or ``text``: the verdict, a row per artifact and,
        when they differ, where to compare them."""
        if format == "json":
            return self.json()
        tag = " (simulated)" if self.simulated else ""
        lines = [
            f"witness {self.release_path}  builder {self.builder}  verdict {self.verdict}{tag}",
            f"  backend   {self.backend.get('name')}",
        ]
        header = ("variant", "target", "expected", "actual", "match")
        rows = [
            (
                str(row["variant"]),
                str(row["target"]),
                str(row["expected"])[:12],
                "-" if row["actual"] is None else str(row["actual"])[:12],
                "yes" if row["match"] else "NO",
            )
            for row in self.artifacts
        ]
        widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]
        for cells in (header, *rows):
            padded = "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True))
            lines.append(f"  {padded}".rstrip())
        if self.error is not None:
            lines.append(f"  error     {self.error.get('code')}: {self.error.get('message')}")
        if self.verdict == "different":
            lines.extend(self._compare_hint())
        return "\n".join(lines) + "\n"

    def _compare_hint(self) -> list[str]:
        lines = [f"the witness build is kept in {self.build}: compare what differs with"]
        for row in self.artifacts:
            if row["match"]:
                continue
            ours = self.built.get(f"{row['variant']}/{row['target']}")
            theirs = self.release_path / str(row["path"])
            if ours is not None:
                lines.append(f"  diffoscope {theirs} {ours}")
        lines.append(
            f"  tundravm diff RECIPE --tree {self.build / 'mkosi'}  "
            "(from the publisher's checkout: its recipe against this witness's tree)"
        )
        return lines


def witnesses_dir(release: Path) -> Path:
    """Where the witnesses of the release at *release* live by default:
    ``RELEASES/<name>.witnesses/``, beside it (never inside: a release is immutable)."""
    root = _named(Path(release))
    return root.parent / f"{root.name}{WITNESSES_SUFFIX}"


def witness(
    recipe: Recipe,
    release: Path,
    *,
    backend: Backend | BuildBackend,
    build: Path | None = None,
    builder: str | None = None,
    sign: Path | None = None,
    output: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Witness:
    """Rebuild the release at *release* from *recipe* with *backend* and compare.

    *recipe* is the witness's own checkout: its digest must be the release's
    ``recipe_digest`` (else ``ValidationError``). The lockfile is the release's own, from
    its evidence bundle, and must hash to its ``lock_sha256`` (else ``LockfileError``).
    The released variants are baked into *build* (default
    ``build/witness-<release>``; fetched first unless the backend builds nothing) and
    each artifact's sha256 is compared with the release's. The witness file goes to
    *output* (default ``RELEASES/<release>.witnesses/witness-<builder>.json``, never
    inside the release directory); *sign* (an SSH private key) also writes its
    ``.sig`` in the ``tundravm-witness`` namespace, for principal *builder* (default
    ``<user>@<host>``). A failed rebuild is the verdict ``error``, not an exception; a
    matched witness removes the default build directory, any other keeps it.
    *progress* receives one line per step. An unfinished release raises ``StateError``.
    """
    built = backend.build_backend() if isinstance(backend, Backend) else backend
    img = replace(lower(recipe), backend=built)
    return witness_image(
        recipe,
        img,
        release,
        build=build,
        builder=builder,
        sign=sign,
        output=output,
        progress=progress,
    )


def witness_image(
    recipe: Recipe,
    img: Lowered,
    release: Path,
    *,
    build: Path | None = None,
    builder: str | None = None,
    sign: Path | None = None,
    output: Path | None = None,
    progress: Callable[[str], None] | None = None,
    reporter: Reporter | None = None,
) -> Witness:
    """:func:`witness` for a recipe already lowered as *img*, which carries the backend;
    *reporter* receives the bake's progress events."""
    if img.backend is None:
        raise ValidationError(
            "No build backend to witness the release with.",
            hint="Pass --backend KIND (witness(backend=...)), or bind `backend` in the "
            "recipe file.",
        )
    say = progress if progress is not None else _quiet
    root = Path(release)
    index, manifest_sha = _finished_release(root)
    name = str(index["name"])
    who = _builder(builder)
    target = Path(output) if output is not None else witnesses_dir(root) / f"witness-{who}.json"
    _outside(target, root, "The witness file", "--output FILE")
    default_build = build is None
    work = Path("build") / f"witness-{name}" if build is None else Path(build)
    _outside(work, root, "The witness build", "--build DIR")
    signer = None if sign is None else _signer(Path(sign), who)
    digest = img.select(tuple(img.state.profiles)).digest()
    if digest != index.get("recipe_digest"):
        raise ValidationError(
            f"This checkout's recipe digest {digest[:12]} is not release {name}'s "
            f"({str(index.get('recipe_digest'))[:12]}): it would rebuild another recipe.",
            hint="Check out the commit the release was cut from (its recipe digest is in "
            "release.json), then witness again.",
            context={"release": str(root), "recipe_digest": digest},
        )
    say(f"recipe     digest {digest[:12]}, as release {name} records")
    lock_bytes = _bundled_lock(root)
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()
    if lock_sha != index.get("lock_sha256"):
        raise LockfileError(
            f"The lockfile in {root / BUNDLE_NAME} hashes to {lock_sha[:12]}, but release "
            f"{name} records {str(index.get('lock_sha256'))[:12]}.",
            hint=f"The evidence is not the release's: run `tundravm verify {root}` and "
            "witness a release that verifies.",
            context={"release": str(root), "lock_sha256": lock_sha},
        )
    locked = _parse_lock(lock_bytes, root)
    names = _released(recipe, index, root)
    drift = lock_status(recipe, locked, variants=names)
    if drift:
        listed = "; ".join(f"{d.subject or d.code}: {d.message}" for d in drift)
        raise LockfileError(
            f"This checkout drifted from release {name}'s lockfile: {listed}.",
            hint="Check out the commit the release was cut from, with the tundravm "
            "version release.json records, then witness again.",
            context={"release": str(root)},
        )
    say(f"lockfile   {BUNDLE_NAME}: sha256 {lock_sha[:12]}, as release {name} records")
    say(f"bake       {', '.join(names)} with {img.backend.name} into {work}")
    fingerprint = backend_fingerprint(img.backend)
    error: dict[str, str] | None = None
    found: dict[tuple[str, str], tuple[str, str]] = {}
    simulated = img.backend.name == INPROCESS
    try:
        _, artifacts = bake_image(
            img, names, locked=locked, backend=img.backend, build=work, reporter=reporter
        )
        for artifact in artifacts:
            if artifact.variant in names:
                found[(artifact.variant, artifact.target)] = (
                    _file_sha256(artifact.path),
                    str(artifact.path),
                )
                simulated = simulated or artifact.simulated
    except TdxError as exc:
        message = str(exc.args[0]) if exc.args else str(exc)
        error = {"code": exc.code, "message": message}
        say(f"bake       failed: {exc.code}: {message}")
    rows = _compare(index, found)
    verdict: WitnessVerdict = "matched" if all(row["match"] for row in rows) else "different"
    if error is not None:
        verdict = "error"
    result = Witness(
        release=name,
        recipe=str(index.get("recipe")),
        version=str(index.get("version")),
        manifest_sha256=manifest_sha,
        recipe_digest=digest,
        lock_sha256=lock_sha,
        builder=who,
        backend=fingerprint,
        simulated=simulated,
        artifacts=tuple(rows),
        verdict=verdict,
        created=_now(),
        error=error,
        signer=signer,
        release_path=root,
        path=target,
        build=work,
        built={f"{v}/{t}": path for (v, t), (_, path) in found.items()},
    )
    signature = _write(result, target, sign)
    kept = True
    if result.matched and default_build:
        shutil.rmtree(work, ignore_errors=True)
        kept = work.exists()
    return replace(result, signature=signature, kept=kept)


def _quiet(line: str) -> None:
    del line


def _named(root: Path) -> Path:
    """*root* with a name (``.`` resolved), so ``<name>.witnesses`` sits beside it."""
    return root if root.name else root.resolve()


def _builder(given: str | None) -> str:
    """The builder's name: *given*, else ``<user>@<host>``; also its principal."""
    if given is not None:
        if not _BUILDER.fullmatch(given):
            raise ValidationError(
                f"Builder name {given!r} is not a principal name.",
                hint="Use up to 64 letters, digits, '.', '_', '@', '+' and '-', starting "
                "with a letter or digit, e.g. --builder alice@example.com.",
            )
        return given
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = "witness"
    host = socket.gethostname().split(".")[0] or "host"
    name = re.sub(r"[^A-Za-z0-9._@+-]", "-", f"{user}@{host}").lstrip("._@+-")
    return name[:64] or "witness"


def _signer(key: Path, principal: str) -> dict[str, str]:
    public = _ssh_sig.public_key(key)
    return {
        "principal": principal,
        "public_key": public,
        "fingerprint": _ssh_sig.fingerprint(public),
    }


def _outside(path: Path, root: Path, what: str, flag: str) -> None:
    """Refuse *path* inside the release directory *root*: every file there is listed."""
    if path.resolve().is_relative_to(root.resolve()):
        raise ValidationError(
            f"{what} cannot go inside the release directory {root}: a release is immutable, "
            "and `tundravm verify` fails on every file its manifest does not list.",
            hint=f"Write it beside the release: drop {flag} for the default, "
            f"{witnesses_dir(root)}/ for the witness file.",
            context={"release": str(root), "path": str(path)},
        )


def _finished_release(root: Path) -> tuple[dict[str, Any], str]:
    """``release.json`` of the finished release at *root*, and its manifest's sha256;
    refused when it is not a release, not finished, or disagrees with its manifest."""
    if not is_release(root):
        raise ValidationError(
            f"{root} is not a release directory: it holds no {INDEX_NAME}.",
            hint="Pass a directory `tundravm release` wrote, RELEASES/<recipe>-<version>.",
            context={"path": str(root)},
        )
    index = _read_json(root / INDEX_NAME)
    if index is None or index.get("schema") != RELEASE_SCHEMA:
        raise ValidationError(
            f"{root / INDEX_NAME} is not a {RELEASE_SCHEMA} index.",
            hint=f"Run `tundravm verify {root}`: witness a release that verifies.",
            context={"path": str(root)},
        )
    if (root / STATE_NAME).exists() or not index.get("finished"):
        raise StateError(
            f"Release {index.get('name')} is not finished (verdict {index.get('verdict')}): "
            "its manifest still changes.",
            hint="Witness a finished release: one whose `tundravm release` passed.",
            context={"release": str(root)},
        )
    manifest_path = root / MANIFEST_NAME
    manifest = _read_json(manifest_path)
    if manifest is None or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValidationError(
            f"{manifest_path} is missing or not a {MANIFEST_SCHEMA} manifest.",
            hint=f"Run `tundravm verify {root}`: witness a release that verifies.",
            context={"path": str(root)},
        )
    listed = {
        str(row.get("path")): row.get("sha256")
        for row in manifest.get("files") or ()
        if isinstance(row, Mapping)
    }
    differ = [
        key
        for key in ("name", "recipe", "version", "recipe_digest", "lock_sha256")
        if index.get(key) != manifest.get(key)
    ]
    differ += [
        str(row.get("path"))
        for row in _rows(index)
        if listed.get(str(row.get("path"))) != row.get("sha256")
    ]
    if differ:
        raise ValidationError(
            f"{INDEX_NAME} and {MANIFEST_NAME} of {root} disagree on {', '.join(differ)}.",
            hint=f"Run `tundravm verify {root}`: witness a release that verifies.",
            context={"release": str(root)},
        )
    return index, _file_sha256(manifest_path)


def _rows(index: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in index.get("artifacts") or () if isinstance(row, Mapping)]


def _bundled_lock(root: Path) -> bytes:
    """The lockfile member of the release's evidence bundle, read without extracting."""
    bundle = root / BUNDLE_NAME
    hint = f"Run `tundravm verify {root}`: witness a release whose evidence holds its lockfile."
    try:
        with tarfile.open(bundle, mode="r:*") as archive:
            index = json.loads(_member(archive, EVIDENCE_INDEX) or b"null")
            entry = index.get("lockfile") if isinstance(index, dict) else None
            member = entry.get("member") if isinstance(entry, dict) else None
            data = None if not isinstance(member, str) else _member(archive, member)
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise ValidationError(
            f"Unreadable evidence bundle {bundle}: {exc}.",
            hint=hint,
            context={"release": str(root)},
        ) from exc
    if data is None:
        raise ValidationError(
            f"The evidence bundle {bundle} holds no lockfile: the release was not cut from "
            "a locked recipe.",
            hint=hint,
            context={"release": str(root)},
        )
    return data


def _member(archive: tarfile.TarFile, name: str) -> bytes | None:
    """The bytes of the plain-file member ``evidence/<name>``, else ``None``."""
    try:
        info = archive.getmember(f"{BUNDLE_ROOT}/{name}")
    except KeyError:
        return None
    if not info.isfile() or info.size > _MEMBER_LIMIT:
        return None
    stream = archive.extractfile(info)
    return None if stream is None else stream.read()


def _parse_lock(data: bytes, root: Path) -> Lock:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockfileError(
            f"The lockfile in {root / BUNDLE_NAME} is not UTF-8 text.",
            hint=f"Run `tundravm verify {root}`: witness a release that verifies.",
            context={"release": str(root)},
        ) from exc
    return Lock.of(parse_lockfile(text))


def _released(recipe: Recipe, index: Mapping[str, Any], root: Path) -> tuple[str, ...]:
    names = tuple(str(name) for name in index.get("variants") or ())
    declared = {variant.name for variant in recipe.variants}
    unknown = [name for name in names if name not in declared]
    if unknown or not names:
        raise ValidationError(
            f"Release {index.get('name')} ships variant(s) this recipe does not declare: "
            f"{', '.join(unknown) or '(none)'}.",
            hint="Check out the commit the release was cut from, then witness again.",
            context={"release": str(root)},
        )
    return names


def _compare(
    index: Mapping[str, Any], found: Mapping[tuple[str, str], tuple[str, str]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _rows(index):
        key = (str(row.get("variant")), str(row.get("target")))
        expected = str(row.get("sha256"))
        actual = found[key][0] if key in found else None
        rows.append(
            {
                "variant": key[0],
                "target": key[1],
                "path": str(row.get("path")),
                "expected": expected,
                "actual": actual,
                "match": actual == expected,
            }
        )
    return rows


def _write(result: Witness, target: Path, sign: Path | None) -> Path | None:
    """Write the witness file to *target*, and with *sign* its ``.sig``; a stale ``.sig``
    of an earlier witness at *target* is removed."""
    text = result.json()
    signature = target.with_name(f"{target.name}.sig")
    signature.unlink(missing_ok=True)
    _write_text(target, text)
    if sign is None:
        return None
    signed = _ssh_sig.sign(text.encode(), Path(sign), namespace=NAMESPACE)
    _write_text(signature, signed.decode())
    return signature


# ── the trust policy ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WitnessCheck:
    """One witness file under the policy: ``counted`` toward it, or not with a reason
    ``code`` (:data:`WITNESS_CODES`); ``principal`` is who its signature verified for."""

    file: str
    builder: str | None
    counted: bool
    message: str
    code: str | None = None
    principal: str | None = None
    simulated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "builder": self.builder,
            "principal": self.principal,
            "counted": self.counted,
            "code": self.code,
            "simulated": self.simulated,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class WitnessVerification:
    """What :func:`verify_witnesses` found: one :class:`WitnessCheck` per witness file.
    :attr:`passed` when at least ``min_witnesses`` distinct principals counted."""

    release: str
    witnesses: str
    min_witnesses: int
    checks: tuple[WitnessCheck, ...]
    allow_simulated: bool = False
    manifest_sha256: str | None = None
    notes: tuple[Mapping[str, str], ...] = ()

    @property
    def counted(self) -> tuple[str, ...]:
        """The distinct principals that count, in file order."""
        return tuple(str(check.principal) for check in self.checks if check.counted)

    @property
    def passed(self) -> bool:
        return len(self.counted) >= self.min_witnesses

    @property
    def verdict(self) -> str:
        return "pass" if self.passed else "fail"

    @property
    def problems(self) -> tuple[Mapping[str, str], ...]:
        """``{"code", "subject", "message"}`` for each witness that did not count, then
        ``witness-insufficient`` when too few did."""
        found = [
            {"code": str(check.code), "subject": check.file, "message": check.message}
            for check in self.checks
            if check.code is not None
        ]
        if not self.passed:
            found.append(
                {
                    "code": "witness-insufficient",
                    "subject": self.witnesses,
                    "message": f"{len(self.counted)} distinct witness(es) count, "
                    f"{self.min_witnesses} required",
                }
            )
        return tuple(found)

    def to_dict(self) -> dict[str, object]:
        """``verify RELEASE_DIR --witnesses DIR --format json``'s ``witnesses``; ``schema``
        is ``tundravm.witness-verify/1``."""
        return {
            "schema": WITNESS_VERIFY_SCHEMA,
            "release": self.release,
            "witnesses": self.witnesses,
            "manifest_sha256": self.manifest_sha256,
            "min_witnesses": self.min_witnesses,
            "allow_simulated": self.allow_simulated,
            "verdict": self.verdict,
            "passed": self.passed,
            "counted": list(self.counted),
            "checks": [check.to_dict() for check in self.checks],
            "problems": [dict(problem) for problem in self.problems],
            "notes": [dict(note) for note in self.notes],
        }

    def render(self, format: WitnessFormat = "text") -> str:
        """``json`` (:meth:`to_dict`) or ``text``: a line per witness, then the problems."""
        if format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        lines = [
            f"witnesses {self.witnesses}  verdict {self.verdict}  "
            f"({len(self.counted)} counted, {self.min_witnesses} required)"
        ]
        for check in self.checks:
            state = "counted" if check.counted else "excluded"
            code = f"{check.code}  " if check.code else ""
            lines.append(f"  {state:<9} {code}{check.file}: {check.message}")
        lines.extend(f"  note      {note['code']}: {note['message']}" for note in self.notes)
        if not self.passed:
            (last,) = self.problems[-1:]
            lines.append(f"  fail      {last['code']}: {last['message']}")
        return "\n".join(lines) + "\n"


def verify_witnesses(
    release_dir: Path,
    witnesses_dir: Path,
    *,
    min_witnesses: int = 1,
    allowed_signers: Path | None = None,
    allow_simulated: bool = False,
) -> WitnessVerification:
    """Apply the witness trust policy to the ``*.json`` files in *witnesses_dir*.

    A witness counts when it references this release (its name and the sha256 of its
    ``release-manifest.json``; else ``witness-wrong-release``), carries a ``.sig``
    (``witness-unsigned``) that verifies in the ``tundravm-witness`` namespace against
    *allowed_signers* for its builder as principal (``witness-signature-invalid``,
    also when no *allowed_signers* is given), is not signed by the publisher
    (``witness-self``: the key ``release-manifest.json.sig`` was made with, read from
    the signature rather than the ``signer`` metadata, a key that metadata names, or the
    publisher's principal), says ``matched`` with
    every released artifact's sha256 (``witness-mismatch``) and was not built with the
    in-process backend unless *allow_simulated* (``witness-simulated``). Distinct
    principals count once, and so does a key listed under two. The policy passes when
    at least *min_witnesses* count (``witness-insufficient`` otherwise). A failed policy
    is a value; a *release_dir* without ``release.json`` raises ``ValidationError``.
    """
    root = Path(release_dir)
    if not is_release(root):
        raise ValidationError(
            f"{root} is not a release directory: it holds no {INDEX_NAME}.",
            hint="Pass a directory `tundravm release` wrote, RELEASES/<recipe>-<version>.",
            context={"path": str(root)},
        )
    if min_witnesses < 0:
        raise ValidationError(
            f"--min-witnesses {min_witnesses} is negative.",
            hint="Pass the number of distinct witnesses the release needs, e.g. 2.",
        )
    directory = Path(witnesses_dir)
    manifest_path = root / MANIFEST_NAME
    manifest_sha = _file_sha256(manifest_path) if manifest_path.is_file() else None
    index = _read_json(root / INDEX_NAME) or {}
    publisher = _publisher(root, index, _read_json(manifest_path) or {})
    notes: list[dict[str, str]] = []
    files = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not directory.is_dir():
        notes.append({"code": "no-witnesses", "message": f"{directory} is not a directory"})
    elif not files:
        notes.append({"code": "no-witnesses", "message": f"{directory} holds no witness file"})
    if allowed_signers is None and files:
        message = "no allowed-signers file: no witness signature is checked (--allowed-signers)"
        notes.append({"code": "signatures-unchecked", "message": message})
    context = _Policy(
        index=index,
        manifest_sha=manifest_sha,
        publisher=publisher,
        allowed=None if allowed_signers is None else Path(allowed_signers),
        allow_simulated=allow_simulated,
    )
    checks: list[WitnessCheck] = []
    principals: dict[str, str] = {}
    keys: dict[str, str] = {}
    for path in files:
        check, key = _judge(path, context)
        principal = check.principal
        if check.counted and principal is not None:
            earlier = principals.get(principal) or (keys.get(key) if key else None)
            if earlier is not None:
                message = f"{principal}: already counted ({earlier})"
                check = replace(check, counted=False, message=message)
            else:
                principals[principal] = path.name
                if key:
                    keys[key] = path.name
        checks.append(check)
    return WitnessVerification(
        release=str(root),
        witnesses=str(directory),
        min_witnesses=min_witnesses,
        checks=tuple(checks),
        allow_simulated=allow_simulated,
        manifest_sha256=manifest_sha,
        notes=tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class _Policy:
    index: Mapping[str, Any]
    manifest_sha: str | None
    publisher: tuple[str | None, frozenset[str]]
    allowed: Path | None
    allow_simulated: bool


def _publisher(
    root: Path, index: Mapping[str, Any], manifest: Mapping[str, Any]
) -> tuple[str | None, frozenset[str]]:
    """The release signer's principal and key fingerprints (``None`` and none for an
    unsigned release). The key is the one ``release-manifest.json.sig`` was made with,
    read from the signature ``verify_release`` authenticates, so false ``signer``
    metadata cannot pass the publisher's own witness off as independent; keys the
    metadata claims are excluded too (``verify_release`` rejects a contradiction)."""
    records = (index.get("signer"), manifest.get("signer"))
    principal: str | None = None
    for recorded in records:
        if isinstance(recorded, Mapping):
            principal = str(recorded.get("principal") or SIGNER)
    key = signing_key(root)
    if key is not None and principal is None:
        principal = SIGNER
    claimed = recorded_keys(*records)
    return principal, claimed if key is None else claimed | {key}


def _judge(path: Path, policy: _Policy) -> tuple[WitnessCheck, str | None]:
    """The check of the witness file at *path*, and the fingerprint of its signing key."""
    payload = _read_json(path)
    if payload is None or payload.get("schema") != WITNESS_SCHEMA:
        message = f"not a {WITNESS_SCHEMA} file"
        return _excluded(path, None, "witness-wrong-release", message), None
    builder = payload.get("builder")
    who = str(builder) if isinstance(builder, str) else None
    index = policy.index
    for field_name in ("release", "recipe_digest", "lock_sha256"):
        mine = index.get("name" if field_name == "release" else field_name)
        if payload.get(field_name) != mine:
            theirs = payload.get(field_name)
            message = f"it witnesses {field_name} {theirs!r}, this release's is {mine!r}"
            return _excluded(path, who, "witness-wrong-release", message), None
    if payload.get("manifest_sha256") != policy.manifest_sha:
        theirs = str(payload.get("manifest_sha256"))[:12]
        message = (
            f"it witnesses manifest {theirs}, this release's "
            f"{MANIFEST_NAME} is {str(policy.manifest_sha)[:12]}"
        )
        return _excluded(path, who, "witness-wrong-release", message), None
    signature = path.with_name(f"{path.name}.sig")
    if not signature.is_file():
        message = f"{who}: no {signature.name}"
        return _excluded(path, who, "witness-unsigned", message), None
    if policy.allowed is None or who is None:
        why = "no allowed-signers file" if policy.allowed is None else "it names no builder"
        message = f"{who}: the signature is not checked: {why}"
        return _excluded(path, who, "witness-signature-invalid", message), None
    raw = signature.read_bytes()
    key = _ssh_sig.signature_fingerprint(raw)
    principal, publisher_keys = policy.publisher
    if (principal is not None and who == principal) or (key is not None and key in publisher_keys):
        message = f"{who}: signed by the release's own signer: a publisher cannot witness itself"
        return _excluded(path, who, "witness-self", message, principal=who), key
    result = _ssh_sig.verify(
        path.read_bytes(), raw, policy.allowed, signer=who, namespace=NAMESPACE
    )
    if not result.valid:
        message = f"{who}: the signature does not verify: {result.message}"
        return _excluded(path, who, "witness-signature-invalid", message), None
    mismatched = _mismatched(payload, index)
    if mismatched:
        message = f"{who}: verdict {payload.get('verdict')}, {mismatched}"
        return _excluded(path, who, "witness-mismatch", message, principal=who), key
    simulated = bool(payload.get("simulated"))
    total = len(_rows(index))
    if simulated and not policy.allow_simulated:
        message = f"{who}: built with the in-process backend: nothing was built (--allow-simulated)"
        return _excluded(path, who, "witness-simulated", message, principal=who, sim=True), key
    tag = ", simulated" if simulated else ""
    message = f"{who}: matched {total}/{total} artifacts, signed by {who}{tag}"
    check = WitnessCheck(path.name, who, True, message, principal=who, simulated=simulated)
    return check, key


def _excluded(
    path: Path,
    builder: str | None,
    code: str,
    message: str,
    *,
    principal: str | None = None,
    sim: bool = False,
) -> WitnessCheck:
    return WitnessCheck(path.name, builder, False, message, code, principal, sim)


def _mismatched(payload: Mapping[str, Any], index: Mapping[str, Any]) -> str:
    """Why *payload* does not vouch for every artifact *index* lists; empty when it does."""
    rows = payload.get("artifacts")
    theirs = {
        (str(row.get("variant")), str(row.get("target"))): row
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, Mapping)
    }
    off: list[str] = []
    for row in _rows(index):
        key = (str(row.get("variant")), str(row.get("target")))
        seen = theirs.get(key)
        sha = row.get("sha256")
        if (
            seen is None
            or seen.get("match") is not True
            or seen.get("expected") != sha
            or seen.get("actual") != sha
        ):
            off.append(f"{key[0]}/{key[1]}")
    if payload.get("verdict") != "matched" and not off:
        return "not matched"
    return f"{len(off)} artifact(s) differ: {', '.join(off)}" if off else ""


# ── files ────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically: a sibling temporary file renamed over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, scratch = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(scratch, 0o644)
        os.replace(scratch, path)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


__all__ = [
    "NAMESPACE",
    "WITNESSES_SUFFIX",
    "WITNESS_CODES",
    "WITNESS_FORMATS",
    "WITNESS_SCHEMA",
    "WITNESS_VERDICTS",
    "WITNESS_VERIFY_SCHEMA",
    "Witness",
    "WitnessCheck",
    "WitnessVerification",
    "verify_witnesses",
    "witness",
    "witness_image",
    "witnesses_dir",
]
