"""Cut a release: one immutable directory from a locked recipe, built, measured and evidenced.

:func:`release` runs the stages of :data:`STAGES` in order: ``lock-check`` (refuse
drift; the lockfile is never updated), ``fetch``, ``bake`` (with a second build that
must match), ``measure`` (a verifier policy per variant), ``sbom`` (SPDX per
artifact), ``evidence`` (bundle, HTML page, optional signature), ``verify`` (the
bundle just written), ``changes`` (against the previous release) and ``notes``
(``RELEASE.md``). Everything lands in ``RELEASES/<recipe>-<version>/``, which ends
with ``release.json``.

Until it is finished the directory holds ``release.state.json``: each completed
stage with the sha256 of its inputs and of its outputs. Running the release again
keeps every stage whose inputs and outputs are unchanged and runs the first stage
whose inputs changed and every stage after it. A release that passes is finished:
the state goes and the directory is immutable. One that fails keeps its state, so
it resumes once the cause is fixed.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import _ssh_sig
from .backends.base import BuildBackend
from .changes import changes
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
    ValidationError,
)
from .evidence import _requirements, evidence
from .formats import md_cell, md_table
from .measure.policy import write_policy
from .models import BAKE_RESULT_FILENAME
from .verify import verify_evidence

RELEASE_SCHEMA = "tundravm.release/1"
"""The ``schema`` of ``release.json``, which ``release --format json`` prints."""
STATE_SCHEMA = "tundravm.release-state/1"
"""The ``schema`` of ``release.state.json``."""
INDEX_NAME = "release.json"
STATE_NAME = "release.state.json"
NOTES_NAME = "RELEASE.md"
BUNDLE_NAME = "evidence.tar.gz"
HTML_NAME = "evidence.html"
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
ReleaseFormat = Literal["text", "json"]
RELEASE_FORMATS: tuple[ReleaseFormat, ...] = ("text", "json")
StageStatus = Literal["done", "skipped"]
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


@dataclass(frozen=True, slots=True)
class ReleaseStage:
    """One stage of a release: ``done`` or ``skipped`` (it did not apply, ``summary`` says
    why); ``resumed`` when an earlier run completed it with the same inputs. ``inputs`` is
    the sha256 of what it read, ``outputs`` the sha256 of each file it wrote into the
    release directory."""

    name: str
    status: StageStatus
    started: str
    finished: str
    seconds: float
    inputs: str
    outputs: Mapping[str, str] = field(default_factory=dict)
    summary: str = ""
    resumed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
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
        shown = "kept" if self.resumed and self.status == "done" else self.status
        took = "" if self.resumed else f"{self.seconds:.1f}s"
        head = f"[{number}/{len(STAGES)}] {self.name:<10}  {shown:<7} {took:>6}"
        return f"{head}  {self.summary}".rstrip()


@dataclass(frozen=True, slots=True)
class Release:
    """A release directory and what made it; :meth:`to_dict` is its ``release.json``.

    ``verdict`` is ``pass`` only when the evidence and its verification pass, every
    ``require`` check included; ``reasons`` say why not. ``finished`` when it passed:
    the directory is then immutable.
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
            "backend": self.backend,
            "simulated": self.simulated,
            "reproducibility": self.reproducibility,
            "required": list(self.required),
            "since": self.since,
            "signer": None if self.signer is None else dict(self.signer),
            "artifacts": [dict(row) for row in self.artifacts],
            "stages": [stage.to_dict() for stage in self.stages],
            "verdict": self.verdict,
            "reasons": [dict(reason) for reason in self.reasons],
            "finished": self.finished,
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
    require: tuple[str, ...]
    sign: Path | None
    public_key: str | None
    since: Path | None
    since_label: str | None
    recipe_path: Path | None
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdict: str = "pass"
    reasons: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: StageStatus
    summary: str
    outputs: dict[str, str] = field(default_factory=dict)
    watched: dict[str, str] = field(default_factory=dict)
    """Files outside the release directory a later stage reads, by absolute path."""
    facts: dict[str, Any] = field(default_factory=dict)


