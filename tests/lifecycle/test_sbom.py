"""``sbom``: mkosi's package manifest, the lockfile's pins and recipe metadata in one document."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tundravm import Artifact, Sbom, sbom
from tundravm._source import Source
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Install,
    Lock,
    Package,
    Recipe,
    Variant,
    lock,
    lower,
)
from tundravm.declarative.bom import SBOM_FORMATS, manifest_path, purl, read_manifest
from tundravm.declarative.utils import EfiStub
from tundravm.errors import ArtifactError, ValidationError

REPO = "https://example.com/app.git"
COMMIT = "c" * 40
DEB_SHA = "d" * 64
SNAPSHOT = "20251113T083151Z"
EFI_VERSION = "257.8-1~deb13u1"

# The exact shape mkosi 26 writes for ManifestFormat=json: Manifest.as_dict() in the
# installed mkosi/manifest.py (manifest_version, config, packages[type, name, version,
# architecture], extension); these entries are copied from a real trixie bake's
# build/default/output/default.manifest.
PACKAGES: list[dict[str, str]] = [
    {"type": "deb", "name": "zlib1g", "version": "1:1.3.dfsg+really1.3.1-1+b1",
     "architecture": "amd64"},
    {"type": "deb", "name": "base-files", "version": "13.8+deb13u7", "architecture": "amd64"},
    {"type": "deb", "name": "bsdutils", "version": "1:2.41.5-0+deb13u1", "architecture": "amd64"},
    {"type": "deb", "name": "ca-certificates", "version": "20250419", "architecture": "all"},
    {"type": "deb", "name": "systemd", "version": "257.13-1~deb13u1", "architecture": "amd64"},
]  # fmt: skip
MANIFEST: dict[str, object] = {
    "manifest_version": 1,
    "config": {
        "name": "default",
        "distribution": "debian",
        "architecture": "x86-64",
        "output_format": "uki",
        "release": "trixie",
    },
    "packages": PACKAGES,
    "extension": {},
}


def _recipe() -> Recipe:
    return Recipe(
        "node",
        Fragment(
            "common",
            items=(
                Package("systemd"),
                Package("ca-certificates"),
                Build(
                    "app",
                    Git(REPO, "main"),
                    script="make",
                    install=(Install("app", "/usr/bin/app"), Install("app.conf", "/etc/app.conf")),
                ),
                EfiStub(snapshot=SNAPSHOT, version=EFI_VERSION),
            ),
        ),
        snapshot=SNAPSHOT,
        variants=(Variant("default", target="qemu"),),
    )


def _resolve(source: Source) -> str:
    return COMMIT if source.kind == "git" else DEB_SHA


@pytest.fixture
def locked() -> Lock:
    return lock(_recipe(), resolver=_resolve)


def _artifact(tmp_path: Path, *, manifest: bool = True) -> Artifact:
    output = tmp_path / "default" / "output"
    output.mkdir(parents=True)
    path = output / "default.efi"
    path.write_bytes(b"uki")
    if manifest:
        (output / "default.manifest").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    return Artifact(
        path=path,
        variant="default",
        target="qemu",
        sha256="a" * 64,
        recipe_digest="r" * 64,
        lock_digest="l" * 64,
        tree_digest="t" * 64,
    )


# ── the manifest parser ──────────────────────────────────────────────────


def test_read_manifest_parses_mkosi_26_shape(tmp_path: Path) -> None:
    path = tmp_path / "default.manifest"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    config, packages = read_manifest(path)
    assert config["distribution"] == "debian" and config["release"] == "trixie"
    assert [(p.name, p.version, p.arch, p.type) for p in packages][:2] == [
        ("zlib1g", "1:1.3.dfsg+really1.3.1-1+b1", "amd64", "deb"),
        ("base-files", "13.8+deb13u7", "amd64", "deb"),
    ]
    assert all(p.kind == "package" and not p.declared and not p.origin for p in packages)


def test_read_manifest_keeps_an_origin_repository_when_present(tmp_path: Path) -> None:
    payload = {**MANIFEST, "packages": [{**PACKAGES[0], "repository": "backports"}]}
    path = tmp_path / "default.manifest"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (package,) = read_manifest(path)[1]
    assert package.origin == "backports"


@pytest.mark.parametrize("text", ["{not json", json.dumps({"manifest_version": 2})])
def test_read_manifest_rejects_unreadable_or_newer_manifests(tmp_path: Path, text: str) -> None:
    path = tmp_path / "default.manifest"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ArtifactError) as raised:
        read_manifest(path)
    assert raised.value.hint


def test_manifest_path_is_the_variant_manifest_beside_the_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, manifest=False)
    assert manifest_path(artifact) == artifact.path.parent / "default.manifest"
    json_name = artifact.path.parent / "default.manifest.json"
    json_name.write_text("{}", encoding="utf-8")
    assert manifest_path(artifact) == json_name


# ── merging ──────────────────────────────────────────────────────────────


def test_artifact_sbom_merges_manifest_lock_and_metadata(tmp_path: Path, locked: Lock) -> None:
    document = sbom(_artifact(tmp_path), lock=locked)
    assert isinstance(document, Sbom)
    assert document.manifest_found and document.notes == ()
    assert [p.name for p in document.packages] == sorted(p["name"] for p in PACKAGES)
    assert (document.base, document.arch, document.snapshot) == (
        "debian/trixie",
        "x86_64",
        SNAPSHOT,
    )
    assert (document.variant, document.artifact_sha256) == ("default", "a" * 64)
    assert (document.recipe_digest, document.tree_digest) == ("r" * 64, "t" * 64)
    build, stub = document.sources
    assert (build.kind, build.name, build.commit, build.ref, build.url) == (
        "build",
        "app",
        COMMIT,
        "main",
        REPO,
    )
    assert build.installs == ("/usr/bin/app", "/etc/app.conf")
    assert (stub.kind, stub.name, stub.sha256) == ("efi-stub", "efi-stub", DEB_SHA)
    assert stub.url.endswith(f"systemd-boot-efi_{EFI_VERSION}_amd64.deb")


def test_missing_manifest_lists_declared_packages_and_says_so(tmp_path: Path, locked: Lock) -> None:
    document = sbom(_artifact(tmp_path, manifest=False), lock=locked)
    assert not document.manifest_found
    assert document.manifest == tmp_path / "default" / "output" / "default.manifest"
    assert any("no mkosi package manifest" in note for note in document.notes)
    names = {p.name for p in document.packages}
    assert {"systemd", "ca-certificates"} <= names
    assert all(p.declared and not p.version and p.arch == "amd64" for p in document.packages)
    assert len(document.sources) == 2


def test_artifact_without_lock_lists_no_sources(tmp_path: Path) -> None:
    document = sbom(_artifact(tmp_path))
    assert document.sources == ()
    assert document.base == "debian/trixie" and document.arch == "x86_64"
    assert any("no lockfile" in note for note in document.notes)


def test_lowered_sbom_lists_declared_sources_pinned_by_the_lock(
    tmp_path: Path, locked: Lock
) -> None:
    img = lower(_recipe(), variants=["default"])
    pinned = sbom(img, lock=locked, manifest=tmp_path / "absent.manifest")
    assert [(c.kind, c.version) for c in pinned.sources] == [
        ("build", COMMIT),
        ("efi-stub", DEB_SHA),
    ]
    assert pinned.recipe_digest == img.digest() and pinned.snapshot == SNAPSHOT
    unpinned = sbom(img, manifest=tmp_path / "absent.manifest")
    assert [c.version for c in unpinned.sources] == ["", ""]
    assert unpinned.sources[0].ref == "main"


# ── serializers ──────────────────────────────────────────────────────────


def test_purls_follow_the_package_url_spec(tmp_path: Path, locked: Lock) -> None:
    document = sbom(_artifact(tmp_path), lock=locked)
    by_name = {c.name: c for c in document.components}
    assert purl(by_name["bsdutils"], distro="debian-trixie") == (
        "pkg:deb/debian/bsdutils@1:2.41.5-0%2Bdeb13u1?arch=amd64&distro=debian-trixie"
    )
    assert purl(by_name["app"]) == (f"pkg:generic/app@{COMMIT}?vcs_url=git%2B{REPO}%40main")
    stub = purl(by_name["efi-stub"])
    assert stub.startswith(f"pkg:generic/efi-stub@{DEB_SHA}?checksum=sha256:{DEB_SHA}")
    assert "&download_url=https://snapshot.debian.org/" in stub


def test_spdx_document_structure(tmp_path: Path, locked: Lock) -> None:
    document = sbom(_artifact(tmp_path), lock=locked).to_spdx()
    for key in ("spdxVersion", "dataLicense", "SPDXID", "name", "documentNamespace"):
        assert document[key]
    assert document["spdxVersion"] == "SPDX-2.3" and document["SPDXID"] == "SPDXRef-DOCUMENT"
    assert document["creationInfo"]["creators"] == ["Tool: tundravm-0.1.0"]
    assert document["creationInfo"]["created"].endswith("Z")
    packages = document["packages"]
    ids = [p["SPDXID"] for p in packages]
    assert len(ids) == len(set(ids)) == 1 + len(PACKAGES) + 2
    for package in packages:
        assert package["SPDXID"].startswith("SPDXRef-")
        assert {"name", "versionInfo", "downloadLocation", "filesAnalyzed"} <= set(package)
    image = packages[0]
    assert image["checksums"] == [{"algorithm": "SHA256", "checksumValue": "a" * 64}]
    app = next(p for p in packages if p["name"] == "app")
    assert app["downloadLocation"] == f"git+{REPO}@{COMMIT}"
    assert app["externalRefs"][0]["referenceType"] == "purl"
    kinds = [(r["spdxElementId"], r["relationshipType"]) for r in document["relationships"]]
    assert kinds[0] == ("SPDXRef-DOCUMENT", "DESCRIBES")
    assert kinds.count(("SPDXRef-Image", "CONTAINS")) == len(PACKAGES)
    assert kinds.count(("SPDXRef-Image", "GENERATED_FROM")) == 2
    related = {r["relatedSpdxElement"] for r in document["relationships"]}
    assert related == set(ids)


def test_cyclonedx_document_structure(tmp_path: Path, locked: Lock) -> None:
    document = sbom(_artifact(tmp_path), lock=locked).to_cyclonedx()
    assert (document["bomFormat"], document["specVersion"], document["version"]) == (
        "CycloneDX",
        "1.5",
        1,
    )
    assert document["serialNumber"].startswith("urn:uuid:")
    assert document["metadata"]["component"]["type"] == "operating-system"
    refs = [c["bom-ref"] for c in document["components"]]
    assert len(refs) == len(set(refs))
    assert all({"type", "name", "purl"} <= set(c) for c in document["components"])
    (dependency,) = document["dependencies"]
    assert dependency == {"ref": "image", "dependsOn": refs}
    app = next(c for c in document["components"] if c["name"] == "app")
    assert {"name": "tundravm:install", "value": "/usr/bin/app"} in app["properties"]


def test_documents_are_deterministic(
    tmp_path: Path, locked: Lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    artifact = _artifact(tmp_path)
    first, second = sbom(artifact, lock=locked), sbom(artifact, lock=locked)
    for format in SBOM_FORMATS:
        assert first.render(format) == second.render(format)
    assert first.to_spdx()["creationInfo"]["created"] == "1970-01-01T00:00:00Z"


def test_text_and_markdown_tables(tmp_path: Path, locked: Lock) -> None:
    document = sbom(_artifact(tmp_path), lock=locked)
    text = document.render("text")
    assert text.startswith("sbom default\n")
    assert "packages (5)" in text and "sources (2)" in text
    assert "  bsdutils         1:2.41.5-0+deb13u1" in text
    markdown = document.render("markdown")
    assert "| name | version | arch | origin |" in markdown
    assert f"| app | build | {COMMIT} | main | {REPO} | /usr/bin/app, /etc/app.conf |" in markdown
    with pytest.raises(ValidationError):
        document.render("xml")  # type: ignore[arg-type]
