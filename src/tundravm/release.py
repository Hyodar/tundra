"""Cut a release: one immutable directory from a locked recipe, built, measured and evidenced.

:func:`release` runs the stages of :data:`STAGES` in order: ``lock-check`` (refuse
drift; the lockfile is never updated), ``fetch``, ``bake`` (with a second build that
must match), ``measure`` (a verifier policy per variant), ``sbom`` (SPDX per
artifact), ``evidence`` (bundle, HTML page, optional signature), ``verify`` (the
bundle just written), ``changes`` (against the previous release) and ``notes``
(``RELEASE.md``). Everything lands in ``RELEASES/<recipe>-<version>/``, which ends
with ``release.json`` and ``release-manifest.json`` (every file with its sha256 and
size; signed as ``release-manifest.json.sig`` with the release's key).
:func:`verify_release` checks such a directory on its own.

Until it is finished the directory holds ``release.state.json``: the release's
specification (variants, requirements, signing key, since, backend, build directory,
attestation records and their maximum age)
and each stage with the sha256 of its inputs and of its outputs. Running the release
again inherits every option it omits from that specification, refuses a changed one
unless ``change_spec`` says so (recorded in ``release.json``), keeps every stage whose
inputs and outputs are unchanged and runs the first stage whose inputs changed and
every stage after it. A release that passes is finished: the state goes and the
directory is immutable. One that fails keeps its state, so it resumes once the cause
is fixed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from . import _ssh_sig
from .backends.base import BuildBackend, backend_fingerprint
from .changes import changes
from .declarative._bake import package_manifest_candidates
from .declarative._lowered import Lowered
from .declarative.bom import sbom
from .declarative.lifecycle import (
    INPROCESS,
    MANIFEST_KEY,
    Artifact,
    Backend,
    Lock,
    bake_image,
    fetch_image,
    lock_status,
    measure,
    read_artifacts,
    read_lock,
    recorded_reproducibility,
    select_artifact,
    verify_artifact,
)
from .declarative.lower import lower
from .declarative.model import Recipe
from .errors import (
    ArtifactError,
    LockfileError,
    MeasurementError,
    ReproducibilityError,
    StateError,
    TdxError,
    ValidationError,
)
from .evidence import DEFAULT_MAX_AGE, _requirements, demands, evidence
from .formats import md_cell, md_table
from .measure.policy import write_policy
from .models import BAKE_RESULT_FILENAME
from .verify import EvidenceVerification, VerifyCheck, verify_evidence

RELEASE_SCHEMA = "tundravm.release/1"
"""The ``schema`` of ``release.json``, which ``release --format json`` prints."""
STATE_SCHEMA = "tundravm.release-state/1"
"""The ``schema`` of ``release.state.json``."""
MANIFEST_SCHEMA = "tundravm.release-manifest/1"
"""The ``schema`` of ``release-manifest.json``."""
RELEASE_VERIFY_SCHEMA = "tundravm.release-verify/1"
"""The ``schema`` of :meth:`ReleaseVerification.to_dict` (``verify RELEASE_DIR --format json``)."""
INDEX_NAME = "release.json"
STATE_NAME = "release.state.json"
NOTES_NAME = "RELEASE.md"
BUNDLE_NAME = "evidence.tar.gz"
HTML_NAME = "evidence.html"
MANIFEST_NAME = "release-manifest.json"
MANIFEST_SIGNATURE_NAME = "release-manifest.json.sig"
SIGNER = "release"
"""The principal the ``verify`` stage checks the signature for, with the signing key's
public half."""
STAGES: tuple[str, ...] = (
    "lock-check",
    "fetch",
    "bake",
    "measure",
    "sbom",
    "evidence",
    "verify",
    "changes",
    "notes",
)
"""Every stage of a release, in the order they run."""
SPEC_KEYS: tuple[str, ...] = (
    "variants",
    "require",
    "signing",
    "since",
    "backend",
    "build",
    "allow_ineligible",
    "attestations",
    "max_age",
)
"""What a release's specification holds; a resumed run inherits each key it omits."""
ReleaseFormat = Literal["text", "json"]
RELEASE_FORMATS: tuple[ReleaseFormat, ...] = ("text", "json")
StageExecution = Literal["completed", "skipped", "failed"]
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_ABSENT = "absent"
"""The sha256 a watched file that did not exist is recorded with."""


@dataclass(frozen=True, slots=True)
class ReleaseStage:
    """One stage of a release. ``execution`` is ``completed``, ``skipped`` (it did not
    apply, ``summary`` says why) or ``failed`` (it raised; only the state records it);
    ``verdict`` is what the stage concluded (``evidence``, ``verify`` and ``notes``:
    ``pass``, ``fail`` or ``incomplete``; ``None`` for the others). ``resumed`` when an
    earlier run completed it with the same inputs. ``inputs`` is the sha256 of what it
    read, ``outputs`` the sha256 of each file it wrote into the release directory."""

    name: str
    execution: StageExecution
    started: str
    finished: str
    seconds: float
    inputs: str
    outputs: Mapping[str, str] = field(default_factory=dict)
    summary: str = ""
    verdict: str | None = None
    resumed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "execution": self.execution,
            "verdict": self.verdict,
            "resumed": self.resumed,
            "started": self.started,
            "finished": self.finished,
            "seconds": round(self.seconds, 3),
            "inputs": self.inputs,
            "outputs": dict(sorted(self.outputs.items())),
            "summary": self.summary,
        }

    def line(self, number: int) -> str:
        """``[3/9] bake        done   34.1s  reproducible: matched``."""
        shown = {"completed": "done"}.get(self.execution, self.execution)
        if self.resumed and self.execution == "completed":
            shown = "kept"
        took = "" if self.resumed else f"{self.seconds:.1f}s"
        head = f"[{number}/{len(STAGES)}] {self.name:<10}  {shown:<7} {took:>6}"
        return f"{head}  {self.summary}".rstrip()


@dataclass(frozen=True, slots=True)
class Release:
    """A release directory and what made it; :meth:`to_dict` is its ``release.json``.

    ``verdict`` is ``pass`` only when the evidence and its verification pass, every
    ``require`` check included; ``reasons`` say why not. ``finished`` when it passed:
    the directory is then immutable. ``spec`` is the specification it was cut with
    and ``spec_history`` each change a resumed run made to it (``change_spec``);
    ``skipped`` names the declared variants it does not release, and why.
    """

    name: str
    version: str
    path: Path
    recipe: str
    recipe_digest: str
    lock_sha256: str
    variants: tuple[str, ...]
    backend: str
    simulated: bool
    artifacts: tuple[Mapping[str, str], ...]
    stages: tuple[ReleaseStage, ...]
    verdict: str
    reasons: tuple[Mapping[str, Any], ...] = ()
    required: tuple[str, ...] = ()
    since: str | None = None
    signer: Mapping[str, str] | None = None
    reproducibility: str | None = None
    created: str = ""
    spec: Mapping[str, Any] = field(default_factory=dict)
    spec_history: tuple[Mapping[str, Any], ...] = ()
    skipped: tuple[Mapping[str, str], ...] = ()
    allow_ineligible: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def finished(self) -> bool:
        """Whether the release is final (it passed): a second run of its version refuses."""
        return self.passed

    def to_dict(self) -> dict[str, object]:
        """``release.json``: ``schema`` is ``tundravm.release/1``."""
        from tundravm import __version__

        return {
            "schema": RELEASE_SCHEMA,
            "name": self.name,
            "recipe": self.recipe,
            "version": self.version,
            "created": self.created,
            "tundravm": __version__,
            "recipe_digest": self.recipe_digest,
            "lock_sha256": self.lock_sha256,
            "variants": list(self.variants),
            "skipped": [dict(row) for row in self.skipped],
            "allow_ineligible": self.allow_ineligible,
            "backend": self.backend,
            "simulated": self.simulated,
            "reproducibility": self.reproducibility,
            "required": list(self.required),
            "since": self.since,
            "signer": None if self.signer is None else dict(self.signer),
            "spec": dict(self.spec),
            "spec_history": [dict(entry) for entry in self.spec_history],
            "artifacts": [dict(row) for row in self.artifacts],
            "stages": [stage.to_dict() for stage in self.stages],
            "verdict": self.verdict,
            "reasons": [dict(reason) for reason in self.reasons],
            "finished": self.finished,
            "manifest": MANIFEST_NAME,
        }

    def render(self, format: ReleaseFormat = "text") -> str:
        """``json`` (``release.json``) or ``text``: each reason, then the release path and
        its verdict."""
        if format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        lines = [
            f"  {reason['source']:<8}  {reason['code']}  {reason['subject'] or '-'}: "
            f"{reason['message']}"
            for reason in self.reasons
        ]
        tag = " (simulated)" if self.simulated else ""
        lines.append(f"release {self.path}: {self.verdict}{tag}")
        return "\n".join(lines) + "\n"


