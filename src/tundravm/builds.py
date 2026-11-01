"""Immutable build directories: ``build`` makes one, everything else reads one.

``BUILD/<image>/production/<build-id>/`` (``development/`` for a profile that is not a
production one) holds what one build made and what it was made from::

    payload.efi          the UKI, the payload that is measured
    qemu.raw             the raw GPT disk OVMF boots through its ESP (always built)
    azure.vhd            with the azure target
    gcp.tar.gz           with the gcp target
    payload.*.raw        the verity root and hash partitions, when the root is verity
    packages.json        mkosi's package manifest
    measurements.json    per target and boot path: predictions, unknowns, the policy
    sbom.spdx.json       the SBOM, bound to the lockfile and the package manifest
    inputs/              tundravm.lock, summary.json (``mkosi summary --json``, normalised),
                         sources.json (the pins), toolchain.json, mkosi-argv.json
    tree/                the mkosi project it was built from
    manifest.json        the identity (:mod:`tundravm.manifest`); the id is its digest
    manifest.sig         with ``build --sign KEY``
    observations.json    timestamps, runner, reproduction attempts: not identity

A build runs in ``BUILD/<image>/.work/`` (one workspace per image, so the fetched
sources and mkosi's tools tree and cache are reused), assembles the directory in a
scratch sibling, and publishes it with one ``rename``; files are made read-only and
nothing writes into a published directory again. Building the same identity twice
finds the directory there and leaves it alone. ``latest-production`` (and
``latest-development``) point at the newest of each.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import platform
import re
import shutil
import socket
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from . import __version__
from . import manifest as mf
from ._reporting import Event, Reporter
from ._source import (
    SOURCES_DIRNAME,
    CargoBuild,
    GoBuild,
    LocalSource,
    SourceBuild,
    toolchain_record,
)
from ._ssh_sig import SigRunner
from .backends.base import ARTIFACT_NAMES, BOOT_PAYLOAD_NAME, BuildBackend, is_mkosi_state
from .compiler.emit_mkosi import KERNEL_RECORDS as emitted_kernel_records
from .declarative._compile import emit, mkosi_argv
from .declarative._lowered import Lowered
from .declarative.lifecycle import (
    Artifact,
    Lock,
    Measurements,
    bake_image,
    lock_image,
    measure,
    read_lock,
    write_lock,
)
from .diagnostics import Diagnostic
from .errors import (
    ArtifactError,
    LockfileError,
    MeasurementError,
    StateError,
    TdxError,
    ValidationError,
)
from .models import OutputTarget
from .project import lock_command

Kind = Literal["production", "development"]
KINDS: tuple[Kind, ...] = ("production", "development")
TARGETS: tuple[OutputTarget, ...] = ("qemu", "azure", "gcp")
LATEST: dict[Kind, str] = {"production": "latest-production", "development": "latest-development"}
WORK = ".work"
REPRODUCE = ".reproduce"
LOCK_NAME = "tundravm.lock"
SCHEMES: dict[OutputTarget, Literal["qemu", "gcp", "azure"]] = {
    "qemu": "qemu",
    "azure": "azure",
    "gcp": "gcp",
}
HOST_SETTINGS = frozenset(
    {
        "Environment",
        "EnvironmentFiles",
        "PassEnvironment",
        "ProxyUrl",
        "ProxyClientCertificate",
        "ProxyClientKey",
        "ProxyPeerCertificate",
        "ProxyExclude",
        "WorkspaceDirectory",
        "CacheDirectory",
        "PackageCacheDirectory",
        "BuildDirectory",
        "OutputDirectory",
        "LocalMirror",
        "ExtraSearchPaths",
    }
)
"""``summary --json`` keys that describe this host, not the image: left out of
``inputs/summary.json``."""

RTMR3 = "outside the policy: no event schema is defined for RTMR3"


@dataclass(frozen=True, slots=True)
class Build:
    """A published build directory and its manifest."""

    path: Path
    manifest: Mapping[str, Any]

    @classmethod
    def open(cls, path: Path) -> Build:
        return cls(Path(path), mf.load(Path(path)))

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    @property
    def image(self) -> str:
        return str(self.manifest["image"])

    @property
    def profile(self) -> str:
        return str(self.manifest["profile"])

    @property
    def kind(self) -> Kind:
        return cast(Kind, self.manifest["kind"])

    @property
    def production(self) -> bool:
        return self.kind == "production"

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(self.manifest.get("targets", ()))

    def artifact(self, target: str) -> Path:
        """The file a *target* boots: ``qemu.raw``, ``azure.vhd``, ``gcp.tar.gz``."""
        if target not in self.targets:
            raise ValidationError(
                f"Build {self.id} has no {target} artifact (targets: {', '.join(self.targets)}).",
                hint=f"Add {target!r} to the image's targets= if it is not there, then "
                f"`tundravm build --target {target}`.",
                context={"build": str(self.path)},
            )
        return self.path / ARTIFACT_NAMES[cast(OutputTarget, target)]

    def runtime(self) -> dict[str, Any]:
        """``/etc/tundravm/runtime.json`` of the build's tree: its volumes, secrets and
        management port (empty when the image declares none)."""
        found = list((self.path / mf.TREE).glob("**/mkosi.extra/etc/tundravm/runtime.json"))
        if not found:
            return {}
        payload = json.loads(sorted(found)[0].read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def tcp_ports(self) -> tuple[int, ...]:
        """The TCP ports the image's firewall accepts inbound (``tcp dport N accept`` in its
        input chain) and the ones its services declare they listen on (the manifest's
        ``ports``)."""
        ports: set[int] = {int(p) for p in self.manifest.get("ports", ())}
        for conf in (self.path / mf.TREE).glob("**/mkosi.extra/etc/nftables.conf"):
            chain = None
            for line in conf.read_text(encoding="utf-8").splitlines():
                words = line.split()
                if words[:1] == ["chain"] and len(words) > 1:
                    chain = words[1]
                if chain != "input":
                    continue
                if "tcp" in words and "dport" in words and "accept" in words:
                    rest = words[words.index("dport") + 1 : words.index("accept")]
                    text = " ".join(rest).strip("{} ")
                    ports.update(int(p) for p in text.replace(",", " ").split() if p.isdigit())
        return tuple(sorted(ports))


@dataclass(frozen=True, slots=True)
class Outcome:
    """What :func:`build` did: the build, whether it is new, whether the lock was written,
    the reproduction record (``None`` without ``--reproduce``) and the Local sources a
    development build re-pinned in the lock."""

    build: Build
    created: bool
    lock_written: bool
    reproduction: Mapping[str, Any] | None = None
    repinned: tuple[str, ...] = ()

    @property
    def reproducible(self) -> bool | None:
        if self.reproduction is None:
            return None
        return self.reproduction.get("state") == "matched"


# ── selection ────────────────────────────────────────────────────────────


def choose_profile(img: Lowered, profile: str | None, *, dev: bool) -> str:
    """``--profile P``, ``--dev`` (the ``dev`` profile) or the image's default profile."""
    if profile is not None and dev:
        raise ValidationError(
            "--profile and --dev choose the profile twice.", hint="Pass one of them."
        )
    if dev:
        profile = "dev"
        if profile not in img.state.profiles:
            raise ValidationError(
                "The image has no dev profile.",
                hint="Give it one with `dev=DevAccess(...)`, or name a profile with --profile.",
            )
    chosen = profile or img.default_profile
    img.select([chosen])
    return chosen


