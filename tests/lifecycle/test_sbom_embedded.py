"""``sbom(binaries=...)``: Go build information and ``cargo auditable`` crates in built binaries."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from tundravm import sbom
from tundravm.declarative import lower
from tundravm.declarative._embedded import cargo_crates, embedded_dependencies, go_modules
from tundravm.declarative.bom import purl

from .test_sbom import _recipe

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


def test_sbom_nests_each_binarys_dependencies_under_its_build(tmp_path: Path) -> None:
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "bin" / "app").write_bytes(cargo_binary(CRATES))
    img = lower(_recipe(), variants=["default"])
    document = sbom(img, manifest=tmp_path / "absent.manifest", binaries=tmp_path)
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
    (tmp_path / "app").write_bytes(go_binary())
    img = lower(_recipe(), variants=["default"])
    document = sbom(img, manifest=tmp_path / "absent.manifest", binaries=tmp_path)
    assert [purl(c) for c in document.embedded] == [
        "pkg:golang/example.com/new@v1.1.0",
        "pkg:golang/golang.org/x/sys@v0.30.0",
    ]