def release(
    recipe: Recipe,
    *,
    version: str,
    backend: Backend | BuildBackend,
    variants: Sequence[str] | None = None,
    sign: Path | None = None,
    require: Sequence[str] = (),
    since: Path | None = None,
    releases: Path = Path("releases"),
    build: Path = Path("build"),
    lock: Lock | Path | None = None,
    restart: bool = False,
    recipe_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Release:
    """Release *variants* (default: all) of *recipe* as ``releases/<recipe.name>-<version>``.

    Runs :data:`STAGES` against *lock* (a :class:`Lock` or a lockfile path; default the
    recipe's ``build/tundravm.lock``, which must exist), baking with *backend* into the
    build directory *build*. *sign* is an SSH private key that signs the evidence;
    ``verify`` then checks the signature with its public half. *require* names checks of
    :data:`~tundravm.evidence.EVIDENCE_REQUIREMENTS` the evidence must hold. *since* is
    the previous release (its directory, or an evidence bundle) the changes are against;
    default: the newest finished release of the recipe in *releases*. *recipe_path* is
    recorded in the evidence. *progress* receives one line per stage.

    The release directory keeps ``release.state.json`` until the release passes, so a
    run that failed or was interrupted resumes: stages whose inputs are unchanged are
    kept, and the first stage whose inputs changed runs with every stage after it.
    *restart* discards that state. A finished release refuses (``StateError``): bump the
    version. A drifted lockfile is a ``LockfileError``; a release never relocks.
    """
    built = backend.build_backend() if isinstance(backend, Backend) else backend
    img = replace(lower(recipe), backend=built)
    return release_image(
        recipe,
        img,
        version=version,
        variants=variants,
        sign=sign,
        require=require,
        since=since,
        releases=releases,
        build=build,
        lock=lock,
        restart=restart,
        recipe_path=recipe_path,
        progress=progress,
    )


def release_image(
    recipe: Recipe,
    img: Lowered,
    *,
    version: str,
    variants: Sequence[str] | None = None,
    sign: Path | None = None,
    require: Sequence[str] = (),
    since: Path | None = None,
    releases: Path = Path("releases"),
    build: Path = Path("build"),
    lock: Lock | Path | None = None,
    restart: bool = False,
    recipe_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Release:
    """:func:`release` for a recipe already lowered as *img*, which carries the backend."""
    if img.backend is None:
        raise ValidationError(
            "No build backend to release with.",
            hint="Pass --backend KIND (release(backend=...)), or bind `backend` in the "
            "recipe file.",
        )
    _check_version(version)
    required = _requirements(require)
    names = _variants(recipe, variants)
    root = Path(releases) / release_name(recipe.name, version)
    locked, lock_path, lock_bytes = _lock(img, lock)
    previous, label = _since(since, Path(releases), recipe.name, version)
    public = None if sign is None else _ssh_sig.public_key(Path(sign))
    state = _open(root, recipe.name, version, restart=restart)
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
        require=required,
        sign=None if sign is None else Path(sign),
        public_key=public,
        since=previous,
        since_label=label,
        recipe_path=recipe_path,
    )
    stages = _run_stages(run, state, progress)
    result = _result(run, stages)
    _write_text(root / INDEX_NAME, result.render("json"))
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


def _variants(recipe: Recipe, wanted: Sequence[str] | None) -> tuple[str, ...]:
    declared = tuple(v.name for v in recipe.variants)
    if not wanted:
        return declared
    unknown = [name for name in wanted if name not in declared]
    if unknown:
        raise ValidationError(
            f"Unknown variant(s): {', '.join(unknown)}.",
            hint=f"Declared variants: {', '.join(declared)}",
        )
    return tuple(dict.fromkeys(wanted))


def _open(root: Path, recipe: str, version: str, *, restart: bool) -> dict[str, Any]:
    """The state of the release at *root*: refused when finished, fresh when there is none."""
    state_path = root / STATE_NAME
    name = release_name(recipe, version)
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
    if restart and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    fresh: dict[str, Any] = {"schema": STATE_SCHEMA, "release": name, "stages": {}}
    if not state_path.is_file():
        return fresh
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


def _since(
    given: Path | None, releases: Path, recipe: str, version: str
) -> tuple[Path | None, str | None]:
    """The evidence the changes compare with, and how to name it."""
    if given is not None:
        path = Path(given)
        if path.is_dir() and (path / BUNDLE_NAME).is_file():
            return path / BUNDLE_NAME, path.name
        return path, path.name
    found = previous_release(releases, recipe, exclude=version)
    if found is None:
        return None, None
    return found / BUNDLE_NAME, found.name


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
        fresh = isinstance(entry, dict) and entry.get("inputs") == inputs
        if not rerun and isinstance(entry, dict) and fresh and _intact(entry, run.root):
            stage = _kept(name, entry)
            run.facts[name] = dict(entry.get("facts") or {})
        else:
            rerun = True
            for later in STAGES[number - 1 :]:
                recorded.pop(later, None)
            _save(run.root, state)
            stage, entry = _execute(run, name, inputs)
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
        status=outcome.status,
        started=started,
        finished=_now(),
        seconds=seconds,
        inputs=inputs,
        outputs=dict(outcome.outputs),
        summary=outcome.summary,
    )
    entry = {
        **stage.to_dict(),
        "watched": dict(sorted(outcome.watched.items())),
        "facts": outcome.facts,
    }
    return stage, entry