def kind_of(img: Lowered, profile: str) -> Kind:
    return "development" if profile in img.unreleased else "production"


def choose_targets(
    img: Lowered, profile: str, given: Sequence[str] | None
) -> tuple[OutputTarget, ...]:
    """The targets a build makes: ``--target`` (or the profile's), ``qemu`` always.

    ``--target`` picks among the targets the image declares (``targets=``): a cloud's glue
    is part of the payload and its wrapper part of the tree, so a target the image does not
    declare cannot be built from it. The order is not part of the identity.
    """
    declared = tuple(img.targets.get(profile, ("qemu",)))
    wanted = list(given) if given else list(declared)
    unknown = [target for target in wanted if target not in TARGETS]
    if unknown:
        raise ValidationError(
            f"Unknown target(s): {', '.join(unknown)}.",
            hint=f"Targets: {', '.join(TARGETS)} (comma-separated: --target qemu,gcp).",
        )
    undeclared = [target for target in wanted if target not in (*declared, "qemu")]
    if undeclared:
        raise ValidationError(
            f"The {profile} profile does not target {', '.join(undeclared)} "
            f"(its targets: {', '.join(declared)}).",
            hint=f"Add {undeclared[0]!r} to the image's targets=: its cloud glue is part of "
            "the payload, so a target is declared, not chosen at build time.",
        )
    chosen = {"qemu", *wanted}
    if set(declared) == chosen:
        return declared
    return tuple(target for target in TARGETS if target in chosen)


def parse_targets(raw: Sequence[str] | None) -> tuple[str, ...] | None:
    """``--target`` values (repeatable, each comma-separated) as one tuple."""
    if not raw:
        return None
    return tuple(part.strip() for item in raw for part in item.split(",") if part.strip())


# ── build ────────────────────────────────────────────────────────────────


