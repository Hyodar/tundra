"""What is in an image: :func:`sbom` merges three sources into one :class:`Sbom`.

1. mkosi's JSON package manifest (``ManifestFormat=json``, written next to the
   artifact as ``<variant>.manifest``): every distribution package with its
   version and architecture. Without one (a simulated bake, or a manifest that
   was removed) the packages the recipe declares are listed instead, unversioned.
2. The lockfile's pins: each source build, built kernel and ``EfiStub`` package
   with its URL, git ref and commit or sha256, plus the paths a build installs.
3. Recipe metadata: base, arch, snapshot, mirror, variant, the recipe, tree and
   artifact digests and the tundravm version.

:meth:`Sbom.render` writes SPDX 2.3 JSON, CycloneDX 1.5 JSON, a text report or
Markdown tables; every list is sorted, so the same inputs give the same document
(apart from the creation time, which ``SOURCE_DATE_EPOCH`` fixes).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import quote

from tundravm._source import GitSource, NamedSource
from tundravm.errors import ArtifactError, ValidationError
from tundravm.lockfile import LockedFetch

from ._bake import package_manifest
from ._embedded import embedded_dependencies
from ._lowered import Lowered
from .lifecycle import Artifact, Lock, verify_artifact

ComponentKind = Literal["package", "build", "kernel", "efi-stub", "go-module", "crate"]
EMBEDDED_KINDS: frozenset[str] = frozenset({"go-module", "crate"})
"""Component kinds read from a built binary (``Component.parent`` names the build)."""
SbomFormat = Literal["spdx-json", "cyclonedx-json", "text", "markdown"]
SBOM_FORMATS: tuple[SbomFormat, ...] = get_args(SbomFormat)
MANIFEST_VERSION = 1
"""The mkosi manifest version this parser reads (mkosi 26 writes ``manifest_version: 1``)."""

_KIND_ORDER: dict[str, int] = {
    "package": 0,
    "build": 1,
    "kernel": 2,
    "efi-stub": 3,
    "go-module": 4,
    "crate": 5,
}
_DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}
_FROM_MKOSI_ARCH = {"x86-64": "x86_64", "arm64": "aarch64"}
_PURL_TYPES = {"deb": "deb", "rpm": "rpm", "pkg": "alpm"}
_SPDX_ID = re.compile(r"[^A-Za-z0-9.-]+")
_NAMESPACE = uuid.UUID("6f1e0c52-2f6b-5b1c-9a57-7475e6472a76")


@dataclass(frozen=True, slots=True)
class Component:
    """One thing in the image: a distribution ``package``, a pinned source or an embedded one.

    Sources are a ``build`` (a source build), the ``kernel`` built from source
    (``kernel-<variant>`` when the variant's differs) or the ``efi-stub`` package.
    Embedded dependencies are the ``go-module`` and ``crate`` entries a build's binary
    records (Go build information, ``cargo auditable``); *parent* names that build.
    """

    kind: ComponentKind
    name: str
    version: str = ""
    """A package's version; a source's pin (its commit, or its sha256); empty when unknown."""
    arch: str = ""
    type: str = ""
    """The package type the manifest records (``deb``, ``rpm``, ``pkg``); empty for a source."""
    origin: str = ""
    """The repository a package came from, when the manifest records one (mkosi 26 does not)."""
    url: str = ""
    ref: str = ""
    """The git ref a source's commit was resolved from; empty for an http source."""
    sha256: str = ""
    """An http source's sha256 (its ``version`` too); empty for git."""
    installs: tuple[str, ...] = ()
    """The image paths a source build installs (its ``Install`` destinations)."""
    declared: bool = False
    """A package the recipe declares, listed because no manifest says what was installed."""
    parent: str = ""
    """The source build whose binary records this ``go-module`` or ``crate``."""

    @property
    def git(self) -> bool:
        """Whether this is a git source (it has a ref)."""
        return self.kind != "package" and not self.sha256 and bool(self.ref)

    @property
    def commit(self) -> str:
        """A git source's pinned commit; empty otherwise."""
        return self.version if self.git else ""


@dataclass(frozen=True, slots=True)
class UnboundBinary:
    """A binary ``binaries=`` read whose sha256 is not the one the bake recorded for its
    image path (or the bake recorded none): its embedded dependencies are listed here,
    with where they came from, and never as components of the image."""

    build: str
    dest: str
    """The image path the build installs it at."""
    path: str
    """Where it was read: under the ``binaries`` directory, at its image path or file name."""
    sha256: str
    recorded: str | None
    """The sha256 the bake recorded for ``dest``; ``None`` when it recorded none."""
    dependencies: tuple[Component, ...] = ()

    def describe(self) -> str:
        """One sentence: which binary, its sha256, what the bake recorded."""
        baked = (
            "the bake recorded no digest for it"
            if self.recorded is None
            else f"the bake recorded {self.recorded}"
        )
        return f"{self.build}'s {self.dest} read from {self.path} hashes to {self.sha256}; {baked}"


@dataclass(frozen=True, slots=True)
class Sbom:
    """A software bill of materials for one variant; :meth:`render` serializes it."""

    variant: str
    components: tuple[Component, ...]
    base: str = ""
    arch: str = ""
    snapshot: str | None = None
    mirror: str | None = None
    recipe_digest: str = ""
    tree_digest: str = ""
    artifact: Path | None = None
    """The artifact's path relative to the bake output directory (absolute when outside it)."""
    artifact_sha256: str = ""
    tundravm_version: str = ""
    manifest: Path | None = None
    """The mkosi manifest read, or where it was looked for when ``manifest_found`` is false
    (relative to the bake output directory for an artifact)."""
    manifest_found: bool = False
    bound: bool = True
    """Whether the materials match the bake: ``False`` when ``allow_unbound`` accepted a
    lockfile or manifest other than the bake's, or a material the bake recorded is gone
    (``missing``); the document is incomplete, and a note says why."""
    missing: tuple[str, ...] = ()
    """The materials the bake recorded that are gone (``manifest``); each makes it unbound."""
    created: str = ""
    """ISO 8601 UTC creation time: ``SOURCE_DATE_EPOCH`` when set, else when built."""
    notes: tuple[str, ...] = ()
    """What the document could not include, and why (missing manifest, no lockfile)."""
    unbound_binaries: tuple[UnboundBinary, ...] = ()
    """The ``binaries=`` whose sha256 the bake did not record: their dependencies are not
    components, and each makes the document unbound."""

    @property
    def complete(self) -> bool:
        """Whether the installed inventory (mkosi's manifest) is available: ``False`` when the
        packages listed are the declared ones, unversioned."""
        return self.manifest_found

    @property
    def packages(self) -> tuple[Component, ...]:
        return tuple(c for c in self.components if c.kind == "package")

    @property
    def sources(self) -> tuple[Component, ...]:
        return tuple(
            c for c in self.components if c.kind != "package" and c.kind not in EMBEDDED_KINDS
        )

    @property
    def embedded(self) -> tuple[Component, ...]:
        """The Go modules and crates the built binaries record."""
        return tuple(c for c in self.components if c.kind in EMBEDDED_KINDS)

    def render(self, format: SbomFormat = "spdx-json") -> str:
        """The document as *format*: ``spdx-json``, ``cyclonedx-json``, ``text`` or ``markdown``."""
        if format == "spdx-json":
            return json.dumps(self.to_spdx(), indent=2) + "\n"
        if format == "cyclonedx-json":
            return json.dumps(self.to_cyclonedx(), indent=2) + "\n"
        if format == "text":
            return _text(self)
        if format == "markdown":
            return _markdown(self)
        raise ValidationError(
            f"Unknown SBOM format {format!r}.",
            hint=f"Use one of: {', '.join(SBOM_FORMATS)}",
        )

    # ── SPDX 2.3 ────────────────────────────────────────────────────────

    def to_spdx(self) -> dict[str, Any]:
        """SPDX 2.3 JSON: the image package DESCRIBES, CONTAINS packages, GENERATED_FROM sources."""
        image = "SPDXRef-Image"
        packages: list[dict[str, Any]] = [self._spdx_image(image)]
        relationships: list[dict[str, str]] = [_relation("SPDXRef-DOCUMENT", "DESCRIBES", image)]
        builds = {c.name: _spdx_id(c) for c in self.components if c.kind == "build"}
        for component in self.components:
            ref = _spdx_id(component)
            packages.append(_spdx_package(component, ref, self._distro()))
            if component.kind in EMBEDDED_KINDS and component.parent in builds:
                relationships.append(_relation(builds[component.parent], "DEPENDS_ON", ref))
                continue
            kind = "CONTAINS" if component.kind == "package" else "GENERATED_FROM"
            relationships.append(_relation(image, kind, ref))
        creation: dict[str, Any] = {
            "created": self.created,
            "creators": [f"Tool: tundravm-{self.tundravm_version}"],
        }
        if self.notes:
            creation["comment"] = " ".join(self.notes)
        annotations = [
            {
                "annotationDate": self.created,
                "annotationType": "OTHER",
                "annotator": f"Tool: tundravm-{self.tundravm_version}",
                "comment": _unbound_comment(binary),
            }
            for binary in self.unbound_binaries
        ]
        return {
            **({"annotations": annotations} if annotations else {}),
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"tundravm-{self.variant}",
            "documentNamespace": (
                f"https://spdx.org/spdxdocs/tundravm-{_spdx_safe(self.variant)}-{self._uuid()}"
            ),
            "creationInfo": creation,
            "packages": packages,
            "relationships": relationships,
        }

    def _spdx_image(self, ref: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "SPDXID": ref,
            "name": self.variant,
            "versionInfo": self.artifact_sha256 or self.recipe_digest or "NOASSERTION",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "primaryPackagePurpose": "OPERATING-SYSTEM",
            "description": f"tundravm image, variant {self.variant}",
            "comment": "; ".join(f"{key} {value}" for key, value in self.metadata()),
        }
        if self.artifact is not None:
            entry["packageFileName"] = self.artifact.name
        if self.artifact_sha256:
            entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": self.artifact_sha256}]
        return entry

    # ── CycloneDX 1.5 ───────────────────────────────────────────────────

    def to_cyclonedx(self) -> dict[str, Any]:
        """CycloneDX 1.5 JSON: the image is ``metadata.component``, depending on each component."""
        image: dict[str, Any] = {
            "type": "operating-system",
            "bom-ref": "image",
            "name": self.variant,
            "properties": [
                {"name": f"tundravm:{key.replace(' ', '-')}", "value": value}
                for key, value in self.metadata()
            ],
        }
        if self.artifact_sha256:
            image["hashes"] = [{"alg": "SHA-256", "content": self.artifact_sha256}]
        metadata: dict[str, Any] = {
            "timestamp": self.created,
            "tools": {
                "components": [
                    {"type": "application", "name": "tundravm", "version": self.tundravm_version}
                ]
            },
            "component": image,
        }
        if self.notes:
            metadata["properties"] = [{"name": "tundravm:note", "value": n} for n in self.notes]
        components = [_cyclonedx_component(c, self._distro()) for c in self.components]
        refs = {c.name: _spdx_id(c).removeprefix("SPDXRef-") for c in self.components}
        nested: dict[str, list[str]] = {}
        for component, entry in zip(self.components, components, strict=True):
            if component.kind in EMBEDDED_KINDS and component.parent in refs:
                nested.setdefault(refs[component.parent], []).append(entry["bom-ref"])
        top = [
            entry["bom-ref"]
            for component, entry in zip(self.components, components, strict=True)
            if component.kind not in EMBEDDED_KINDS or component.parent not in refs
        ]
        tool = {"type": "application", "name": "tundravm", "version": self.tundravm_version}
        annotations = [
            {
                "subjects": ["image"],
                "annotator": {"component": tool},
                "timestamp": self.created,
                "text": _unbound_comment(binary),
            }
            for binary in self.unbound_binaries
        ]
        return {
            **({"annotations": annotations} if annotations else {}),
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "serialNumber": f"urn:uuid:{self._uuid()}",
            "version": 1,
            "metadata": metadata,
            "components": components,
            "dependencies": [
                {"ref": "image", "dependsOn": top},
                *({"ref": ref, "dependsOn": deps} for ref, deps in sorted(nested.items())),
            ],
        }

    # ── shared ──────────────────────────────────────────────────────────

    def metadata(self) -> tuple[tuple[str, str], ...]:
        """``(key, value)`` recipe metadata rows, in a fixed order; unknown values are ``-``."""
        rows = (
            ("variant", self.variant),
            ("base", self.base),
            ("arch", self.arch),
            ("snapshot", self.snapshot or ""),
            ("mirror", self.mirror or ""),
            ("recipe digest", self.recipe_digest),
            ("tree digest", self.tree_digest),
            ("artifact", "" if self.artifact is None else str(self.artifact)),
            ("artifact sha256", self.artifact_sha256),
            ("manifest", _manifest_cell(self)),
            ("bound", "yes" if self.bound else "no (incomplete)"),
            ("installed", "yes" if self.complete else "no (declared packages)"),
            ("tundravm", self.tundravm_version),
        )
        return tuple((key, value or "-") for key, value in rows)

    def _distro(self) -> tuple[str, str]:
        """``(namespace, distro qualifier)`` for package PURLs: ``debian``, ``debian-trixie``."""
        namespace = self.base.split("/", 1)[0] if self.base else ""
        return namespace, self.base.replace("/", "-")

    def _uuid(self) -> uuid.UUID:
        """A name-based UUID over everything but the creation time: same inputs, same id."""
        unbound = [_unbound_comment(b) for b in self.unbound_binaries]
        payload = json.dumps(
            [list(self.metadata()), [_key(c) for c in self.components], *unbound], sort_keys=True
        )
        return uuid.uuid5(_NAMESPACE, payload)


# ── building ─────────────────────────────────────────────────────────────


def sbom(
    subject: Artifact | Lowered,
    *,
    lock: Lock | None = None,
    manifest: Path | None = None,
    allow_unbound: bool = False,
    binaries: Path | None = None,
) -> Sbom:
    """The bill of materials of a baked *subject* (an :class:`Artifact`) or a lowered recipe.

    *manifest* is mkosi's JSON package manifest (default: :func:`manifest_path`);
    when it does not exist the document says so and lists the packages the recipe
    declares, from *lock* (an artifact) or the lowered recipe. *lock* supplies the
    source pins (each build's commit or sha256, the kernel's, the ``efi-stub``'s)
    and, for an artifact, the base, arch, snapshot and mirror it was locked with.
    A lowered recipe must have one active variant (``lower(recipe, variants=[...])``).

    An artifact is checked first (:func:`verify_artifact`). Its document is bound to
    the bake: *lock* must hash to the lockfile sha256 ``bake-result.json`` records
    and the manifest to the one recorded beside the artifact, else ``ArtifactError``
    (``E_ARTIFACT_CHANGED``). *allow_unbound* accepts them instead, with a note,
    and marks the document incomplete (``bound`` is false). Paths in the document
    are relative to the bake output directory, so moving it changes nothing.

    *binaries* is a directory holding the built binaries, at their image paths (an
    unpacked root) or by file name (a build cache): each source build's installed
    binary that records its dependencies (Go build information, ``cargo auditable``'s
    ``.dep-v0``) adds them as ``go-module`` or ``crate`` components, a note naming
    any binary not found or recording nothing. A binary is only taken when its sha256
    is the one the bake recorded for its image path (``Artifact.installed``, read from
    the UKI's initrd); otherwise (another binary, or nothing recorded, as for a lowered
    recipe) it goes to :attr:`Sbom.unbound_binaries` with its path, sha256 and
    dependencies, and the document is unbound (incomplete) with a note.
    """
    if isinstance(subject, Artifact):
        document = _of_artifact(subject, lock, manifest, allow_unbound=allow_unbound)
    else:
        document = _of_lowered(subject, lock, manifest)
    if binaries is None:
        return document
    notes = list(document.notes)
    recorded = subject.installed if isinstance(subject, Artifact) else None
    found, unbound = _embedded(document.sources, Path(binaries), notes, recorded or {})
    notes.extend(
        f"unbound, the document is incomplete: binaries: {binary.describe()}; its "
        f"{len(binary.dependencies)} dependencies are listed as unbound, not as components"
        for binary in unbound
    )
    return replace(
        document,
        components=_sorted((*document.components, *found)),
        notes=tuple(notes),
        bound=document.bound and not unbound,
        unbound_binaries=tuple(unbound),
    )


def _embedded(
    sources: Iterable[Component], root: Path, notes: list[str], recorded: Mapping[str, str]
) -> tuple[list[Component], list[UnboundBinary]]:
    """The dependencies the binaries of the ``build`` *sources* record, read under *root*:
    those of a binary whose sha256 is the one the bake *recorded* for its image path, and
    the binaries whose sha256 is not (or was not recorded), with theirs."""
    found: dict[tuple[str, str, str, str], Component] = {}
    unbound: list[UnboundBinary] = []
    for source in sources:
        if source.kind != "build":
            continue
        for dest in source.installs:
            path = next(
                (p for p in (root / dest.lstrip("/"), root / Path(dest).name) if p.is_file()),
                None,
            )
            if path is None:
                notes.append(f"binaries: {source.name}'s {dest} is not under {root}")
                continue
            data = path.read_bytes()
            deps = embedded_dependencies(data)
            if deps is None:
                notes.append(
                    f"binaries: {source.name}'s {dest} records no dependencies "
                    "(not Go 1.18+ build information or cargo auditable)"
                )
                continue
            components: dict[tuple[str, str, str, str], Component] = {
                (dep.kind, dep.name, dep.version, source.name): Component(
                    dep.kind,
                    dep.name,
                    dep.version,
                    origin=dep.source if dep.kind == "crate" else "",
                    parent=source.name,
                )
                for dep in deps
                if not dep.main
            }
            sha = _sha256(data)
            if recorded.get(dest) == sha:
                found.update(components)
                continue
            unbound.append(
                UnboundBinary(
                    source.name,
                    dest,
                    str(path),
                    sha,
                    recorded.get(dest),
                    tuple(components.values()),
                )
            )
    return list(found.values()), unbound


def manifest_path(artifact: Artifact) -> Path:
    """Where mkosi wrote *artifact*'s package manifest: ``<variant>.manifest`` beside it.

    ``<variant>.manifest.json`` and ``<artifact stem>.manifest`` are taken too when
    they exist; the first candidate is returned when none does.
    """
    return package_manifest(artifact.path, artifact.variant)


def read_manifest(path: Path) -> tuple[dict[str, str], tuple[Component, ...]]:
    """mkosi's manifest at *path*: its ``config`` (name, distribution, release, ...) and packages.

    The shape is mkosi's ``Manifest.as_dict()`` (``mkosi/manifest.py``):
    ``{"manifest_version": 1, "config": {...}, "packages": [{"type", "name",
    "version", "architecture"}], "extension": {...}}``.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError(
            f"Unreadable mkosi manifest {path}: {exc}.",
            hint="Bake the variant again to rewrite it.",
            context={"path": str(path)},
        ) from exc
    return parse_manifest(payload, path)


def parse_manifest(
    payload: object, path: Path | str = "the manifest"
) -> tuple[dict[str, str], tuple[Component, ...]]:
    """:func:`read_manifest` of an already parsed *payload*; *path* names it in errors."""
    context = {"path": str(path)}
    if not isinstance(payload, dict) or payload.get("manifest_version") != MANIFEST_VERSION:
        version = payload.get("manifest_version") if isinstance(payload, dict) else None
        raise ArtifactError(
            f"Unsupported mkosi manifest version {version!r} in {path}.",
            hint=f"tundravm reads manifest_version {MANIFEST_VERSION} (mkosi 26 writes it).",
            context=context,
        )
    config = payload.get("config") or {}
    found: list[Component] = []
    for entry in payload.get("packages") or ():
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        found.append(
            Component(
                kind="package",
                name=str(entry["name"]),
                version=str(entry.get("version") or ""),
                arch=str(entry.get("architecture") or ""),
                type=str(entry.get("type") or "deb"),
                origin=str(entry.get("origin") or entry.get("repository") or ""),
            )
        )
    return {str(k): str(v) for k, v in config.items()}, tuple(found)


def _of_artifact(
    artifact: Artifact, lock: Lock | None, manifest: Path | None, *, allow_unbound: bool
) -> Sbom:
    verify_artifact(artifact)
    path = manifest_path(artifact) if manifest is None else Path(manifest)
    unbound = _unbound(artifact, lock, path)
    if unbound and not allow_unbound:
        raise ArtifactError(
            f"The SBOM of {artifact.variant}/{artifact.target} would not describe the bake: "
            + "; ".join(unbound)
            + ".",
            hint=(
                "Pass the lockfile the bake recorded (--lockfile) and keep mkosi's manifest "
                "beside the artifact, or bake again. --allow-unbound (allow_unbound=True) "
                "writes the document anyway, marked incomplete."
            ),
            context={"variant": artifact.variant, "target": artifact.target},
        )
    payload = lock.lockfile.recipe if lock is not None else {}
    profiles = payload.get("profiles") or {}
    profile: Mapping[str, Any] = profiles.get(artifact.variant) or {}
    distribution: Mapping[str, Any] = payload.get("distribution") or payload
    declared: Iterable[str] = ()
    if lock is not None:
        declared = lock.lockfile.dependencies.get(artifact.variant) or profile.get("packages", ())
    notes = [f"unbound, the document is incomplete: {problem}" for problem in unbound]
    if artifact.simulated:
        notes.append("the artifact is simulated (in-process backend): nothing was installed")
    shown = _relative(path, artifact.build)
    missing = ("manifest",) if artifact.manifest_sha256 is not None and not path.is_file() else ()
    if missing:
        notes.append(f"the manifest the bake recorded at {shown} is gone: the document is unbound")
    packages, config, found = _packages(path, declared, distribution.get("arch"), notes, shown)
    if lock is None:
        notes.append("no lockfile: source pins are not listed")
    base = str(distribution.get("base") or _base_of(config))
    arch = str(distribution.get("arch") or _FROM_MKOSI_ARCH.get(config.get("architecture", ""), ""))
    sources = () if lock is None else _locked_sources(lock, artifact.variant, profile)
    return Sbom(
        variant=artifact.variant,
        components=_sorted(packages + sources),
        base=base,
        arch=arch,
        snapshot=_opt(distribution.get("snapshot")),
        mirror=_opt(distribution.get("mirror")),
        recipe_digest=artifact.recipe_digest,
        tree_digest=artifact.tree_digest,
        artifact=_relative(artifact.path, artifact.build),
        artifact_sha256=artifact.sha256,
        tundravm_version=_version(),
        manifest=shown,
        manifest_found=found,
        bound=not unbound and not missing,
        missing=missing,
        created=_created(),
        notes=tuple(notes),
    )


def _relative(path: Path, build: Path | None) -> Path:
    """*path* relative to the build directory *build*, when it is under it."""
    if build is None:
        return path
    try:
        return path.resolve().relative_to(build.resolve())
    except ValueError:
        return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unbound(artifact: Artifact, lock: Lock | None, manifest: Path) -> list[str]:
    """Why *lock* and *manifest* are not what the bake of *artifact* recorded; empty if they are."""
    problems: list[str] = []
    if lock is not None:
        actual = _sha256(lock.text().encode())
        if artifact.lock_sha256 is None:
            problems.append(f"the bake read no lockfile, but one is given (sha256 {actual[:12]})")
        elif actual != artifact.lock_sha256:
            problems.append(
                f"the lockfile hashes to {actual[:12]}, the bake recorded "
                f"{artifact.lock_sha256[:12]}"
            )
    if manifest.is_file():
        actual = _sha256(manifest.read_bytes())
        shown = _relative(manifest, artifact.build)
        if artifact.manifest_sha256 is None:
            problems.append(f"the bake recorded no manifest, but {shown} exists")
        elif actual != artifact.manifest_sha256:
            problems.append(
                f"{shown} hashes to {actual[:12]}, the bake recorded "
                f"{artifact.manifest_sha256[:12]}"
            )
    return problems


def _of_lowered(img: Lowered, lock: Lock | None, manifest: Path | None) -> Sbom:
    variant = img.profile()
    path = (
        img.build_dir / variant / "output" / f"{variant}.manifest"
        if manifest is None
        else Path(manifest)
    )
    notes: list[str] = []
    declared = sorted(img.state.effective_profile(variant).packages)
    packages, _, found = _packages(path, declared, img.arch, notes, path)
    pins = {} if lock is None else {f.name: f for f in lock.lockfile.fetches if f.name}
    if lock is None:
        notes.append("no lockfile: sources are listed with their refs, unpinned")
    specs: list[tuple[ComponentKind, NamedSource]] = [
        ("build", build) for build in img.source_builds(profile=variant).values()
    ]
    kernel = img.kernel_source(variant)
    if kernel is not None:
        specs.append(("kernel", kernel))
    deb = img.deb_file(variant)
    if deb is not None:
        specs.append(("efi-stub", deb))
    sources = tuple(_declared_source(kind, spec, pins) for kind, spec in specs)
    return Sbom(
        variant=variant,
        components=_sorted(packages + sources),
        base=img.base,
        arch=img.arch,
        snapshot=img.snapshot,
        mirror=img.mirror,
        recipe_digest=img.select([variant]).digest(),
        tundravm_version=_version(),
        manifest=path,
        manifest_found=found,
        created=_created(),
        notes=tuple(notes),
    )


def _packages(
    path: Path, declared: Iterable[str], arch: object, notes: list[str], shown: Path
) -> tuple[tuple[Component, ...], dict[str, str], bool]:
    """The manifest's packages, or the *declared* ones (with a note) when it is missing."""
    if path.is_file():
        config, packages = read_manifest(path)
        return packages, config, True
    notes.append(
        f"no mkosi package manifest at {shown}: listing the packages the recipe declares, "
        "without versions"
    )
    deb_arch = _DEB_ARCH.get(str(arch or ""), "")
    packages = tuple(
        Component(kind="package", name=name, arch=deb_arch, type="deb", declared=True)
        for name in sorted(set(declared))
    )
    return packages, {}, False


def _declared_source(
    kind: ComponentKind, spec: NamedSource, pins: Mapping[str, LockedFetch]
) -> Component:
    source = spec.source
    pin = spec.pin_from(pins) or ""
    installs = tuple(step.dest for step in getattr(spec, "install", ()))
    if isinstance(source, GitSource):
        return Component(
            kind=kind,
            name=spec.key,
            version=pin,
            url=source.url,
            ref=source.ref,
            installs=installs,
        )
    return Component(
        kind=kind, name=spec.key, version=pin, url=source.url, sha256=pin, installs=installs
    )


def _locked_sources(lock: Lock, variant: str, profile: Mapping[str, Any]) -> tuple[Component, ...]:
    """The lock's pins that *variant* builds from, read from the lock's recipe payload."""
    pins = {f.name: f for f in lock.lockfile.fetches if f.name}
    found: list[Component] = []
    builds: Mapping[str, Any] = profile.get("source_builds") or {}
    extends = profile.get("extends")
    for name, spec in builds.items():
        keys = (f"{variant}/{name}", f"{extends}/{name}" if extends else "", name)
        fetch = next((pins[key] for key in keys if key in pins), None)
        installs = tuple(str(step.get("dest", "")) for step in spec.get("install") or ())
        if fetch is not None:
            found.append(_pinned("build", fetch, installs))
    if not profile or profile.get("kernel", True) is not None:
        kernel = pins.get(f"kernel-{variant}") or pins.get("kernel")
        if kernel is not None:
            found.append(_pinned("kernel", kernel, ()))
    stub = pins.get(f"efi-stub-{variant}") or pins.get("efi-stub")
    if stub is not None:
        found.append(_pinned("efi-stub", stub, ()))
    if not profile:
        known = {c.name for c in found}
        found.extend(
            _pinned("build", fetch, ())
            for name, fetch in sorted(pins.items())
            if name not in known and "/" not in name and not name.startswith(("kernel", "efi-stub"))
        )
    return tuple(found)


def _pinned(kind: ComponentKind, fetch: LockedFetch, installs: tuple[str, ...]) -> Component:
    if fetch.kind == "git":
        return Component(
            kind=kind,
            name=fetch.name or fetch.source,
            version=fetch.digest,
            url=fetch.source,
            ref=fetch.ref or "",
            installs=installs,
        )
    return Component(
        kind=kind,
        name=fetch.name or fetch.source,
        version=fetch.digest,
        url=fetch.source,
        sha256=fetch.digest,
        installs=installs,
    )


def _sorted(components: Iterable[Component]) -> tuple[Component, ...]:
    return tuple(sorted(components, key=_key))


def _key(component: Component) -> tuple[int, str, str, str, str]:
    return (
        _KIND_ORDER[component.kind],
        component.name,
        component.arch,
        component.version,
        component.url,
    )


def _base_of(config: Mapping[str, str]) -> str:
    distribution = config.get("distribution", "")
    release = config.get("release", "")
    return f"{distribution}/{release}" if distribution and release else distribution


def _opt(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _version() -> str:
    from tundravm import __version__

    return __version__


def _created() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
    moment = (
        datetime.fromtimestamp(int(epoch), UTC) if epoch.isdigit() else datetime.now(UTC)
    ).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def spdx_packages(document: Mapping[str, Any]) -> dict[str, str]:
    """Name to version (empty when unknown) of each package an SPDX *document*'s image
    ``CONTAINS``, as :meth:`Sbom.to_spdx` writes it."""
    contained = {
        r.get("relatedSpdxElement")
        for r in document.get("relationships") or []
        if isinstance(r, dict) and r.get("relationshipType") == "CONTAINS"
    }
    found: dict[str, str] = {}
    for package in document.get("packages") or []:
        if isinstance(package, dict) and package.get("SPDXID") in contained:
            version = str(package.get("versionInfo") or "")
            found[str(package.get("name"))] = "" if version == "NOASSERTION" else version
    return found


# ── PURLs and serializers ────────────────────────────────────────────────


def purl(component: Component, *, namespace: str = "debian", distro: str = "") -> str:
    """The package URL of *component*.

    A package: ``pkg:deb/<namespace>/<name>@<version>?arch=<arch>&distro=<distro>``
    (``rpm``/``alpm`` for those manifests). A git source:
    ``pkg:generic/<name>@<commit>?vcs_url=git+<url>@<ref>``; an http one:
    ``pkg:generic/<name>@<sha256>?checksum=sha256:<sha256>&download_url=<url>``.
    An embedded Go module: ``pkg:golang/<module path>@<version>``; a crate:
    ``pkg:cargo/<name>@<version>``. Parts are percent-encoded as the PURL spec's
    canonical form does.
    """
    if component.kind == "go-module":
        path = "/".join(_quote(part) for part in component.name.split("/"))
        return f"pkg:golang/{path}@{_quote(component.version)}"
    if component.kind == "crate":
        return f"pkg:cargo/{_quote(component.name)}@{_quote(component.version)}"
    name = _quote(component.name.replace("/", "-"))
    version = f"@{_quote(component.version)}" if component.version else ""
    qualifiers: dict[str, str] = {}
    if component.kind == "package":
        kind = _PURL_TYPES.get(component.type or "deb", "generic")
        qualifiers = {"arch": component.arch, "distro": distro}
        prefix = f"pkg:{kind}/{_quote(namespace)}/" if namespace else f"pkg:{kind}/"
        return prefix + name + version + _qualifiers(qualifiers)
    if component.git:
        ref = f"@{component.ref}" if component.ref else ""
        qualifiers = {"vcs_url": f"git+{component.url}{ref}"}
    else:
        qualifiers = {"download_url": component.url}
        if component.sha256:
            qualifiers["checksum"] = f"sha256:{component.sha256}"
    return f"pkg:generic/{name}{version}{_qualifiers(qualifiers)}"


def _quote(text: str) -> str:
    return quote(text, safe=":")


def _qualifiers(values: Mapping[str, str]) -> str:
    pairs = [f"{key}={quote(value, safe=':/')}" for key, value in sorted(values.items()) if value]
    return "?" + "&".join(pairs) if pairs else ""


def _spdx_safe(text: str) -> str:
    return _SPDX_ID.sub("-", text).strip("-") or "x"


def _spdx_id(component: Component) -> str:
    digest = hashlib.sha256(json.dumps(_key(component)).encode()).hexdigest()[:8]
    prefix = "Package" if component.kind == "package" else "Source"
    return f"SPDXRef-{prefix}-{_spdx_safe(component.name)}-{digest}"


def _relation(element: str, kind: str, related: str) -> dict[str, str]:
    return {"spdxElementId": element, "relationshipType": kind, "relatedSpdxElement": related}


def _download(component: Component) -> str:
    if component.kind == "package" or not component.url:
        return "NOASSERTION"
    if component.git:
        return f"git+{component.url}@{component.version or component.ref}"
    return component.url


def _spdx_package(component: Component, ref: str, distro: tuple[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "SPDXID": ref,
        "name": component.name,
        "versionInfo": component.version or "NOASSERTION",
        "downloadLocation": _download(component),
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": purl(component, namespace=distro[0], distro=distro[1]),
            }
        ],
    }
    if component.sha256:
        entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": component.sha256}]
    if component.kind in EMBEDDED_KINDS:
        entry["primaryPackagePurpose"] = "LIBRARY"
        entry["sourceInfo"] = f"{component.kind} recorded in the binary of {component.parent}"
    elif component.kind != "package":
        entry["primaryPackagePurpose"] = "SOURCE" if component.git else "ARCHIVE"
        if component.ref:
            entry["sourceInfo"] = f"{component.kind} from git ref {component.ref}"
        else:
            entry["sourceInfo"] = f"{component.kind} downloaded over http"
    comments = []
    if component.installs:
        comments.append("installs " + ", ".join(component.installs))
    if component.origin:
        comments.append(f"from repository {component.origin}")
    if component.declared:
        comments.append("declared by the recipe; version unknown (no mkosi manifest)")
    if component.kind != "package" and not component.version:
        comments.append("not pinned by a lockfile")
    if comments:
        entry["comment"] = "; ".join(comments)
    return entry


def _cyclonedx_component(component: Component, distro: tuple[str, str]) -> dict[str, Any]:
    library = component.kind in ("package", "efi-stub") or component.kind in EMBEDDED_KINDS
    entry: dict[str, Any] = {
        "type": "library" if library else "application",
        "bom-ref": _spdx_id(component).removeprefix("SPDXRef-"),
        "name": component.name,
    }
    if component.version:
        entry["version"] = component.version
    entry["purl"] = purl(component, namespace=distro[0], distro=distro[1])
    if component.sha256:
        entry["hashes"] = [{"alg": "SHA-256", "content": component.sha256}]
    if component.url:
        kind = "vcs" if component.git else "distribution"
        entry["externalReferences"] = [{"type": kind, "url": component.url}]
    properties = [{"name": "tundravm:kind", "value": component.kind}]
    if component.ref:
        properties.append({"name": "tundravm:ref", "value": component.ref})
    if component.origin:
        properties.append({"name": "tundravm:origin", "value": component.origin})
    properties.extend({"name": "tundravm:install", "value": dest} for dest in component.installs)
    if component.declared:
        properties.append({"name": "tundravm:declared", "value": "true"})
    if component.parent:
        properties.append({"name": "tundravm:parent", "value": component.parent})
    entry["properties"] = properties
    return entry


def _manifest_cell(document: Sbom) -> str:
    if document.manifest is None:
        return ""
    state = "" if document.manifest_found else " (missing)"
    return f"{document.manifest}{state}"


def _package_rows(document: Sbom) -> list[tuple[str, ...]]:
    return [
        (c.name, c.version or "-", c.arch or "-", c.origin or ("declared" if c.declared else "-"))
        for c in document.packages
    ]


def _source_rows(document: Sbom) -> list[tuple[str, ...]]:
    return [
        (
            c.name,
            c.kind,
            c.version or "unpinned",
            c.ref or "-",
            c.url or "-",
            ", ".join(c.installs) or "-",
        )
        for c in document.sources
    ]


def _embedded_rows(document: Sbom) -> list[tuple[str, ...]]:
    return [(c.name, c.kind, c.version, c.parent, c.origin or "-") for c in document.embedded]


_EMBEDDED_HEADER = ("NAME", "KIND", "VERSION", "BUILD", "ORIGIN")
_UNBOUND_HEADER = ("BUILD", "INSTALLS", "READ FROM", "SHA256", "RECORDED")
_UNBOUND_DEPS_HEADER = ("NAME", "KIND", "VERSION", "BUILD", "BINARY", "ORIGIN")


def _unbound_comment(binary: UnboundBinary) -> str:
    """An unbound binary for an SPDX or CycloneDX annotation: provenance, then its purls."""
    purls = ", ".join(purl(c) for c in binary.dependencies) or "none"
    return (
        f"unbound binary: {binary.describe()}; the dependencies it records are not verified "
        f"against the image and are not components: {purls}"
    )


def _unbound_rows(document: Sbom) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """Rows of the unbound binaries and of the dependencies each records."""
    binaries: list[tuple[str, ...]] = [
        (b.build, b.dest, b.path, b.sha256, b.recorded or "-") for b in document.unbound_binaries
    ]
    deps: list[tuple[str, ...]] = [
        (c.name, c.kind, c.version, b.build, b.dest, c.origin or "-")
        for b in document.unbound_binaries
        for c in b.dependencies
    ]
    return binaries, deps


_PACKAGE_HEADER = ("NAME", "VERSION", "ARCH", "ORIGIN")
_SOURCE_HEADER = ("NAME", "KIND", "PIN", "REF", "URL", "INSTALLS")


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    return [
        "  "
        + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in (header, *rows)
    ]


def _text(document: Sbom) -> str:
    lines = [f"sbom {document.variant}"]
    width = max(len(key) for key, _ in document.metadata())
    lines.extend(f"  {key:<{width}}  {value}" for key, value in document.metadata())
    lines.append("")
    lines.append(f"packages ({len(document.packages)})")
    if document.packages:
        lines.extend(_table(_PACKAGE_HEADER, _package_rows(document)))
    lines.append("")
    lines.append(f"sources ({len(document.sources)})")
    if document.sources:
        lines.extend(_table(_SOURCE_HEADER, _source_rows(document)))
    if document.embedded:
        lines.extend(("", f"embedded ({len(document.embedded)})"))
        lines.extend(_table(_EMBEDDED_HEADER, _embedded_rows(document)))
    if document.unbound_binaries:
        binaries, deps = _unbound_rows(document)
        lines.extend(("", f"unbound binaries ({len(binaries)})"))
        lines.extend(_table(_UNBOUND_HEADER, binaries))
        lines.extend(("", f"unbound dependencies ({len(deps)})"))
        lines.extend(_table(_UNBOUND_DEPS_HEADER, deps))
    lines.extend(f"note: {note}" for note in document.notes)
    return "\n".join(lines) + "\n"


def _md_row(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _md_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    head = tuple(cell.lower() for cell in header)
    return [_md_row(head), _md_row("---" for _ in head), *(_md_row(row) for row in rows)]


def _markdown(document: Sbom) -> str:
    lines = [f"## SBOM: {document.variant}", ""]
    lines.extend(_md_table(("KEY", "VALUE"), [tuple(row) for row in document.metadata()]))
    lines.extend(["", f"### Packages ({len(document.packages)})", ""])
    if document.packages:
        lines.extend(_md_table(_PACKAGE_HEADER, _package_rows(document)))
    lines.extend(["", f"### Sources ({len(document.sources)})", ""])
    if document.sources:
        lines.extend(_md_table(_SOURCE_HEADER, _source_rows(document)))
    if document.embedded:
        lines.extend(["", f"### Embedded dependencies ({len(document.embedded)})", ""])
        lines.extend(_md_table(_EMBEDDED_HEADER, _embedded_rows(document)))
    if document.unbound_binaries:
        binaries, deps = _unbound_rows(document)
        lines.extend(["", f"### Unbound binaries ({len(binaries)})", ""])
        lines.extend(_md_table(_UNBOUND_HEADER, binaries))
        lines.extend(["", f"### Unbound dependencies ({len(deps)})", ""])
        lines.extend(_md_table(_UNBOUND_DEPS_HEADER, deps))
    if document.notes:
        lines.append("")
        lines.extend(f"> note: {note}" for note in document.notes)
    return "\n".join(lines) + "\n"
