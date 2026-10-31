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

from tundravm.backends.base import BOOT_PAYLOAD_NAME
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import (
    Backend,
    Package,
    Recipe,
    bake,
    lock,
    sbom,
)
from tundravm.models import BakeRequest, BakeResult

from .test_initrd_digests import newc
from .test_measure import build_pe
from .test_sbom import _recipe, _resolve


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


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── binding: only binaries whose sha256 the bake recorded add components ──


def bootable() -> Recipe:
    """The sbom recipe with a kernel, so the bake lints clean."""
    recipe = _recipe()
    return replace(recipe, items=(*recipe.items, Package("linux-image-amd64")))


def test_the_bake_records_installed_digests_from_the_ukis_initrd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = cargo_binary(CRATES)
    initrd = newc([("usr/bin/app", binary, 0o100755), ("etc/app.conf", b"conf", 0o100644)])
    original = LocalLinuxBackend.execute

    def execute(self: LocalLinuxBackend, request: BakeRequest) -> BakeResult:
        result = original(self, request)
        uki = request.build_dir / request.profile / "output" / BOOT_PAYLOAD_NAME
        uki.write_bytes(build_pe([(".text", b"stub"), (".initrd", gzip.compress(initrd))]))
        return result

    monkeypatch.setattr(LocalLinuxBackend, "execute", execute)
    recipe = bootable()
    locked = lock(recipe, resolver=_resolve)
    (artifact,) = bake(recipe, lock=locked, backend=Backend("local"), build=tmp_path / "build")
    assert artifact.installed == {"/etc/app.conf": sha(b"conf"), "/usr/bin/app": sha(binary)}
    (tmp_path / "bins").mkdir()
    (tmp_path / "bins" / "app").write_bytes(binary)
    document = sbom(artifact, lock=locked, binaries=tmp_path / "bins")
    assert [c.name for c in document.embedded] == ["itoa"] and not document.unbound_binaries