def build(
    img: Lowered,
    *,
    root: Path,
    profile: str,
    targets: Sequence[str] | None = None,
    offline: bool = False,
    reproduce: bool = False,
    sign_key: Path | None = None,
    clean_cache: bool = False,
    reporter: Reporter | None = None,
    sign_runner: SigRunner | None = None,
    ports: Sequence[int] = (),
) -> Outcome:
    """Build *profile* of *img* into an immutable directory under *root* (the build root).

    The lockfile ``root/tundravm.lock`` must be current: a stale one is refused (``lock``
    or ``lock --update`` changes it). A development build first re-pins the Local sources
    whose directory changed, which needs no network (:attr:`Outcome.repinned`); a
    production build never changes the lock. A production build is a locked build,
    so a missing lockfile refuses it (:func:`lock_missing`); a development build creates it
    first (refused with *offline*, which builds only from what the lock pins). *reproduce*
    builds again in an independent workspace and records the comparison in
    ``observations.json``; the directory is published either way and :attr:`Outcome.
    reproducible` says how it went. *sign_key* writes ``manifest.sig``. *ports* are the
    TCP ports the profile's services declare (recorded as the manifest's ``ports``, which
    ``run`` forwards besides the firewall's).
    """
    chosen = choose_targets(img, profile, targets)
    scoped = replace(img, targets={**dict(img.targets), profile: chosen})
    lockfile = root / LOCK_NAME
    locked, written, repinned = _current_lock(img, scoped, profile, lockfile, offline=offline)
    project_sha256 = mf.sha256_file(lockfile)
    kind = kind_of(scoped, profile)
    image_root = root / scoped.image_id
    work = image_root / WORK
    started = time.monotonic()
    started_at = _now()
    recorder = _Recorder(reporter)
    with _exclusive(image_root), tempfile.TemporaryDirectory(prefix="tundravm-lock-") as tmp:
        frozen = Path(tmp) / LOCK_NAME
        write_lock(locked, frozen)
        if clean_cache and work.exists():
            _remove(work)
        _reset(work)
        _, artifacts = bake_image(
            scoped,
            [profile],
            locked=locked,
            backend=None,
            build=work,
            reporter=recorder,
            lock_source=frozen,
            offline=offline,
        )
        measuring = time.monotonic()
        measurements = _measurements(artifacts)
        recorder.phases["measurements"] = time.monotonic() - measuring
        reproduction = None
        if reproduce:
            reproduction = _reproduce(
                scoped, profile, locked, frozen, image_root, artifacts, offline, reporter
            )
        incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=_mkdir(image_root / kind)))
        try:
            payload = _assemble(
                scoped,
                profile,
                kind,
                chosen,
                locked,
                project_sha256,
                work,
                artifacts,
                measurements,
                incoming,
                ports,
            )
            if sign_key is not None:
                (incoming / mf.SIGNATURE).write_bytes(
                    mf.sign(incoming, sign_key, runner=sign_runner)
                )
            observations = {
                "schema": mf.OBSERVATIONS_SCHEMA,
                "id": payload["id"],
                "started_at": started_at,
                "finished_at": _now(),
                "duration_s": round(time.monotonic() - started, 3),
                "runner": _runner(scoped.backend),
                "lock_written": written,
                "reproduction": reproduction,
                "phases": recorder.durations(),
                "sources": recorder.sources,
            }
            mf.write_canonical(incoming / mf.OBSERVATIONS, observations)
            _freeze(incoming)
            final = image_root / kind / str(payload["id"])
            created = _publish(incoming, final)
        finally:
            if incoming.exists():
                _remove(incoming)
        _point(image_root / LATEST[kind], final)
    return Outcome(Build.open(final), created, written, reproduction, repinned)


PHASES = {"fetch": "checkouts", "prefetch": "prefetch", "build": "mkosi"}
"""The bake phases ``observations.json`` records the duration of, by the name it records."""


class _Recorder:
    """A reporter that forwards every event to *reporter* and keeps what the build report
    reads from ``observations.json``: how long each phase of :data:`PHASES` took, and per
    source whether its checkout and its dependency cache were fetched or found."""

    def __init__(self, reporter: Reporter | None) -> None:
        self._reporter = reporter
        self.phases: dict[str, float] = {}
        self.sources: dict[str, dict[str, str]] = {}

    def emit(self, event: Event) -> None:
        extra = event.extra
        if event.kind == "phase" and extra.get("status") == "ok" and extra.get("phase") in PHASES:
            name = PHASES[extra["phase"]]
            self.phases[name] = self.phases.get(name, 0.0) + float(extra.get("duration_s", 0))
        if "name" in extra and ("checkout" in extra or "deps" in extra):
            found = self.sources.setdefault(extra["name"], {})
            found.update({key: extra[key] for key in ("checkout", "deps") if key in extra})
        if self._reporter is not None:
            self._reporter.emit(event)

    def durations(self) -> dict[str, float]:
        return {name: round(seconds, 3) for name, seconds in self.phases.items()}