# ── the run ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Run:
    """What every stage reads, and the facts the stages before it recorded."""

    recipe: Recipe
    img: Lowered
    names: tuple[str, ...]
    version: str
    root: Path
    build: Path
    locked: Lock
    lock_path: Path | None
    lock_sha256: str
    recipe_digest: str
    backend: BuildBackend
    fingerprint: dict[str, Any]
    require: tuple[str, ...]
    sign: Path | None
    public_key: str | None
    since: Path | None
    since_label: str | None
    recipe_path: Path | None
    attestations: tuple[Path, ...] = ()
    max_age: int = DEFAULT_MAX_AGE
    spec: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdict: str = "pass"
    reasons: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Outcome:
    execution: StageExecution
    summary: str
    outputs: dict[str, str] = field(default_factory=dict)
    watched: dict[str, str] = field(default_factory=dict)
    """Files outside the release directory a later stage reads, by absolute path (sha256,
    or ``absent`` for one that did not exist)."""
    facts: dict[str, Any] = field(default_factory=dict)
    verdict: str | None = None


def release(
    recipe: Recipe,
    *,
    version: str,
    backend: Backend | BuildBackend,
    variants: Sequence[str] | None = None,
    sign: Path | None = None,
    unsigned: bool = False,
    require: Sequence[str] | None = None,
    since: Path | None = None,
    releases: Path = Path("releases"),
    build: Path = Path("build"),
    lock: Lock | Path | None = None,
    restart: bool = False,
    change_spec: bool = False,
    allow_ineligible: bool = False,
    recipe_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
    attestations: Sequence[Path] | None = None,
    max_age: float | timedelta | None = None,
) -> Release:
    """Release *variants* of *recipe* as ``releases/<recipe.name>-<version>``.

    Runs :data:`STAGES` against *lock* (a :class:`Lock` or a lockfile path; default the
    recipe's ``build/tundravm.lock``, which must exist), baking with *backend* into the
    build directory *build*. *variants* defaults to every variant eligible for release
    (``Variant(release=True)``); naming an ineligible one needs *allow_ineligible*.
    *sign* is an SSH private key that signs the evidence and the release manifest;
    ``verify`` then checks the signature with its public half. *require* names checks
    of :data:`~tundravm.evidence.EVIDENCE_REQUIREMENTS` the evidence must hold. *since*
    is the previous release (its directory, or an evidence bundle) the changes are
    against; default: the newest finished release of the recipe in *releases*.
    *recipe_path* is recorded in the evidence. *progress* receives one line per stage.
    *attestations* are ``tundravm.attestation/1`` records (``attest --record``) the
    evidence bundles, and *max_age* (seconds or a ``timedelta``; default a day) how far
    their ``observed_at`` may be from the moment the evidence stage judges them (its wall
    clock, never ``SOURCE_DATE_EPOCH``) for ``attested``, which also requires every
    selected artifact ``measured``: an image is attested after it is baked, so a release
    that requires ``attested`` fails until a resumed run gives the records of its
    artifacts.

    The release directory keeps ``release.state.json`` until the release passes, so a
    run that failed or was interrupted resumes. The state holds the specification the
    release started with (:data:`SPEC_KEYS`): a resumed run inherits *variants*,
    *require*, *sign*, *since*, *allow_ineligible*, *attestations* and *max_age* when
    they are omitted (``None``, ``False``; the attestation records must still hash as
    recorded), the first run that gives *attestations* or *max_age* records them, and
    a value that differs from the recorded one, the *backend*'s
    settings and the *build* directory included, raises ``StateError`` unless
    *change_spec* is true; *unsigned* drops the signing key (a change too). The change
    is recorded in ``release.json``'s ``spec_history``. Stages whose inputs are
    unchanged are kept, and the first stage whose inputs changed runs with every stage
    after it. *restart* discards that state. A finished release refuses
    (``StateError``): bump the version. A drifted lockfile is a ``LockfileError``; a
    release never relocks.
    """
    built = backend.build_backend() if isinstance(backend, Backend) else backend
    img = replace(lower(recipe), backend=built)
    return release_image(
        recipe,
        img,
        version=version,
        variants=variants,
        sign=sign,
        unsigned=unsigned,
        require=require,
        since=since,
        releases=releases,
        build=build,
        lock=lock,
        restart=restart,
        change_spec=change_spec,
        allow_ineligible=allow_ineligible,
        recipe_path=recipe_path,
        progress=progress,
        attestations=attestations,
        max_age=max_age,
    )


def release_image(
    recipe: Recipe,
    img: Lowered,
    *,
    version: str,
    variants: Sequence[str] | None = None,
    sign: Path | None = None,
    unsigned: bool = False,
    require: Sequence[str] | None = None,
    since: Path | None = None,
    releases: Path = Path("releases"),
    build: Path = Path("build"),
    lock: Lock | Path | None = None,
    restart: bool = False,
    change_spec: bool = False,
    allow_ineligible: bool = False,
    recipe_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
    attestations: Sequence[Path] | None = None,
    max_age: float | timedelta | None = None,
) -> Release:
    """:func:`release` for a recipe already lowered as *img*, which carries the backend."""
    if img.backend is None:
        raise ValidationError(
            "No build backend to release with.",
            hint="Pass --backend KIND (release(backend=...)), or bind `backend` in the "
            "recipe file.",
        )
    if sign is not None and unsigned:
        raise ValidationError(
            "A release cannot be signed (--sign) and unsigned (--no-sign) at once.",
            hint="Pass one of them.",
        )
    _check_version(version)
    name = release_name(recipe.name, version)
    root = Path(releases) / name
    previous = _read_state(root, name, restart=restart)
    recorded = None if previous is None else previous.get("spec")
    given: dict[str, Any] = {
        "backend": backend_fingerprint(img.backend),
        "build": str(Path(build).resolve()),
    }
    if variants:
        given["variants"] = list(_variants(recipe, variants))
    if require is not None:
        given["require"] = list(_requirements(require))
    if unsigned:
        given["signing"] = None
    elif sign is not None:
        given["signing"] = _signing(Path(sign))
    if allow_ineligible:
        given["allow_ineligible"] = True
    if since is not None:
        given["since"] = _since(Path(since))
    if attestations is not None:
        given["attestations"] = [_attestation(Path(path)) for path in attestations]
    if max_age is not None:
        seconds = max_age.total_seconds() if isinstance(max_age, timedelta) else max_age
        given["max_age"] = int(seconds)
    locked, lock_path, lock_bytes = _lock(img, lock)
    spec, changed = _merge_spec(
        recorded if isinstance(recorded, dict) else None, given, change_spec, name
    )
    _complete_spec(spec, recipe, Path(releases), version)
    names, skipped = _eligible(recipe, spec)
    signing = spec["signing"]
    if signing is not None and "signing" not in given:
        _check_inherited_key(signing, name)
    if "attestations" not in given:
        _check_inherited_attestations(spec.get("attestations") or (), name)
    _prepare(root, restart=restart)
    state = previous if previous is not None else _fresh_state(name)
    state["spec"] = spec
    history: list[dict[str, Any]] = list(state.get("spec_history") or [])
    if changed:
        history.append({"at": _now(), "changes": changed})
    state["spec_history"] = history
    _save(root, state)
    since_spec = spec["since"]
    run = _Run(
        recipe=recipe,
        img=img,
        names=names,
        version=version,
        root=root,
        build=Path(build),
        locked=locked,
        lock_path=lock_path,
        lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        recipe_digest=img.select(tuple(img.state.profiles)).digest(),
        backend=img.backend,
        fingerprint=dict(spec["backend"]),
        require=tuple(spec["require"]),
        sign=None if signing is None else Path(signing["key"]),
        public_key=None if signing is None else str(signing["public_key"]),
        since=None if since_spec is None else Path(since_spec["path"]),
        since_label=None if since_spec is None else str(since_spec["label"]),
        recipe_path=recipe_path,
        attestations=tuple(Path(row["path"]) for row in spec.get("attestations") or ()),
        max_age=int(spec.get("max_age") or DEFAULT_MAX_AGE),
        spec=spec,
        history=history,
        skipped=skipped,
    )
    if progress is not None:
        for row in skipped:
            progress(f"skip variant {row['variant']}: {row['reason']}")
    stages = _run_stages(run, state, progress)
    result = _result(run, stages)
    _write_text(root / INDEX_NAME, result.render("json"))
    _write_manifest(run, result)
    if result.passed:
        (root / STATE_NAME).unlink(missing_ok=True)
    return result


