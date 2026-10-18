"""``sbom(binaries=...)``: Go build information and ``cargo auditable`` crates in built binaries."""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm import Artifact, Backend, bake, sbom
from tundravm.backends.inprocess import UKI_FILENAME, InProcessBackend
from tundravm.declarative import Lock, Package, Recipe, lock, lower, read_artifacts
from tundravm.declarative._embedded import cargo_crates, embedded_dependencies, go_modules
from tundravm.declarative.bom import Sbom, UnboundBinary, purl
from tundravm.models import BakeRequest, BakeResult

from .test_initrd_digests import newc
from .test_measure import build_pe
from .test_sbom import _artifact, _recipe, _resolve

SENTINEL = b"\x30\x77\xaf\x0c\x92\x74\x08\x02\x41\xe1\xc1\x07\xe6\xd6\x18\xe6"
MODINFO = (
    "path\texample.com/app/cmd/app\n"
    "mod\texample.com/app\t(devel)\t\n"
    "dep\tgolang.org/x/sys\tv0.30.0\th1:sys=\n"
    "dep\texample.com/old\tv1.0.0\th1:old=\n"
    "=>\texample.com/new\tv1.1.0\th1:new=\n"
    "build\t-trimpath=true\n"
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def go_binary() -> bytes:
    """Bytes with a Go 1.18+ inline build information blob at a 16-byte aligned offset."""
    header = b"\xff Go buildinf:" + bytes((8, 2))
    header += b"\0" * (32 - len(header))
    modinfo = SENTINEL + MODINFO.encode() + SENTINEL
    blob = header + _varint(8) + b"go1.24.4" + _varint(len(modinfo)) + modinfo
    return b"\x7fELF" + b"\0" * 28 + blob + b"\0" * 64


def cargo_binary(packages: list[dict[str, object]]) -> bytes:
    """A little-endian ELF64 file whose ``.dep-v0`` section holds *packages*."""
    dep = zlib.compress(json.dumps({"packages": packages, "format": 1}).encode())
    names = b"\0.dep-v0\0.shstrtab\0"
    dep_at = 64
    names_at = dep_at + len(dep)
    shoff = names_at + len(names)
    header = b"\x7fELF" + bytes((2, 1, 1)) + b"\0" * 9
    header += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 3, 2)

    def section(name: int, offset: int, size: int) -> bytes:
        return struct.pack("<IIQQQQIIQQ", name, 1, 0, 0, offset, size, 0, 0, 1, 0)

    sections = bytes(64) + section(1, dep_at, len(dep)) + section(9, names_at, len(names))
    return header + dep + names + sections


CRATES: list[dict[str, object]] = [
    {"name": "app", "version": "0.1.0", "source": "local", "root": True, "dependencies": [1]},
    {"name": "itoa", "version": "1.0.18", "source": "crates.io"},
    {"name": "cc", "version": "1.2.0", "source": "crates.io", "kind": "build"},
]


def test_go_build_information_lists_modules_with_replacements() -> None:
    modules = go_modules(go_binary())
    assert modules is not None
    assert [(m.name, m.version, m.main) for m in modules] == [
        ("example.com/app", "(devel)", True),
        ("golang.org/x/sys", "v0.30.0", False),
        ("example.com/new", "v1.1.0", False),
    ]
    assert go_modules(b"\x7fELF no build information") is None


def test_cargo_auditable_section_lists_runtime_crates() -> None:
    crates = cargo_crates(cargo_binary(CRATES))
    assert crates is not None
    assert [(c.name, c.source, c.main) for c in crates] == [
        ("app", "local", True),
        ("itoa", "crates.io", False),
    ]
    assert cargo_crates(go_binary()) is None
    assert embedded_dependencies(b"plain bytes") is None


def baked(tmp_path: Path, installed: dict[str, str] | None) -> tuple[Artifact, Lock]:
    """An artifact whose bake recorded *installed* (``{image path: sha256}``), and its lock."""
    locked = lock(_recipe(), resolver=_resolve)
    return replace(_artifact(tmp_path, lock=locked), installed=installed), locked


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_sbom_nests_each_binarys_dependencies_under_its_build(tmp_path: Path) -> None:
    binary = cargo_binary(CRATES)
    (tmp_path / "root" / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "root" / "usr" / "bin" / "app").write_bytes(binary)
    artifact, locked = baked(tmp_path, {"/usr/bin/app": sha(binary)})
    document = sbom(artifact, lock=locked, binaries=tmp_path / "root")
    assert document.bound and not document.unbound_binaries
    assert [(c.kind, c.name, c.parent) for c in document.embedded] == [("crate", "itoa", "app")]
    assert purl(document.embedded[0]) == "pkg:cargo/itoa@1.0.18"
    assert any("/etc/app.conf is not under" in note for note in document.notes)
    cyclonedx = document.to_cyclonedx()
    crate_ref = next(c["bom-ref"] for c in cyclonedx["components"] if c["name"] == "itoa")
    assert crate_ref not in cyclonedx["dependencies"][0]["dependsOn"]
    assert cyclonedx["dependencies"][1]["dependsOn"] == [crate_ref]
    spdx = document.to_spdx()
    assert any(r["relationshipType"] == "DEPENDS_ON" for r in spdx["relationships"])
    assert "embedded (1)" in document.render("text")