def _current_lock(
    img: Lowered, scoped: Lowered, profile: str, lockfile: Path, *, offline: bool
) -> tuple[Lock, bool, tuple[str, ...]]:
    """The lock a build of *scoped* (*img* with the targets it builds) freezes against,
    whether this build wrote the project lock, and the Local sources it re-pinned there.

    The project lock is the image's as declared: a missing one refuses a production
    build and is written from *img* for a development one, which also re-pins in it the
    Local sources whose directory changed (:func:`_repin_local`); a stale one is refused. The
    outputs are part of the locked payload, so a build whose targets differ from the
    declared ones (``qemu`` is always built; ``--target``) freezes against a lock derived
    from the project's for *scoped*: every pin and the toolchain kept, nothing resolved,
    the digests recomputed for the targets built. A lock already
    current for *scoped* (a build's own ``inputs/tundravm.lock``, which ``verify --rebuild``
    builds against) is used as it is.
    """
    if not lockfile.is_file():
        if kind_of(img, profile) == "production":
            missing = lock_missing(lockfile)
            raise LockfileError(
                f"{missing.message} ({missing.code}).",
                hint=missing.hint,
                context={"path": str(lockfile)},
            )
        if offline:
            raise LockfileError(
                f"No lockfile at {lockfile}: an offline build builds only what the lock pins.",
                hint=f"Run `{lock_command()}` with the network first.",
                context={"path": str(lockfile)},
            )
        locked = lock_image(img, img.profile_names)
        write_lock(locked, lockfile)
        written = True
        repinned: tuple[str, ...] = ()
    else:
        locked = read_lock(lockfile)
        written = False
        repinned = ()
        if kind_of(img, profile) == "development":
            locked, repinned = _repin_local(img, locked)
            if repinned:
                write_lock(locked, lockfile)
        drift = img.select([profile]).lock_status(lockfile)
        if not drift.is_clean:
            if scoped.select([profile]).lock_status(lockfile).is_clean:
                return locked, False, repinned
            raise LockfileError(
                f"The lockfile {lockfile} is stale:\n{drift.render()}",
                hint="A build never updates the lock except to re-pin a development build's "
                f"Local sources: run `{lock_command()}` (or `{lock_command('--update', 'NAME')}` "
                "to move one pin), review the change, then build.",
                context={"path": str(lockfile)},
            )
    if tuple(img.targets.get(profile, ("qemu",))) != tuple(scoped.targets[profile]):
        locked = lock_image(scoped, scoped.profile_names, previous=locked, offline=True)
    return locked, written, repinned


def _repin_local(img: Lowered, locked: Lock) -> tuple[Lock, tuple[str, ...]]:
    """*locked* with each Local source whose directory changed pinned at its digest now,
    and their lockfile names; every other entry is kept as it is."""
    sources = img.select(img.profile_names).lock_sources()
    fetches = []
    moved: list[str] = []
    for fetch in locked.lockfile.fetches:
        spec = sources.get(fetch.name or "")
        if spec is not None and isinstance(spec.source, LocalSource) and spec.matches(fetch):
            digest = spec.source.digest()
            if digest != fetch.digest:
                fetch = replace(fetch, digest=digest)
                moved.append(spec.key)
        fetches.append(fetch)
    if not moved:
        return locked, ()
    return Lock.of(replace(locked.lockfile, fetches=fetches)), tuple(moved)


def lock_missing(lockfile: Path) -> Diagnostic:
    """The ``lock-missing`` finding a production build without *lockfile* refuses with."""
    return Diagnostic(
        "lock-missing",
        "error",
        f"No lockfile at {lockfile}: a production build builds only what a lock pins",
        subject=str(lockfile),
        hint=f"Run `{lock_command()}`, review and commit the lockfile, then build.",
    )


@contextlib.contextmanager
def _exclusive(image_root: Path) -> Iterator[None]:
    """One build of an image at a time (they share the workspace)."""
    _mkdir(image_root)
    with open(image_root / ".lock", "w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateError(
                f"Another build of this image is running in {image_root}.",
                hint="Wait for it to finish; builds of one image share its workspace.",
                context={"path": str(image_root)},
            ) from None
        yield


def _reset(work: Path) -> None:
    """Empty *work* of the previous build's outputs; keep its sources and mkosi state (the
    tools tree mkosi leaves beside the tree's config, its caches)."""
    _mkdir(work)
    for entry in work.iterdir():
        if entry.name in (".sources", ".mkosi"):
            continue
        if entry.name == "mkosi" and entry.is_dir() and not entry.is_symlink():
            for child in entry.iterdir():
                if not is_mkosi_state(child.name):
                    _discard(child)
            continue
        _discard(entry)