def release_name(recipe: str, version: str) -> str:
    """The release directory's name: ``<recipe>-<version>``."""
    return f"{recipe}-{version}"


def _check_version(version: str) -> None:
    if not _VERSION.fullmatch(version):
        raise ValidationError(
            f"Release version {version!r} is not a version.",
            hint="Use letters, digits, '.', '_', '+' and '-', starting with a letter or "
            "digit, e.g. --version 1.4.0.",
        )


def _variants(recipe: Recipe, wanted: Sequence[str]) -> tuple[str, ...]:
    declared = tuple(v.name for v in recipe.variants)
    unknown = [name for name in wanted if name not in declared]
    if unknown:
        raise ValidationError(
            f"Unknown variant(s): {', '.join(unknown)}.",
            hint=f"Declared variants: {', '.join(declared)}",
        )
    return tuple(dict.fromkeys(wanted))


# ── the specification ────────────────────────────────────────────────────


def _signing(key: Path) -> dict[str, str]:
    """The spec's ``signing``: the private key's path, its public half and fingerprint."""
    public = _ssh_sig.public_key(key)
    return {"key": str(key), "public_key": public, "fingerprint": _ssh_sig.fingerprint(public)}


def _since(given: Path) -> dict[str, str]:
    """The spec's ``since``: the evidence the changes compare with, and how to name it."""
    if given.is_dir() and (given / BUNDLE_NAME).is_file():
        return {"path": str(given / BUNDLE_NAME), "label": given.name}
    return {"path": str(given), "label": given.name}


def _attestation(path: Path) -> dict[str, str]:
    """The spec's entry for an attestation record: its absolute path and sha256."""
    if not path.is_file():
        raise ValidationError(
            f"Attestation record {path} does not exist.",
            hint="Write one with `tundravm attest --policy POLICY --record FILE`.",
            context={"attestation": str(path)},
        )
    return {"path": str(path.resolve()), "sha256": _file_sha256(path)}


def _check_inherited_attestations(rows: Sequence[Mapping[str, Any]], name: str) -> None:
    """Each attestation record the release inherits must still be there, unchanged."""
    for row in rows:
        path = Path(str(row.get("path")))
        if not path.is_file() or _file_sha256(path) != row.get("sha256"):
            state = "is gone" if not path.is_file() else "has changed"
            raise StateError(
                f"Release {name} bundles the attestation record {path}, which {state}.",
                hint="Restore the record, or pass --change-spec with --attestation FILE "
                "for each record the release should bundle now.",
                context={"release": name, "attestation": str(path)},
            )


def _comparable(key: str, value: Any) -> Any:
    """*value* as a change of *key* is judged: a signing key by its public half alone."""
    if key == "signing" and isinstance(value, Mapping):
        return {"public_key": value.get("public_key"), "fingerprint": value.get("fingerprint")}
    return value


def _merge_spec(
    recorded: dict[str, Any] | None, given: dict[str, Any], change_spec: bool, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The spec of this run: *recorded* (the release's, ``None`` when it starts) with what
    was *given*, and each recorded key whose value changed (``{"old", "new"}``).

    A change needs *change_spec*: dropping a ``require`` check or the signing key weakens
    what the release accepts, and changing anything else rebuilds what it vouches for.
    """
    if recorded is None:
        return dict(given), {}
    spec = dict(recorded)
    changed: dict[str, Any] = {}
    for key, value in given.items():
        if key in recorded and _comparable(key, recorded[key]) != _comparable(key, value):
            changed[key] = {
                "old": _public_value(key, recorded[key]),
                "new": _public_value(key, value),
            }
        spec[key] = value
    if changed and not change_spec:
        listed = "; ".join(
            f"{key} {json.dumps(diff['old'], sort_keys=True)} -> "
            f"{json.dumps(diff['new'], sort_keys=True)}"
            for key, diff in changed.items()
        )
        raise StateError(
            f"Release {name} was started with another specification: {listed}.",
            hint="Omit the option to keep what the release started with, or pass "
            "--change-spec (release(change_spec=True)) to change it; the change is "
            "recorded in release.json's spec_history.",
            context={"release": name, "changed": ", ".join(changed)},
        )
    return spec, changed


def _complete_spec(spec: dict[str, Any], recipe: Recipe, releases: Path, version: str) -> None:
    """Fill what neither the recorded spec nor this run gave: the defaults of a new release."""
    spec.setdefault("require", [])
    spec.setdefault("signing", None)
    spec.setdefault("allow_ineligible", False)
    if "variants" not in spec:
        spec["variants"] = [v.name for v in recipe.variants if v.release]
    if "since" not in spec:
        found = previous_release(releases, recipe.name, exclude=version)
        spec["since"] = None if found is None else _since(found)


def _eligible(
    recipe: Recipe, spec: Mapping[str, Any]
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """The variants the spec releases, and every other declared one with why it is left out."""
    names = tuple(str(name) for name in spec["variants"])
    declared = {v.name: v for v in recipe.variants}
    unknown = [name for name in names if name not in declared]
    if unknown:
        raise ValidationError(
            f"Unknown variant(s): {', '.join(unknown)}.",
            hint=f"Declared variants: {', '.join(declared)}",
        )
    if not names:
        raise ValidationError(
            f"Recipe {recipe.name!r} has no variant eligible for release: every one sets "
            "release=False.",
            hint="Mark the variant to ship Variant(..., release=True), or name one with "
            "--variant NAME --allow-ineligible.",
        )
    ineligible = [name for name in names if not declared[name].release]
    if ineligible and not spec["allow_ineligible"]:
        listed = ", ".join(ineligible)
        raise ValidationError(
            f"Variant(s) {listed} are not eligible for release (Variant(release=False)).",
            hint="Drop them from --variant, or pass --allow-ineligible to release them "
            "anyway (recorded in release.json).",
            context={"variants": listed},
        )
    skipped = [
        {
            "variant": name,
            "reason": "not selected (--variant)"
            if variant.release
            else "Variant(release=False): not for release",
        }
        for name, variant in declared.items()
        if name not in names
    ]
    return names, skipped


def _check_inherited_key(signing: Mapping[str, Any], name: str) -> None:
    """The recorded signing key must still be there, and still be the same key."""
    key = Path(str(signing.get("key")))
    if not key.is_file():
        raise StateError(
            f"Release {name} is signed with {key} ({signing.get('fingerprint')}), "
            "which no longer exists.",
            hint="Pass --sign KEY with the same key, or --change-spec --no-sign "
            "(or another --sign KEY) to change it.",
            context={"release": name, "key": str(key)},
        )
    if _ssh_sig.public_key(key) != signing.get("public_key"):
        raise StateError(
            f"The key at {key} is no longer release {name}'s signing key "
            f"({signing.get('fingerprint')}).",
            hint="Pass --sign KEY with the original key, or --change-spec --sign KEY to "
            "sign with this one.",
            context={"release": name, "key": str(key)},
        )


def _public_value(key: str, value: Any) -> Any:
    """*value* as ``release.json`` shows it: a signing key without its private path."""
    if key == "signing" and isinstance(value, Mapping):
        return {"public_key": value.get("public_key"), "fingerprint": value.get("fingerprint")}
    return value


def _public_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The spec as ``release.json`` records it: no private key path, no build directory."""
    return {
        key: _public_value(key, spec[key]) for key in SPEC_KEYS if key in spec and key != "build"
    }


# ── the release directory ────────────────────────────────────────────────


def _fresh_state(name: str) -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "release": name, "spec": None, "spec_history": [], "stages": {}}


def _read_state(root: Path, name: str, *, restart: bool) -> dict[str, Any] | None:
    """The state of the release at *root*: refused when finished or not a release, ``None``
    when there is none (or *restart* discards it). Changes nothing on disk."""
    state_path = root / STATE_NAME
    if (root / INDEX_NAME).is_file() and not state_path.is_file():
        raise StateError(
            f"Release {name} is finished ({root}); a finished release is immutable.",
            hint="Bump the version (--version) to cut a new release.",
            context={"release": str(root)},
        )
    if root.exists() and not state_path.is_file() and any(root.iterdir()):
        raise StateError(
            f"{root} exists and is not a release in progress.",
            hint="Remove it, or release into another --releases directory.",
            context={"release": str(root)},
        )
    if restart or not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("schema") != STATE_SCHEMA or state.get("release") != name:
            raise ValueError(f"not the state of {name}")
        if not isinstance(state.get("stages"), dict):
            raise ValueError("no stages")
    except (OSError, ValueError, AttributeError) as exc:
        raise StateError(
            f"Unreadable release state {state_path}: {exc}.",
            hint="Run the release again with --restart (release(restart=True)).",
            context={"path": str(state_path)},
        ) from exc
    return dict(state)


def _prepare(root: Path, *, restart: bool) -> None:
    if restart and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)