def test_go_module_purl_keeps_the_module_path(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "app").write_bytes(go_binary())
    artifact, locked = baked(tmp_path, {"/usr/bin/app": sha(go_binary())})
    document = sbom(artifact, lock=locked, binaries=tmp_path / "cache")
    assert document.bound
    assert [purl(c) for c in document.embedded] == [
        "pkg:golang/example.com/new@v1.1.0",
        "pkg:golang/golang.org/x/sys@v0.30.0",
    ]


# ── binding: only binaries whose sha256 the bake recorded add components ──


def assert_unbound(document: Sbom, recorded: str | None) -> UnboundBinary:
    """One unbound binary, its dependencies kept out of the components, the document unbound."""
    assert not document.bound and not document.embedded
    (binary,) = document.unbound_binaries
    assert (binary.build, binary.dest, binary.recorded) == ("app", "/usr/bin/app", recorded)
    assert [c.name for c in binary.dependencies] == ["itoa"]
    assert any(
        n.startswith("unbound, the document is incomplete: binaries:") for n in document.notes
    )
    assert dict(document.metadata())["bound"] == "no (incomplete)"
    text = document.render("text")
    assert "unbound binaries (1)" in text and binary.sha256 in text
    spdx = document.to_spdx()
    assert "itoa" not in {p["name"] for p in spdx["packages"]}
    assert binary.sha256 in spdx["annotations"][0]["comment"]
    assert "pkg:cargo/itoa@1.0.18" in spdx["annotations"][0]["comment"]
    cyclonedx = document.to_cyclonedx()
    assert "itoa" not in {c["name"] for c in cyclonedx["components"]}
    assert binary.path in cyclonedx["annotations"][0]["text"]
    assert "### Unbound dependencies (1)" in document.render("markdown")
    return binary


def test_a_stale_binary_is_unbound(tmp_path: Path) -> None:
    stale = cargo_binary(CRATES)
    (tmp_path / "root" / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "root" / "usr" / "bin" / "app").write_bytes(stale)
    artifact, locked = baked(tmp_path, {"/usr/bin/app": sha(b"what the image holds")})
    document = sbom(artifact, lock=locked, binaries=tmp_path / "root")
    binary = assert_unbound(document, sha(b"what the image holds"))
    assert binary.sha256 == sha(stale)
    assert binary.path == str(tmp_path / "root" / "usr" / "bin" / "app")


def test_an_unrelated_binary_found_by_file_name_is_unbound(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "app").write_bytes(cargo_binary(CRATES))
    artifact, locked = baked(tmp_path, {"/usr/bin/app": sha(go_binary())})
    document = sbom(artifact, lock=locked, binaries=tmp_path / "elsewhere")
    assert assert_unbound(document, sha(go_binary())).path == str(tmp_path / "elsewhere" / "app")


def test_nothing_recorded_leaves_every_binary_unbound(tmp_path: Path) -> None:
    (tmp_path / "root" / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "root" / "usr" / "bin" / "app").write_bytes(cargo_binary(CRATES))
    artifact, locked = baked(tmp_path, None)
    binary = assert_unbound(sbom(artifact, lock=locked, binaries=tmp_path / "root"), None)
    assert "recorded no digest" in binary.describe()
    img = lower(_recipe(), variants=["default"])
    lowered = sbom(img, manifest=tmp_path / "absent.manifest", binaries=tmp_path / "root")
    assert_unbound(lowered, None)


def test_a_binary_recording_nothing_is_only_noted(tmp_path: Path) -> None:
    (tmp_path / "app").write_bytes(b"\x7fELF plain C")
    artifact, locked = baked(tmp_path, {"/usr/bin/app": sha(b"other")})
    document = sbom(artifact, lock=locked, binaries=tmp_path)
    assert document.bound and not document.unbound_binaries
    assert any("records no dependencies" in note for note in document.notes)


def bootable() -> Recipe:
    """The sbom recipe with a kernel, so the in-process bake lints clean."""
    recipe = _recipe()
    return replace(recipe, items=(*recipe.items, Package("linux-image-amd64")))


def test_the_bake_records_installed_digests_from_the_ukis_initrd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = cargo_binary(CRATES)
    initrd = newc([("usr/bin/app", binary, 0o100755), ("etc/app.conf", b"conf", 0o100644)])
    original = InProcessBackend.execute

    def execute(self: InProcessBackend, request: BakeRequest) -> BakeResult:
        result = original(self, request)
        uki = request.build_dir / request.profile / UKI_FILENAME
        uki.write_bytes(build_pe([(".text", b"stub"), (".initrd", gzip.compress(initrd))]))
        return result

    monkeypatch.setattr(InProcessBackend, "execute", execute)
    recipe = bootable()
    locked = lock(recipe, resolver=_resolve)
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=tmp_path / "build")
    (artifact,) = read_artifacts(tmp_path / "build")
    assert artifact.installed == {"/etc/app.conf": sha(b"conf"), "/usr/bin/app": sha(binary)}
    (tmp_path / "bins").mkdir()
    (tmp_path / "bins" / "app").write_bytes(binary)
    document = sbom(artifact, lock=locked, binaries=tmp_path / "bins")
    assert [c.name for c in document.embedded] == ["itoa"] and not document.unbound_binaries


def test_a_simulated_bake_records_no_installed_digests(tmp_path: Path) -> None:
    recipe = bootable()
    locked = lock(recipe, resolver=_resolve)
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=tmp_path)
    (artifact,) = read_artifacts(tmp_path)
    assert artifact.installed is None
    payload = json.loads((tmp_path / "bake-result.json").read_text(encoding="utf-8"))
    assert "installed" not in payload["variants"]["default"]["declarative"]