def _discard(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        _remove(entry)
    else:
        entry.unlink()


def _reproduce(
    img: Lowered,
    profile: str,
    locked: Lock,
    lockfile: Path,
    image_root: Path,
    first: Sequence[Artifact],
    offline: bool,
    reporter: Reporter | None,
) -> dict[str, Any]:
    """Build again in ``.reproduce/`` (its own sources, state and tree) and compare."""
    second_root = image_root / REPRODUCE
    if second_root.exists():
        _remove(second_root)
    started_at = _now()
    try:
        _, second = bake_image(
            img,
            [profile],
            locked=locked,
            backend=None,
            build=second_root,
            reporter=reporter,
            lock_source=lockfile,
            offline=offline,
        )
    except KeyboardInterrupt:
        raise
    except TdxError as exc:
        return {
            "state": "error",
            "started_at": started_at,
            "error": f"{type(exc).__name__}: {exc}".splitlines()[0],
            "workspace": REPRODUCE,
        }
    rows = _digests(first)
    again = _digests(second)
    compared = {
        name: {"first": rows.get(name), "second": again.get(name)}
        for name in sorted({*rows, *again})
    }
    differ = sorted(name for name, pair in compared.items() if pair["first"] != pair["second"])
    kept = bool(differ)
    if not differ:
        try:
            _remove(second_root)
        except StateError:
            kept = True  # root-owned files of a sudo build; the next --reproduce says so
    return {
        "state": "different" if differ else "matched",
        "started_at": started_at,
        "finished_at": _now(),
        "workspace": REPRODUCE if kept else None,
        "artifacts": compared,
        "differ": differ,
    }


def _digests(artifacts: Sequence[Artifact]) -> dict[str, str]:
    found: dict[str, str] = {}
    for artifact in artifacts:
        found[ARTIFACT_NAMES[artifact.target]] = artifact.sha256
        if artifact.boot_payload_sha256:
            found[BOOT_PAYLOAD_NAME] = artifact.boot_payload_sha256
    return found


# ── assembly ─────────────────────────────────────────────────────────────


def _assemble(
    img: Lowered,
    profile: str,
    kind: Kind,
    targets: Sequence[OutputTarget],
    locked: Lock,
    project_sha256: str,
    work: Path,
    artifacts: Sequence[Artifact],
    measurements: Mapping[str, Any],
    out: Path,
    ports: Sequence[int] = (),
) -> dict[str, Any]:
    """Fill *out* from the workspace *work*; return the manifest it wrote."""
    unchecked: list[str] = []
    by_target = {artifact.target: artifact for artifact in artifacts}
    missing_targets = [t for t in targets if t not in by_target]
    if missing_targets:
        raise ArtifactError(
            f"The build produced no {', '.join(missing_targets)} artifact.",
            hint="Run the build with --verbose and read mkosi's output.",
            context={"workspace": str(work)},
        )
    qemu = _with_package_manifest(by_target["qemu"])
    sbom_record = _sbom(qemu, locked, img, out)
    packages = _packages(qemu, out)
    tree_src = work / "mkosi"

    def state(directory: str, names: list[str]) -> list[str]:
        """mkosi's state beside the config (the tools tree, caches): not the tree."""
        rel = Path(directory).relative_to(tree_src)
        return [name for name in names if is_mkosi_state((rel / name).as_posix())]

    shutil.copytree(tree_src, out / mf.TREE, symlinks=True, ignore=state)
    inputs = _mkdir(out / mf.INPUTS)
    (inputs / LOCK_NAME).write_text(locked.text(), encoding="utf-8")
    argv = mkosi_argv(img, profile)
    mf.write_canonical(inputs / "mkosi-argv.json", {"argv": argv})
    summary = _summary(out / mf.TREE, argv, work, inputs / "summary.json")
    toolchains = _toolchains(img, profile, locked, work)
    sources = [
        {
            "name": f.name,
            "kind": f.kind,
            "source": f.source,
            "ref": f.ref,
            "pin": f.digest,
            **toolchains.get(str(f.name), {}),
        }
        for f in locked.lockfile.fetches
    ]
    mf.write_canonical(inputs / "sources.json", {"sources": sources})
    toolchain = {
        "tundravm": __version__,
        "backend": None if img.backend is None else img.backend.name,
        "locked": None
        if locked.lockfile.toolchain is None
        else locked.lockfile.toolchain.payload(),
    }
    mf.write_canonical(inputs / "toolchain.json", toolchain)
    moved: set[Path] = set()
    for artifact in artifacts:
        _move(artifact.path, out / ARTIFACT_NAMES[artifact.target], moved)
        if artifact.boot_payload is not None:
            _move(Path(artifact.boot_payload), out / mf.UKI, moved)
        for split in sorted(Path(artifact.path).parent.glob("payload.*.raw")):
            _move(split, out / split.name, moved)
    kernel = _kernel_records(artifacts, out, moved)
    mf.write_canonical(out / mf.MEASUREMENTS, measurements)
    uki = out / mf.UKI
    root_hash = mf.uki_root_hash(uki) if uki.is_file() else None
    if img.mkosi.root == "verity" and root_hash is None:
        unchecked.append("root hash: the UKI's command line carries none")
    states = {_record_name(r): str(r["state"]) for r in measurements["records"]}
    unchecked.extend(
        f"measurements {name}: {state}"
        for name, state in sorted(states.items())
        if state != "predicted"
    )
    unchecked.append("RTMR3: " + RTMR3)
    if kind == "development":
        unchecked.append("development build: never production evidence")
    tree, files = mf.tree_digest(out / mf.TREE)
    payload: dict[str, Any] = {
        "schema": mf.SCHEMA,
        "image": img.image_id,
        "profile": profile,
        "kind": kind,
        "targets": list(targets),
        "ports": sorted(set(ports)),
        "rootfs": img.mkosi.root,
        "rootfs_max_size": img.mkosi.root_size,
        "image_digest": locked.recipe_digest,
        "artifacts": mf.artifact_index(out),
        "uki": {
            "path": mf.UKI,
            **mf.file_record(uki),
            "cmdline": mf.uki_cmdline(uki),
            "authenticode_sha384": mf.uki_authenticode(uki),
        }
        if uki.is_file()
        else mf.missing("the build produced no payload.efi"),
        "root_hash": root_hash,
        "lock": {
            "path": f"{mf.INPUTS}/{LOCK_NAME}",
            "sha256": mf.sha256_file(inputs / LOCK_NAME),
            "image_digest": locked.recipe_digest,
            "project_sha256": project_sha256,
        },
        "tree": {"path": mf.TREE, "digest": tree, "files": files},
        "sources": sources,
        "packages": packages,
        "sbom": sbom_record,
        "measurements": {
            "path": mf.MEASUREMENTS,
            "sha256": mf.sha256_file(out / mf.MEASUREMENTS),
            "records": states,
        },
        "summary": summary,
        "toolchain": toolchain,
        "unchecked": unchecked,
        "probe": img.probes[profile].manifest() if profile in img.probes else [],
    }
    if kernel:
        payload["kernel"] = kernel
    payload["id"] = mf.build_id(payload)
    mf.write_canonical(out / mf.MANIFEST, payload)
    return payload


_TOOLCHAIN_MISSING = {
    GoBuild: (
        "go_toolchain",
        "no prefetch recorded it: the host had no `go`, or its module download failed",
    ),
    CargoBuild: (
        "rust_toolchain",
        "no prefetch recorded it: the host had no `cargo` or `rustup`, or a download failed",
    ),
}
"""The recipes whose prefetch records a toolchain: its key in the source, and why it is missing."""


def _toolchains(img: Lowered, profile: str, locked: Lock, work: Path) -> dict[str, Any]:
    """Each Go and Cargo source build of *profile* by lock name: ``{go_toolchain: ...}`` or
    ``{rust_toolchain: ...}``, the toolchain its prefetch recorded."""
    pins = {f.name: f for f in locked.lockfile.fetches if f.name is not None}
    found: dict[str, Any] = {}
    for spec in img.select([profile]).lock_sources().values():
        if not isinstance(spec, SourceBuild) or type(spec.build) not in _TOOLCHAIN_MISSING:
            continue
        key, why = _TOOLCHAIN_MISSING[type(spec.build)]
        pin = spec.pin_from(pins)
        record = None if pin is None else toolchain_record(spec, pin, work / SOURCES_DIRNAME)
        found[spec.key] = {key: record or mf.missing(why)}
    return found


KERNEL_RECORDS = dict(
    zip(
        emitted_kernel_records,
        (
            ("config_before", "kernel/config.before"),
            ("config_after", "kernel/config.after"),
            ("drivers", "kernel/drivers.txt"),
        ),
        strict=True,
    )
)
"""A built kernel's records in mkosi's output directory: the manifest's ``kernel`` key for
each and its path in the build directory."""


def _kernel_records(artifacts: Sequence[Artifact], out: Path, moved: set[Path]) -> dict[str, Any]:
    """Move a built kernel's records into ``kernel/`` of *out*; the manifest's ``kernel``
    section naming each, empty when the build compiled no kernel."""
    found: dict[str, Any] = {}
    for directory in sorted({Path(artifact.path).parent for artifact in artifacts}):
        for name, (key, rel) in KERNEL_RECORDS.items():
            source = directory / name
            if key in found or not source.is_file():
                continue
            _mkdir((out / rel).parent)
            _move(source, out / rel, moved)
            found[key] = {"path": rel, **mf.file_record(out / rel)}
    return found


def _record_name(record: Mapping[str, Any]) -> str:
    return f"{record['target']}/{record['boot']}"


def _move(source: Path, target: Path, moved: set[Path]) -> None:
    if source in moved or not source.exists():
        return
    moved.add(source)
    os.replace(source, target)


def _measurements(artifacts: Sequence[Artifact]) -> dict[str, Any]:
    """One record per target and boot path; a prediction or an explicit state."""
    records: list[dict[str, Any]] = []
    for artifact in sorted(artifacts, key=lambda a: TARGETS.index(a.target)):
        paths: list[tuple[str, Artifact]] = [("disk", artifact)]
        if artifact.target == "qemu" and artifact.boot_payload and artifact.boot_payload_sha256:
            uki = replace(
                artifact,
                path=Path(artifact.boot_payload),
                sha256=artifact.boot_payload_sha256,
                boot_payload=None,
                boot_payload_sha256=None,
            )
            paths.insert(0, ("uki", uki))
        for boot, subject in paths:
            name = mf.UKI if boot == "uki" else ARTIFACT_NAMES[artifact.target]
            base: dict[str, Any] = {
                "target": artifact.target,
                "boot": boot,
                "artifact": name,
                "artifact_sha256": subject.sha256,
            }
            try:
                found = measure(subject, scheme=SCHEMES[artifact.target])
            except (MeasurementError, ArtifactError) as exc:
                records.append({**base, "state": "unknown", "reason": str(exc)})
                continue
            records.append({**base, **_prediction(found, name)})
    return {"schema": mf.MEASUREMENTS_SCHEMA, "records": records}


def _prediction(found: Measurements, name: str) -> dict[str, Any]:
    portable = replace(
        found,
        artifact_path=name,
        boot_payload_path=mf.UKI if found.boot_payload_digest else "",
    )
    try:
        policy: dict[str, Any] | None = portable.to_policy()
    except TdxError:
        policy = None
    return {
        "state": "predicted",
        "scheme": found.scheme,
        "tool": found.tool,
        "registers": {key: list(values) for key, values in found.registers},
        "unknowns": dict(found.unverifiable),
        "rtmr3": RTMR3,
        "uki": {"path": mf.UKI, "sha256": found.boot_payload_digest}
        if found.boot_payload_digest
        else None,
        "policy": policy,
    }


PAYLOAD_MANIFEST = "payload.manifest"
"""What mkosi calls the package manifest of the payload subimage, beside ``qemu.raw``."""


def _package_manifest(artifact: Artifact) -> Path | None:
    """The package manifest this build's mkosi wrote: the variant's, else the payload's."""
    from .declarative.bom import manifest_path

    for candidate in (manifest_path(artifact), Path(artifact.path).parent / PAYLOAD_MANIFEST):
        if candidate.is_file():
            return candidate
    return None


def _with_package_manifest(artifact: Artifact) -> Artifact:
    """*artifact* bound to the payload's package manifest when the bake recorded none: the
    payload subimage writes it under its own name in the same output directory."""
    found = _package_manifest(artifact)
    if artifact.manifest_sha256 is not None or found is None:
        return artifact
    return replace(artifact, manifest_sha256=mf.sha256_file(found))


def _sbom(artifact: Artifact, locked: Lock, img: Lowered, out: Path) -> dict[str, Any]:
    from .declarative.bom import sbom

    previous = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = str(img.epoch or 0)
    try:
        document = sbom(artifact, lock=locked, manifest=_package_manifest(artifact))
    except TdxError as exc:
        return mf.missing(str(exc).splitlines()[0])
    finally:
        if previous is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous
    (out / mf.SBOM).write_text(document.render(), encoding="utf-8")
    return {
        "path": mf.SBOM,
        "sha256": mf.sha256_file(out / mf.SBOM),
        "bound": document.bound,
        "notes": list(document.notes),
    }


def _packages(artifact: Artifact, out: Path) -> dict[str, Any]:
    found = _package_manifest(artifact)
    if found is None:
        return mf.missing("mkosi wrote no package manifest")
    shutil.copyfile(found, out / mf.PACKAGES)
    try:
        packages = json.loads(found.read_text(encoding="utf-8")).get("packages", [])
    except (ValueError, AttributeError):
        packages = []
    return {
        "path": mf.PACKAGES,
        "sha256": mf.sha256_file(out / mf.PACKAGES),
        "count": len(packages),
    }


def _summary(tree: Path, argv: Sequence[str], work: Path, target: Path) -> dict[str, Any]:
    """``mkosi summary --json`` of the build's tree, normalised, as ``inputs/summary.json``."""
    try:
        from . import _mkosi
    except ImportError as exc:
        return mf.missing(f"mkosi does not import on this host: {exc}")
    try:
        found = _mkosi.summary(tree.resolve(), extra_argv=argv)
    except (_mkosi.MkosiError, OSError) as exc:
        said = [line for line in getattr(exc, "log_text", str(exc)).splitlines() if line.strip()]
        reason = (said[-1] if said else type(exc).__name__).replace(str(tree.resolve()), "$TREE")
        return mf.missing(f"mkosi summary failed: {reason.lstrip('‣ ')}")
    places = {
        str(tree.resolve()): "$TREE",
        str(work.resolve()): "$WORK",
        str(Path.home()): "$HOME",
    }
    images = [_normalised(image, places) for image in found.images]
    tools = None if found.tools is None else _normalised(found.tools, places)
    mf.write_canonical(target, {"Images": images, "Tools": tools})
    return {"path": f"{mf.INPUTS}/{target.name}", "sha256": mf.sha256_file(target)}


def _normalised(value: Any, places: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _normalised(v, places) for k, v in value.items() if k not in HOST_SETTINGS}
    if isinstance(value, list):
        return [_normalised(item, places) for item in value]
    if isinstance(value, str):
        for prefix, name in places.items():
            if value == prefix or value.startswith(prefix + "/"):
                return name + value[len(prefix) :]
        return _RESOURCES.sub("$MKOSI_RESOURCES/", value)
    return value


_RESOURCES = re.compile(r"^/\S*?/resources/(?=mkosi-)")
"""mkosi's own resources (the default tools tree), unpacked to a temporary directory."""


def _runner(backend: BuildBackend | None) -> dict[str, Any]:
    ci = {
        key: os.environ[key]
        for key in ("GITHUB_RUN_ID", "GITHUB_SHA", "GITHUB_REPOSITORY", "RUNNER_NAME")
        if key in os.environ
    }
    try:
        user: str | None = getpass.getuser()
    except (OSError, KeyError):
        user = None
    return {
        "hostname": socket.gethostname(),
        "user": user,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "backend": None if backend is None else backend.name,
        "ci": ci or None,
    }


def _freeze(root: Path) -> None:
    """Drop every write bit of the files under *root* (directories stay removable)."""
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _publish(incoming: Path, final: Path) -> bool:
    """Rename *incoming* to *final*; ``False`` when that identity is already published."""
    if final.exists():
        return False
    os.rename(incoming, final)
    return True


def _point(link: Path, final: Path) -> None:
    """Point *link* at *final* (relative), atomically."""
    scratch = link.with_name(f".{link.name}.{os.getpid()}")
    scratch.unlink(missing_ok=True)
    scratch.symlink_to(final.relative_to(link.parent))
    os.replace(scratch, link)


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _remove(path: Path) -> None:
    def writable(function: Any, target: str, _: object) -> None:
        os.chmod(os.path.dirname(target), 0o755)
        function(target)

    try:
        shutil.rmtree(path, onexc=writable)
    except OSError as exc:
        raise StateError(
            f"Cannot remove {path}: {exc.strerror or exc}.",
            hint="A build that ran mkosi with sudo leaves root-owned files (its tools tree); "
            f"remove them as their owner: `sudo rm -rf -- {path}`.",
            context={"path": str(exc.filename or path)},
        ) from exc


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── tree-only ────────────────────────────────────────────────────────────


def export_tree(img: Lowered, profiles: Sequence[str] | None, destination: Path) -> tuple[str, ...]:
    """Write the standalone mkosi project of *profiles* (default: every profile) to
    *destination*; returns the ``mkosi`` options that build each profile from it."""
    names = tuple(profiles) if profiles else img.profile_names
    selected = img.select(names)
    emit(selected, destination)
    return tuple(" ".join(mkosi_argv(selected, name)) for name in names)


# ── reading builds ───────────────────────────────────────────────────────


def builds_of(root: Path, image: str | None = None) -> list[Build]:
    """Every published build under *root* (of *image*), newest first by publication time."""
    found: list[tuple[float, Build]] = []
    images = (
        [root / image]
        if image
        else [p for p in root.iterdir() if p.is_dir()]
        if root.is_dir()
        else []
    )
    for image_root in images:
        for kind in KINDS:
            directory = image_root / kind
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                if entry.name.startswith(".") or not (entry / mf.MANIFEST).is_file():
                    continue
                with contextlib.suppress(StateError):
                    found.append((entry.stat().st_mtime, Build.open(entry)))
    return [b for _, b in sorted(found, key=lambda pair: pair[0], reverse=True)]


def resolve(ref: str | None, *, root: Path, image: str | None, dev: bool = False) -> Build:
    """The build *ref* names: a directory, ``latest`` (``latest-production``, or
    ``latest-development`` with *dev*), a build id or its prefix; ``None`` is ``latest``."""
    if ref is not None and (Path(ref) / mf.MANIFEST).is_file():
        return Build.open(Path(ref))
    if ref is not None and ("/" in ref or Path(ref).exists()):
        raise StateError(
            f"{ref} is not a build directory (it holds no manifest.json).",
            hint="Pass BUILD as build/<image>/production/<id>, a build id or `latest`.",
            context={"build": ref},
        )
    kind: Kind = "development" if dev else "production"
    if ref is None or ref in ("latest", *LATEST.values()):
        if ref in LATEST.values():
            kind = "development" if ref == LATEST["development"] else "production"
        candidates = (
            [root / image / LATEST[kind]] if image else list(root.glob(f"*/{LATEST[kind]}"))
        )
        linked = [path for path in candidates if (path / mf.MANIFEST).is_file()]
        if len(linked) == 1:
            return Build.open(linked[0].resolve())
        flag = " --dev" if kind == "development" else ""
        raise StateError(
            f"No {kind} build under {root}/" + (f" for {image}" if image else "") + ".",
            hint=f"Run `tundravm build{flag}` first, or pass BUILD."
            if not linked
            else "Several images have builds here: pass BUILD.",
            context={"root": str(root)},
        )
    matches = [b for b in builds_of(root, image) if b.id.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    raise StateError(
        f"{'No' if not matches else 'More than one'} build matches {ref!r} under {root}/.",
        hint="Pass the build directory, a longer id prefix, or `latest`.",
        context={"root": str(root)},
    )


__all__ = [
    "KINDS",
    "LATEST",
    "REPRODUCE",
    "TARGETS",
    "WORK",
    "Build",
    "Kind",
    "Outcome",
    "build",
    "builds_of",
    "choose_profile",
    "choose_targets",
    "export_tree",
    "kind_of",
    "lock_missing",
    "parse_targets",
    "resolve",
]