def _lock(img: Lowered, given: Lock | Path | None) -> tuple[Lock, Path | None, bytes]:
    if isinstance(given, Lock):
        return given, None, given.text().encode()
    path = Path(given) if given is not None else img.lock_path
    if not path.is_file():
        raise LockfileError(
            f"No lockfile at {path}: a release is cut from a locked recipe.",
            hint="Run `tundravm lock RECIPE` and commit the lockfile, then release again.",
            context={"path": str(path)},
        )
    return read_lock(path), path, path.read_bytes()


def previous_release(releases: Path, recipe: str, *, exclude: str | None = None) -> Path | None:
    """The newest finished release of *recipe* under *releases* (not version *exclude*).

    Newest by when it finished, then by version (digit runs compared as numbers).
    """
    found: list[tuple[str, tuple[tuple[int, int | str], ...], Path]] = []
    if not Path(releases).is_dir():
        return None
    for path in Path(releases).iterdir():
        index = path / INDEX_NAME
        if not index.is_file() or (path / STATE_NAME).exists():
            continue
        try:
            payload = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != RELEASE_SCHEMA:
            continue
        if payload.get("recipe") != recipe or payload.get("version") == exclude:
            continue
        if not payload.get("finished") or not (path / BUNDLE_NAME).is_file():
            continue
        stamp = str(payload.get("created") or "")
        found.append((stamp, _version_key(str(payload.get("version"))), path))
    return max(found)[2] if found else None