def _kept(name: str, entry: Mapping[str, Any]) -> ReleaseStage:
    return ReleaseStage(
        name=name,
        status="skipped" if entry.get("status") == "skipped" else "done",
        started=str(entry.get("started") or ""),
        finished=str(entry.get("finished") or ""),
        seconds=float(entry.get("seconds") or 0.0),
        inputs=str(entry.get("inputs")),
        outputs=dict(entry.get("outputs") or {}),
        summary=str(entry.get("summary") or ""),
        resumed=True,
    )


def _intact(entry: Mapping[str, Any], root: Path) -> bool:
    """Whether every file a recorded stage wrote (``outputs``, under *root*) or relies on
    (``watched``) still hashes as recorded."""
    files = {
        **{str(root / name): sha for name, sha in (entry.get("outputs") or {}).items()},
        **(entry.get("watched") or {}),
    }
    for path, sha in files.items():
        candidate = Path(path)
        if not candidate.is_file() or _file_sha256(candidate) != sha:
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
    if name in ("lock-check", "fetch"):
        return {**base, "backend": run.backend.name}
    if name == "bake":
        return {**base, "backend": run.backend.name, "tundravm": __version__}
    if name == "measure":
        return {"measured": "measured" in run.require}
    if name in ("evidence", "verify"):
        recipe_file = run.recipe_path
        file_sha = (
            _file_sha256(recipe_file) if recipe_file is not None and recipe_file.is_file() else None
        )
        return {"require": list(run.require), "signer": run.public_key, "recipe_file": file_sha}
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
    return _Outcome("done", f"lock current{where}, sha256 {run.lock_sha256[:12]}")


def _stage_fetch(run: _Run) -> _Outcome:
    if run.backend.name == INPROCESS:
        return _Outcome("skipped", "in-process backend: nothing is built, nothing to fetch")
    fetched = fetch_image(run.img, run.names, locked=run.locked, build=run.build)
    if not fetched:
        return _Outcome("done", "no source builds")
    kept = sum(1 for source in fetched if source.cached)
    pins = {source.name: source.pin for source in fetched}
    return _Outcome("done", f"{len(fetched)} sources ({kept} kept)", facts={"pins": pins})


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
    return _Outcome("done", summary, outputs=outputs, watched=watched, facts=facts)


def _artifacts(run: _Run) -> list[Artifact]:
    return [a for a in read_artifacts(run.build) if a.variant in run.names]


def _stage_measure(run: _Run) -> _Outcome:
    required = "measured" in run.require
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
        "done", summary, outputs=outputs, watched=watched, facts={"registers": registers}
    )


def _stage_sbom(run: _Run) -> _Outcome:
    outputs: dict[str, str] = {}
    packages = 0
    for artifact in _artifacts(run):
        document = sbom(artifact, lock=run.locked)
        relative = f"{artifact.variant}/sbom-{artifact.target}.spdx.json"
        _write_text(run.root / relative, document.render("spdx-json"))
        outputs[relative] = _file_sha256(run.root / relative)
        packages += len(document.packages)
    return _Outcome("done", f"{len(outputs)} SPDX documents, {packages} packages", outputs=outputs)


def _stage_evidence(run: _Run) -> _Outcome:
    found = evidence(
        run.recipe,
        build=run.build,
        variants=run.names,
        lock=run.lock_path if run.lock_path is not None else run.locked,
        recipe_path=run.recipe_path,
        require=run.require,
    )
    if run.sign is not None:
        found = found.sign(run.sign)
    found.bundle(run.root / BUNDLE_NAME)
    _write_text(run.root / HTML_NAME, found.html())
    outputs = {name: _file_sha256(run.root / name) for name in (BUNDLE_NAME, HTML_NAME)}
    reasons = [dict(reason) for reason in found.reasons]
    signed = ", signed" if run.sign is not None else ""
    facts = {"verdict": found.verdict, "reasons": reasons, "verdicts": _verdicts(found.index)}
    return _Outcome("done", f"verdict {found.verdict}{signed}", outputs=outputs, facts=facts)


def _verdicts(index: Mapping[str, Any]) -> dict[str, Any]:
    verdict = index.get("verdict") or {}
    return {key: verdict.get(key) for key in ("integrity", "lock", "reproducible", "policy")}


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
    return _Outcome("done", summary, facts=facts)


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
    return _Outcome("done", f"since {run.since_label}: {total} changes", facts=facts)


def _stage_notes(run: _Run) -> _Outcome:
    _write_text(run.root / NOTES_NAME, _notes(run))
    return _Outcome(
        "done",
        f"{NOTES_NAME}, verdict {run.verdict}",
        outputs={NOTES_NAME: _file_sha256(run.root / NOTES_NAME)},
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
    )


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
    "NOTES_NAME",
    "RELEASE_FORMATS",
    "RELEASE_SCHEMA",
    "STAGES",
    "STATE_NAME",
    "Release",
    "ReleaseStage",
    "previous_release",
    "release",
    "release_image",
    "release_name",
]