def releases_in_progress(releases: Path, recipe: str) -> list[dict[str, Any]]:
    """Each unfinished release of *recipe* under *releases* (a ``release.state.json`` beside
    it), oldest version first: ``name``, ``version``, ``path``, ``spec`` (as recorded),
    ``completed`` (the stages kept so far, in order), ``failed`` (the stage that raised, or
    ``None``), ``verdict`` and ``reasons`` (the distinct codes of its last ``release.json``,
    when a run got that far). Reads only; an unreadable state is skipped."""
    found: list[tuple[tuple[tuple[int, int | str], ...], dict[str, Any]]] = []
    if not Path(releases).is_dir():
        return []
    prefix = f"{recipe}-"
    for path in Path(releases).iterdir():
        try:
            state = json.loads((path / STATE_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
            continue
        name, stages = state.get("release"), state.get("stages")
        if not isinstance(stages, dict):
            continue
        if not isinstance(name, str) or not name.startswith(prefix) or name != path.name:
            continue
        version = name[len(prefix) :]
        recorded = [stages[stage] for stage in STAGES if isinstance(stages.get(stage), dict)]
        index = _read_index(path / INDEX_NAME)
        listed = index.get("reasons")
        reasons: list[Any] = listed if isinstance(listed, list) else []
        found.append(
            (
                _version_key(version),
                {
                    "name": name,
                    "version": version,
                    "path": str(path),
                    "spec": state.get("spec") if isinstance(state.get("spec"), dict) else {},
                    "completed": [
                        str(entry.get("name"))
                        for entry in recorded
                        if entry.get("execution") in ("completed", "skipped")
                    ],
                    "failed": next(
                        (
                            str(entry.get("name"))
                            for entry in recorded
                            if entry.get("execution") == "failed"
                        ),
                        None,
                    ),
                    "verdict": index.get("verdict"),
                    "reasons": list(
                        dict.fromkeys(
                            str(reason.get("code"))
                            for reason in reasons
                            if isinstance(reason, dict)
                        )
                    ),
                },
            )
        )
    return [row for _, row in sorted(found, key=lambda pair: pair[0])]


def _read_index(path: Path) -> dict[str, Any]:
    """``release.json`` at *path*, or ``{}`` when there is none or it is unreadable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", version)
        if part
    )


# ── stages ───────────────────────────────────────────────────────────────


def _run_stages(
    run: _Run, state: dict[str, Any], progress: Callable[[str], None] | None
) -> tuple[ReleaseStage, ...]:
    recorded: dict[str, Any] = state["stages"]
    stages: list[ReleaseStage] = []
    upstream = ""
    rerun = False
    for number, name in enumerate(STAGES, 1):
        if name == "notes":
            run.verdict, run.reasons = _verdict(run)
        inputs = _sha256_json({"stage": name, "params": _params(run, name), "upstream": upstream})
        entry = recorded.get(name)
        fresh = (
            isinstance(entry, dict)
            and entry.get("inputs") == inputs
            and entry.get("execution") in ("completed", "skipped")
        )
        if not rerun and isinstance(entry, dict) and fresh and _intact(entry, run.root):
            stage = _kept(name, entry)
            run.facts[name] = dict(entry.get("facts") or {})
        else:
            rerun = True
            for later in STAGES[number - 1 :]:
                recorded.pop(later, None)
            _save(run.root, state)
            try:
                stage, entry = _execute(run, name, inputs)
            except Exception as exc:
                failed = _failed(name, inputs, exc)
                recorded[name] = failed.to_dict()
                _save(run.root, state)
                if progress is not None:
                    progress(failed.line(number))
                raise
            recorded[name] = entry
            _save(run.root, state)
        upstream = _sha256_json(
            {"inputs": inputs, "outputs": entry.get("outputs"), "facts": entry.get("facts")}
        )
        stages.append(stage)
        if progress is not None:
            progress(stage.line(number))
    return tuple(stages)


def _execute(run: _Run, name: str, inputs: str) -> tuple[ReleaseStage, dict[str, Any]]:
    started = _now()
    clock = time.monotonic()
    outcome = _STAGE_RUNNERS[name](run)
    seconds = time.monotonic() - clock
    run.facts[name] = outcome.facts
    stage = ReleaseStage(
        name=name,
        execution=outcome.execution,
        started=started,
        finished=_now(),
        seconds=seconds,
        inputs=inputs,
        outputs=dict(outcome.outputs),
        summary=outcome.summary,
        verdict=outcome.verdict,
    )
    entry = {
        **stage.to_dict(),
        "watched": dict(sorted(outcome.watched.items())),
        "facts": outcome.facts,
    }
    return stage, entry


def _failed(name: str, inputs: str, exc: Exception) -> ReleaseStage:
    """The record of stage *name* that raised *exc*: never kept, so a resume runs it again."""
    message = str(exc.args[0]) if isinstance(exc, TdxError) and exc.args else str(exc)
    when = _now()
    return ReleaseStage(
        name=name,
        execution="failed",
        started=when,
        finished=when,
        seconds=0.0,
        inputs=inputs,
        summary=f"{type(exc).__name__}: {message}",
    )


def _kept(name: str, entry: Mapping[str, Any]) -> ReleaseStage:
    verdict = entry.get("verdict")
    return ReleaseStage(
        name=name,
        execution="skipped" if entry.get("execution") == "skipped" else "completed",
        started=str(entry.get("started") or ""),
        finished=str(entry.get("finished") or ""),
        seconds=float(entry.get("seconds") or 0.0),
        inputs=str(entry.get("inputs")),
        outputs=dict(entry.get("outputs") or {}),
        summary=str(entry.get("summary") or ""),
        verdict=None if verdict is None else str(verdict),
        resumed=True,
    )


def _intact(entry: Mapping[str, Any], root: Path) -> bool:
    """Whether every file a recorded stage wrote (``outputs``, under *root*) or relies on
    (``watched``) still hashes as recorded; a watched file recorded ``absent`` must still
    be missing."""
    files = {
        **{str(root / name): sha for name, sha in (entry.get("outputs") or {}).items()},
        **(entry.get("watched") or {}),
    }
    for path, sha in files.items():
        candidate = Path(path)
        if sha == _ABSENT:
            if candidate.exists():
                return False
        elif not candidate.is_file() or _file_sha256(candidate) != sha:
            return False
    return True


def _params(run: _Run, name: str) -> dict[str, Any]:
    """What stage *name* reads besides the stages before it."""
    from tundravm import __version__

    base = {
        "recipe_digest": run.recipe_digest,
        "lock_sha256": run.lock_sha256,
        "variants": list(run.names),
    }
    if name == "lock-check":
        return {**base, "backend": run.backend.name}
    if name == "fetch":
        return {**base, "backend": run.backend.name, "build": str(run.build.resolve())}
    if name == "bake":
        return {
            **base,
            "backend": run.fingerprint,
            "build": str(run.build.resolve()),
            "tundravm": __version__,
        }
    if name == "measure":
        return {"measured": demands(run.require, "measured") is not None}
    if name in ("evidence", "verify"):
        recipe_file = run.recipe_path
        file_sha = (
            _file_sha256(recipe_file) if recipe_file is not None and recipe_file.is_file() else None
        )
        return {
            "require": list(run.require),
            "signer": run.public_key,
            "recipe_file": file_sha,
            "attestations": [_file_sha256(path) for path in run.attestations],
            "max_age": run.max_age,
        }
    if name == "changes":
        since = run.since
        digest = _file_sha256(since) if since is not None and since.is_file() else None
        return {"since": None if since is None else str(since), "sha256": digest}
    if name == "notes":
        return {"version": run.version, "verdict": run.verdict, "reasons": run.reasons}
    return {}


def _stage_lock_check(run: _Run) -> _Outcome:
    drift = lock_status(run.recipe, run.locked, variants=run.names)
    if drift:
        listed = "; ".join(f"{d.subject or d.code}: {d.message}" for d in drift)
        raise LockfileError(
            f"The recipe drifted from its lockfile: {listed}.",
            hint="A release never updates the lock: run `tundravm lock RECIPE` (--update "
            "NAME for a source), commit the lockfile, then release again.",
            context={"lockfile": str(run.lock_path or "(given)")},
        )
    where = "" if run.lock_path is None else f" ({run.lock_path})"
    return _Outcome("completed", f"lock current{where}, sha256 {run.lock_sha256[:12]}")


def _stage_fetch(run: _Run) -> _Outcome:
    if run.backend.name == INPROCESS:
        return _Outcome("skipped", "in-process backend: nothing is built, nothing to fetch")
    fetched = fetch_image(run.img, run.names, locked=run.locked, build=run.build)
    if not fetched:
        return _Outcome("completed", "no source builds")
    kept = sum(1 for source in fetched if source.cached)
    pins = {source.name: source.pin for source in fetched}
    return _Outcome("completed", f"{len(fetched)} sources ({kept} kept)", facts={"pins": pins})


def _stage_bake(run: _Run) -> _Outcome:
    differed: ReproducibilityError | None = None
    try:
        bake_image(
            run.img,
            run.names,
            locked=run.locked,
            backend=run.backend,
            build=run.build,
            lock_source=run.lock_path,
            verify_reproducible=True,
        )
    except ReproducibilityError as exc:
        differed = exc  # recorded in bake-result.json; the evidence fails on it
    manifest = run.build / BAKE_RESULT_FILENAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    recorded = recorded_reproducibility(payload.get(MANIFEST_KEY) or {})
    state = "not checked" if recorded is None else str(recorded["state"])
    artifacts = [a for a in read_artifacts(run.build) if a.variant in run.names]
    outputs: dict[str, str] = {}
    watched = {str(manifest.resolve()): _file_sha256(manifest)}
    rows: list[dict[str, str]] = []
    for artifact in artifacts:
        verify_artifact(artifact)
        relative = f"{artifact.variant}/{artifact.path.name}"
        target = run.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        shutil.copyfile(artifact.path, target)
        outputs[relative] = _file_sha256(target)
        if outputs[relative] != artifact.sha256:
            raise ArtifactError(
                f"The copy of {artifact.path} in {run.root} does not match its sha256.",
                hint="Check the disk holding the release directory, then run the release again.",
                context={"artifact": str(artifact.path), "copy": str(target)},
            )
        watched[str(artifact.path.resolve())] = artifact.sha256
        rows.append(
            {
                "variant": artifact.variant,
                "target": artifact.target,
                "path": relative,
                "sha256": artifact.sha256,
            }
        )
    simulated = any(a.simulated for a in artifacts)
    differ = (recorded or {}).get("differ") if differed is not None else None
    detail = f" ({len(differ)} differ)" if isinstance(differ, list) else ""
    summary = f"{len(rows)} artifacts, reproducible: {state}{detail}"
    if simulated:
        summary += ", simulated"
    facts = {
        "artifacts": rows,
        "reproducibility": state,
        "simulated": simulated,
        "backend": run.backend.name,
    }
    return _Outcome("completed", summary, outputs=outputs, watched=watched, facts=facts)


def _artifacts(run: _Run) -> list[Artifact]:
    return [a for a in read_artifacts(run.build) if a.variant in run.names]


def _stage_measure(run: _Run) -> _Outcome:
    required = demands(run.require, "measured") is not None
    if run.facts["bake"].get("simulated"):
        tail = " (measured is required: the release fails)" if required else ""
        return _Outcome("skipped", f"simulated artifacts: nothing to measure{tail}")
    artifacts = _artifacts(run)
    policies: dict[str, dict[str, object]] = {}
    try:
        for name in run.names:
            artifact = select_artifact(artifacts, variant=name)
            policies[name] = measure(artifact).to_policy()
    except MeasurementError as exc:
        if required:
            raise
        reason = str(exc.args[0]) if exc.args else "measurement failed"
        return _Outcome("skipped", f"not measured: {reason}")
    outputs: dict[str, str] = {}
    watched: dict[str, str] = {}
    registers: dict[str, dict[str, str]] = {}
    for name, policy in policies.items():
        source = run.build / name / "policy.json"
        write_policy(policy, source)
        relative = f"{name}/policy.json"
        write_policy(policy, run.root / relative)
        outputs[relative] = _file_sha256(run.root / relative)
        watched[str(source.resolve())] = _file_sha256(source)
        raw = policy.get("registers")
        registers[name] = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    tool = next((str(p.get("tool")) for p in policies.values()), "")
    summary = f"{len(policies)} policies ({tool})"
    return _Outcome(
        "completed", summary, outputs=outputs, watched=watched, facts={"registers": registers}
    )


def _stage_sbom(run: _Run) -> _Outcome:
    outputs: dict[str, str] = {}
    watched: dict[str, str] = {}
    packages = 0
    for artifact in _artifacts(run):
        for manifest in package_manifest_candidates(artifact.path, artifact.variant):
            found = _file_sha256(manifest) if manifest.is_file() else _ABSENT
            watched[str(manifest.resolve())] = found
        document = sbom(artifact, lock=run.locked)
        relative = f"{artifact.variant}/sbom-{artifact.target}.spdx.json"
        _write_text(run.root / relative, document.render("spdx-json"))
        outputs[relative] = _file_sha256(run.root / relative)
        packages += len(document.packages)
    summary = f"{len(outputs)} SPDX documents, {packages} packages"
    return _Outcome("completed", summary, outputs=outputs, watched=watched)


def _stage_evidence(run: _Run) -> _Outcome:
    found = evidence(
        run.recipe,
        build=run.build,
        variants=run.names,
        lock=run.lock_path if run.lock_path is not None else run.locked,
        recipe_path=run.recipe_path,
        require=run.require,
        attestations=run.attestations,
        max_age=run.max_age,
    )
    if run.sign is not None:
        found = found.sign(run.sign)
    found.bundle(run.root / BUNDLE_NAME)
    _write_text(run.root / HTML_NAME, found.html())
    outputs = {name: _file_sha256(run.root / name) for name in (BUNDLE_NAME, HTML_NAME)}
    reasons = [dict(reason) for reason in found.reasons]
    signed = ", signed" if run.sign is not None else ""
    attested = f", {len(run.attestations)} attestations" if run.attestations else ""
    facts = {"verdict": found.verdict, "reasons": reasons, "verdicts": _verdicts(found.index)}
    return _Outcome(
        "completed",
        f"verdict {found.verdict}{signed}{attested}",
        outputs=outputs,
        facts=facts,
        verdict=found.verdict,
    )


def _verdicts(index: Mapping[str, Any]) -> dict[str, Any]:
    verdict = index.get("verdict") or {}
    keys = ("integrity", "lock", "reproducible", "policy", "attested")
    return {key: verdict.get(key) for key in keys}


def _stage_verify(run: _Run) -> _Outcome:
    with tempfile.TemporaryDirectory(prefix="tundravm-release-") as scratch:
        allowed: Path | None = None
        if run.public_key is not None:
            allowed = Path(scratch) / "allowed_signers"
            key = " ".join(run.public_key.split()[:2])
            allowed.write_text(
                f'{SIGNER} namespaces="{_ssh_sig.NAMESPACE}" {key}\n', encoding="utf-8"
            )
        result = verify_evidence(
            run.root / BUNDLE_NAME,
            build=run.root,
            require=run.require,
            allowed_signers=allowed,
            signer=None if allowed is None else SIGNER,
        )
    counts = result.counts()
    total = sum(counts.values())
    problems = [
        {"code": check.code, "subject": check.subject, "message": check.message}
        for check in result.problems
    ]
    signed = f", signed by {result.signer}" if result.signer else ""
    summary = f"verdict {result.verdict}: {counts['pass']}/{total} checks pass{signed}"
    facts = {"verdict": result.verdict, "problems": problems, "signer": result.signer}
    return _Outcome("completed", summary, facts=facts, verdict=result.verdict)


def _stage_changes(run: _Run) -> _Outcome:
    if run.since is None:
        return _Outcome("skipped", "no earlier release of this recipe to compare with")
    report = changes(run.recipe, since=run.since, lock=run.locked, build=run.build)
    total = len(report.changes)
    facts = {
        "since": run.since_label,
        "markdown": report.render("markdown"),
        "counts": report.counts(),
    }
    return _Outcome("completed", f"since {run.since_label}: {total} changes", facts=facts)


def _stage_notes(run: _Run) -> _Outcome:
    _write_text(run.root / NOTES_NAME, _notes(run))
    return _Outcome(
        "completed",
        f"{NOTES_NAME}, verdict {run.verdict}",
        outputs={NOTES_NAME: _file_sha256(run.root / NOTES_NAME)},
        verdict=run.verdict,
    )


_STAGE_RUNNERS: dict[str, Callable[[_Run], _Outcome]] = {
    "lock-check": _stage_lock_check,
    "fetch": _stage_fetch,
    "bake": _stage_bake,
    "measure": _stage_measure,
    "sbom": _stage_sbom,
    "evidence": _stage_evidence,
    "verify": _stage_verify,
    "changes": _stage_changes,
    "notes": _stage_notes,
}


# ── verdict, notes, result ───────────────────────────────────────────────


def _verdict(run: _Run) -> tuple[str, list[dict[str, Any]]]:
    reasons: list[dict[str, Any]] = []
    for reason in run.facts["evidence"].get("reasons") or ():
        reasons.append({"source": "evidence", **dict(reason)})
    for problem in run.facts["verify"].get("problems") or ():
        reasons.append({"source": "verify", "severity": "fail", **dict(problem)})
    evidence_ok = run.facts["evidence"].get("verdict") == "pass"
    verify_ok = run.facts["verify"].get("verdict") == "pass"
    return ("pass" if evidence_ok and verify_ok else "fail"), reasons


def _notes(run: _Run) -> str:
    """``RELEASE.md``: version, variants, artifacts, reproducibility, registers, verify
    verdict and the changes since the previous release."""
    from tundravm import __version__

    bake = run.facts["bake"]
    title = release_name(run.recipe.name, run.version)
    lines = [
        f"# {title}",
        "",
        f"Recipe `{run.recipe.name}` version **{run.version}**, released {_created()} with "
        f"tundravm {__version__}.",
        "",
        f"- Verdict: **{run.verdict}**",
        f"- Variants: {', '.join(f'`{name}`' for name in run.names)}",
        f"- Recipe digest: `{run.recipe_digest}`",
        f"- Lockfile sha256: `{run.lock_sha256}`",
        f"- Backend: `{bake.get('backend')}`",
    ]
    if bake.get("simulated"):
        lines.append(
            "- **Simulated**: the in-process backend built nothing; these artifacts are "
            "stand-ins, not images."
        )
    lines += ["", "## Artifacts", ""]
    rows = [
        (
            md_cell(row["variant"]),
            md_cell(row["target"]),
            md_cell(row["path"], code=True),
            md_cell(row["sha256"], code=True),
        )
        for row in bake.get("artifacts") or ()
    ]
    lines.append(md_table(("Variant", "Target", "File", "sha256"), rows))
    lines += ["", "## Reproducibility", "", _reproduced(str(bake.get("reproducibility")))]
    lines += ["", "## Measurements", ""]
    registers = (run.facts.get("measure") or {}).get("registers")
    if registers:
        measured = [
            (md_cell(name), md_cell(register), md_cell(value, code=True))
            for name, values in sorted(registers.items())
            for register, value in sorted(values.items())
        ]
        lines.append(md_table(("Variant", "Register", "Value"), measured))
    else:
        lines.append("Not measured: no verifier policy is part of this release.")
    verify = run.facts["verify"]
    lines += ["", "## Verification", ""]
    signer = verify.get("signer")
    signed = f"; the evidence is signed by `{signer}`" if signer else "; the evidence is unsigned"
    lines.append(f"`tundravm verify {BUNDLE_NAME} --build .`: **{verify.get('verdict')}**{signed}.")
    if run.reasons:
        lines.append("")
        lines.extend(
            f"- {reason['source']} `{reason.get('code')}` {reason.get('subject') or ''}: "
            f"{reason.get('message')}".replace("  ", " ")
            for reason in run.reasons
        )
    lines += ["", "## Changes", ""]
    found = run.facts.get("changes") or {}
    markdown = found.get("markdown")
    if markdown:
        lines.append(f"Since `{found.get('since')}`.")
        lines.append("")
        lines.append(_demote(str(markdown)).strip())
    else:
        lines.append("First release of this recipe: nothing to compare with.")
    return "\n".join(lines).rstrip() + "\n"


def _reproduced(state: str) -> str:
    return {
        "matched": "matched: a second build gave the same sha256 for every artifact.",
        "different": "**different**: a second build gave other artifacts (see the evidence).",
        "not checked": "not checked.",
    }.get(state, f"{state}: the second build did not complete.")


def _demote(markdown: str) -> str:
    """*markdown* with every heading one level deeper, under the notes' ``## Changes``."""
    return re.sub(r"^(#+) ", lambda m: f"{m.group(1)}# ", markdown, flags=re.M)


def _result(run: _Run, stages: Sequence[ReleaseStage]) -> Release:
    bake = run.facts["bake"]
    signer = None if run.public_key is None else {"principal": SIGNER, "public_key": run.public_key}
    return Release(
        name=release_name(run.recipe.name, run.version),
        version=run.version,
        path=run.root,
        recipe=run.recipe.name,
        recipe_digest=run.recipe_digest,
        lock_sha256=run.lock_sha256,
        variants=run.names,
        backend=str(bake.get("backend")),
        simulated=bool(bake.get("simulated")),
        artifacts=tuple(bake.get("artifacts") or ()),
        stages=tuple(stages),
        verdict=run.verdict,
        reasons=tuple(run.reasons),
        required=run.require,
        since=run.since_label,
        signer=signer,
        reproducibility=str(bake.get("reproducibility")),
        created=_now(),
        spec=_public_spec(run.spec),
        spec_history=tuple(run.history),
        skipped=tuple(run.skipped),
        allow_ineligible=bool(run.spec.get("allow_ineligible")),
    )


# ── the manifest ─────────────────────────────────────────────────────────


RELEASE_CHECK_KINDS = ("file", "signature", "release")
"""The kinds of a :class:`ReleaseVerification`'s own checks, in the order they are reported."""
_UNLISTED = frozenset({STATE_NAME, MANIFEST_NAME, MANIFEST_SIGNATURE_NAME})
"""Files of a release directory the manifest does not list: itself, its signature and the
state of a release in progress."""


def _published(root: Path) -> list[str]:
    """Every file under *root* the manifest lists, as sorted POSIX paths relative to it."""
    found = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    ]
    return sorted(name for name in found if name not in _UNLISTED)


def _write_manifest(run: _Run, result: Release) -> None:
    """``release-manifest.json`` (:data:`MANIFEST_SCHEMA`): the release's identity and every
    file of its directory with its sha256 and size; with a signing key also
    ``release-manifest.json.sig`` (``ssh-keygen -Y sign``, namespace ``tundravm-release``)."""
    root = run.root
    (root / MANIFEST_SIGNATURE_NAME).unlink(missing_ok=True)
    files = [
        {"path": name, "sha256": _file_sha256(root / name), "size": (root / name).stat().st_size}
        for name in _published(root)
    ]
    signing = run.spec.get("signing")
    signer = (
        None
        if not isinstance(signing, Mapping)
        else {
            "principal": SIGNER,
            "public_key": signing.get("public_key"),
            "fingerprint": signing.get("fingerprint"),
        }
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "name": result.name,
        "recipe": result.recipe,
        "version": result.version,
        "recipe_digest": result.recipe_digest,
        "lock_sha256": result.lock_sha256,
        "created": result.created,
        "signer": signer,
        "files": files,
    }
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    _write_text(root / MANIFEST_NAME, text)
    if run.sign is not None:
        signature = _ssh_sig.sign(text.encode(), run.sign, namespace=_ssh_sig.RELEASE_NAMESPACE)
        _write_text(root / MANIFEST_SIGNATURE_NAME, signature.decode())


# ── verifying a release directory ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReleaseVerification:
    """What :func:`verify_release` found: its own ``checks`` (kinds
    :data:`RELEASE_CHECK_KINDS`) and the embedded ``evidence`` verification. :attr:`passed`
    only when every check of both passed."""

    path: str
    checks: tuple[VerifyCheck, ...]
    evidence: EvidenceVerification | None = None
    signer: str | None = None
    """The principal whose manifest signature verified, when one did."""
    notes: tuple[Mapping[str, str], ...] = ()
    """``{"code", "message"}`` each: what was not checked (``unsigned``,
    ``signature-unchecked``) and why."""
    manifest: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def verdict(self) -> str:
        """``fail`` when a check failed, ``incomplete`` when one is missing, else ``pass``."""
        states = {check.state for check in self.problems}
        return "fail" if "fail" in states else "incomplete" if "missing" in states else "pass"

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def problems(self) -> tuple[VerifyCheck, ...]:
        """The checks that did not pass, the evidence's included."""
        own = tuple(check for check in self.checks if check.state != "pass")
        return own + (() if self.evidence is None else self.evidence.problems)

    def counts(self) -> dict[str, int]:
        found = {"pass": 0, "fail": 0, "missing": 0}
        for check in self.checks:
            found[check.state] += 1
        if self.evidence is not None:
            for state, count in self.evidence.counts().items():
                found[state] += count
        return found

    def to_dict(self) -> dict[str, object]:
        """What ``verify RELEASE_DIR --format json`` prints; ``schema`` is
        ``tundravm.release-verify/1``."""
        manifest = self.manifest
        return {
            "schema": RELEASE_VERIFY_SCHEMA,
            "release": self.path,
            "verdict": self.verdict,
            "passed": self.passed,
            "counts": self.counts(),
            "signer": self.signer,
            "recorded": {key: manifest.get(key) for key in ("name", "version", "created")},
            "checks": [check.to_dict() for check in self.checks],
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "notes": [dict(note) for note in self.notes],
        }

    def render(self, format: ReleaseFormat = "text") -> str:
        """``json`` (:meth:`to_dict`) or ``text``: a line per check kind and one for the
        evidence, then every problem."""
        if format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        counts = self.counts()
        lines = [
            f"verify {self.path}  verdict {self.verdict}  "
            f"({counts['pass']} pass, {counts['fail']} fail, {counts['missing']} missing)"
        ]
        if self.manifest:
            lines.append(
                f"  manifest   {self.manifest.get('name')}  version "
                f"{self.manifest.get('version')}  created {self.manifest.get('created')}"
            )
        for kind in RELEASE_CHECK_KINDS:
            found = [c for c in self.checks if c.kind == kind]
            if found:
                good = sum(c.state == "pass" for c in found)
                tail = f", signed by {self.signer}" if kind == "signature" and self.signer else ""
                lines.append(f"  {kind:<10} {good}/{len(found)} pass{tail}")
        if self.evidence is not None:
            inner = self.evidence.counts()
            total = sum(inner.values())
            lines.append(
                f"  evidence   {self.evidence.verdict}: {inner['pass']}/{total} checks pass"
            )
        lines.extend(f"  note       {note['code']}: {note['message']}" for note in self.notes)
        if self.problems:
            lines.append("problems:")
            lines.extend(
                f"  {c.state:<8} {c.code}  {c.subject or '-'}: {c.message}" for c in self.problems
            )
        return "\n".join(lines) + "\n"


def is_release(path: Path) -> bool:
    """Whether *path* is a release directory: one holding ``release.json``."""
    return Path(path).is_dir() and (Path(path) / INDEX_NAME).is_file()


def signing_key(root: Path) -> str | None:
    """The ``SHA256:...`` fingerprint of the key ``release-manifest.json.sig`` was made
    with, read from the signature itself (``None`` for an unsigned release). Unlike the
    ``signer`` metadata, it is what :func:`verify_release` authenticates."""
    signature = Path(root) / MANIFEST_SIGNATURE_NAME
    return _ssh_sig.signature_fingerprint(signature.read_bytes()) if signature.is_file() else None


def recorded_keys(*records: object) -> frozenset[str]:
    """The key fingerprints ``signer`` metadata *records* claim: each one's
    ``public_key`` (fingerprinted) and ``fingerprint``."""
    keys: set[str] = set()
    for recorded in records:
        if not isinstance(recorded, Mapping):
            continue
        public = recorded.get("public_key")
        if isinstance(public, str):
            try:
                keys.add(_ssh_sig.fingerprint(public))
            except ValidationError:
                keys.add(public)
        if isinstance(recorded.get("fingerprint"), str):
            keys.add(str(recorded["fingerprint"]))
    return frozenset(keys)


def _signer_metadata(
    root: Path, index: Mapping[str, Any] | None, manifest: Mapping[str, Any]
) -> list[VerifyCheck]:
    """``release-inconsistent`` when the ``signer`` that ``release.json`` or the manifest
    names contradicts the key the manifest signature was made with, or each other."""
    records = (None if index is None else index.get("signer"), manifest.get("signer"))
    principals = {str(r.get("principal")) for r in records if isinstance(r, Mapping)}
    key = signing_key(root)
    claimed = recorded_keys(*records)
    if len(principals) > 1:
        message = f"{INDEX_NAME} and the manifest name different signers: {sorted(principals)}"
        return [_fail("release", "signer", message, "release-inconsistent")]
    if key is not None and claimed - {key}:
        message = (
            f"the signer metadata names key(s) {', '.join(sorted(claimed - {key}))}, "
            f"{MANIFEST_SIGNATURE_NAME} was made with {key}"
        )
        return [_fail("release", "signer", message, "release-inconsistent")]
    return []


def verify_release(
    path: Path,
    *,
    allowed_signers: Path | None = None,
    signer: str | None = None,
    max_age: float | timedelta | None = None,
    now: datetime | None = None,
) -> ReleaseVerification:
    """Check the release directory at *path* on its own, without the recipe or the build.

    Every file ``release-manifest.json`` lists is re-hashed (``file-hash-mismatch``,
    ``file-missing``) and every other file is a problem (``file-unlisted``; only the
    manifest's signature is not listed). *allowed_signers* (an OpenSSH allowed-signers
    file whose line for the key allows the ``tundravm-release`` and ``tundravm-evidence``
    namespaces) checks ``release-manifest.json.sig`` (``manifest-signature-invalid``,
    ``manifest-unsigned`` when there is none or ``release.json`` names a signer but the
    signature is gone); *signer* is the principal. ``release.json`` must agree with the
    manifest: name, recipe, version, recipe digest, lockfile sha256 and every artifact's
    sha256, and the ``signer`` both name must be the key the manifest signature was made
    with (``release-inconsistent``). Then the evidence bundle is checked with
    :func:`~tundravm.verify_evidence`, the release's artifacts as the build, its
    ``required`` checks and the same signers. Raises ``ValidationError`` when *path* is
    not a release directory.
        *max_age* judges bundled attestation records with that freshness window instead of
    the one the evidence recorded, and *now* at that moment instead of the one it was
    evaluated at (``verify_evidence(max_age=, now=)``).
    """
    root = Path(path)
    if not is_release(root):
        raise ValidationError(
            f"{root} is not a release directory: it holds no {INDEX_NAME}.",
            hint="Pass a directory `tundravm release` wrote, RELEASES/<recipe>-<version>.",
            context={"path": str(root)},
        )
    checks: list[VerifyCheck] = []
    notes: list[dict[str, str]] = []
    manifest = _read_json(root / MANIFEST_NAME)
    index = _read_json(root / INDEX_NAME)
    if manifest is None or manifest.get("schema") != MANIFEST_SCHEMA:
        code = "file-missing" if not (root / MANIFEST_NAME).is_file() else "release-inconsistent"
        message = (
            f"{MANIFEST_NAME} is missing"
            if code == "file-missing"
            else f"{MANIFEST_NAME} is not a {MANIFEST_SCHEMA} manifest"
        )
        checks.append(_fail("file", MANIFEST_NAME, message, code))
        return ReleaseVerification(str(root), tuple(checks))
    listed = _listed(manifest)
    checks.extend(_file_checks(root, listed))
    check, signed_by, note = _manifest_signature(root, index, allowed_signers, signer)
    if check is not None:
        checks.append(check)
    if note is not None:
        notes.append(note)
    checks.extend(_consistency(index, manifest, listed))
    checks.extend(_signer_metadata(root, index, manifest))
    if (root / STATE_NAME).exists() or not (isinstance(index, Mapping) and index.get("finished")):
        message = f"the release is not finished (verdict {(index or {}).get('verdict')})"
        checks.append(_fail("release", INDEX_NAME, message, "release-inconsistent"))
    found: EvidenceVerification | None = None
    bundle = root / BUNDLE_NAME
    if bundle.is_file() and isinstance(index, Mapping):
        required = [str(name) for name in index.get("required") or ()]
        try:
            found = verify_evidence(
                bundle,
                build=root,
                require=required,
                allowed_signers=allowed_signers,
                signer=signer,
                max_age=max_age,
                now=now,
            )
        except ValidationError as exc:
            message = f"{BUNDLE_NAME} could not be checked: {exc.args[0] if exc.args else exc}"
            checks.append(_fail("release", BUNDLE_NAME, message, "release-inconsistent"))
    elif BUNDLE_NAME not in listed:
        message = f"the manifest lists no {BUNDLE_NAME}: the release carries no evidence"
        checks.append(_fail("release", BUNDLE_NAME, message, "release-inconsistent"))
    order = {kind: n for n, kind in enumerate(RELEASE_CHECK_KINDS)}
    ordered = sorted(checks, key=lambda c: order[c.kind])
    return ReleaseVerification(
        str(root),
        tuple(ordered),
        evidence=found,
        signer=signed_by,
        notes=tuple(notes),
        manifest=manifest,
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _fail(kind: str, subject: str, message: str, code: str) -> VerifyCheck:
    return VerifyCheck(kind=kind, subject=subject, state="fail", message=message, code=code)


def _listed(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = manifest.get("files")
    if not isinstance(rows, list):
        return {}
    return {str(row.get("path")): row for row in rows if isinstance(row, Mapping)}


def _file_checks(root: Path, listed: Mapping[str, Mapping[str, Any]]) -> list[VerifyCheck]:
    checks: list[VerifyCheck] = []
    for name, row in sorted(listed.items()):
        target = root / name
        inside = target.resolve().is_relative_to(root.resolve())
        if not inside or not target.is_file():
            message = f"the manifest lists {name}, which is not in the release"
            checks.append(_fail("file", name, message, "file-missing"))
            continue
        sha, size = _file_sha256(target), target.stat().st_size
        if sha != row.get("sha256") or size != row.get("size"):
            message = (
                f"sha256 {sha[:16]}... ({size} bytes), the manifest lists "
                f"{str(row.get('sha256'))[:16]}... ({row.get('size')} bytes)"
            )
            checks.append(_fail("file", name, message, "file-hash-mismatch"))
            continue
        checks.append(
            VerifyCheck("file", name, "pass", f"sha256 {sha[:16]}... ({size} bytes) as listed")
        )
    for name in _published(root):
        if name not in listed:
            message = f"{name} is in the release but not in {MANIFEST_NAME}"
            checks.append(_fail("file", name, message, "file-unlisted"))
    return checks


def _manifest_signature(
    root: Path,
    index: Mapping[str, Any] | None,
    allowed_signers: Path | None,
    signer: str | None,
) -> tuple[VerifyCheck | None, str | None, dict[str, str] | None]:
    """The signature check, the principal that signed, and a note when it was not checked."""
    signature = root / MANIFEST_SIGNATURE_NAME
    recorded = index.get("signer") if isinstance(index, Mapping) else None
    if not signature.is_file():
        if allowed_signers is not None or recorded:
            why = (
                f"release.json names signer {recorded.get('principal')!r}"
                if isinstance(recorded, Mapping)
                else "--allowed-signers was given"
            )
            message = f"{MANIFEST_SIGNATURE_NAME} is missing, and {why}"
            return _fail("signature", MANIFEST_NAME, message, "manifest-unsigned"), None, None
        note = {"code": "unsigned", "message": f"the release carries no {MANIFEST_SIGNATURE_NAME}"}
        return None, None, note
    if allowed_signers is None:
        message = (
            f"{MANIFEST_SIGNATURE_NAME} is present but not checked: pass --allowed-signers FILE"
        )
        return None, None, {"code": "signature-unchecked", "message": message}
    result = _ssh_sig.verify(
        (root / MANIFEST_NAME).read_bytes(),
        signature.read_bytes(),
        allowed_signers,
        signer=signer,
        namespace=_ssh_sig.RELEASE_NAMESPACE,
    )
    if not result.valid:
        message = f"the manifest signature does not verify: {result.message}"
        return _fail("signature", MANIFEST_NAME, message, "manifest-signature-invalid"), None, None
    check = VerifyCheck(
        "signature", MANIFEST_NAME, "pass", f"signed by {result.signer}: {result.message}"
    )
    return check, result.signer, None


def _consistency(
    index: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
    listed: Mapping[str, Mapping[str, Any]],
) -> list[VerifyCheck]:
    """``release.json`` against the manifest: the identity, digests and artifact sha256s."""
    if index is None or index.get("schema") != RELEASE_SCHEMA:
        message = f"{INDEX_NAME} is not a {RELEASE_SCHEMA} index"
        return [_fail("release", INDEX_NAME, message, "release-inconsistent")]
    checks: list[VerifyCheck] = []
    for key in ("name", "recipe", "version", "recipe_digest", "lock_sha256", "created"):
        if index.get(key) != manifest.get(key):
            message = (
                f"{INDEX_NAME} says {key} {index.get(key)!r}, the manifest {manifest.get(key)!r}"
            )
            checks.append(_fail("release", key, message, "release-inconsistent"))
    for row in index.get("artifacts") or ():
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("path"))
        entry = listed.get(name)
        if entry is None or entry.get("sha256") != row.get("sha256"):
            message = f"{INDEX_NAME} lists artifact {name} with another sha256 than the manifest"
            checks.append(_fail("release", name, message, "release-inconsistent"))
    if not checks:
        message = f"{INDEX_NAME} agrees with the manifest: identity, digests and artifacts"
        checks.append(VerifyCheck("release", INDEX_NAME, "pass", message))
    return checks


# ── files ────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _created() -> str:
    value = os.environ.get("SOURCE_DATE_EPOCH", "")
    moment = datetime.fromtimestamp(int(value), UTC) if value.isdigit() else datetime.now(UTC)
    return moment.strftime("%Y-%m-%d")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _save(root: Path, state: Mapping[str, Any]) -> None:
    _write_text(root / STATE_NAME, json.dumps(state, indent=2, sort_keys=True) + "\n")


__all__ = [
    "INDEX_NAME",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "MANIFEST_SIGNATURE_NAME",
    "NOTES_NAME",
    "RELEASE_CHECK_KINDS",
    "RELEASE_FORMATS",
    "RELEASE_SCHEMA",
    "RELEASE_VERIFY_SCHEMA",
    "SPEC_KEYS",
    "STAGES",
    "STATE_NAME",
    "Release",
    "ReleaseStage",
    "ReleaseVerification",
    "is_release",
    "previous_release",
    "release",
    "release_image",
    "release_name",
    "releases_in_progress",
    "verify_release",
]
