"""Dependency lists that built binaries carry, read in pure Python.

* Go (1.18 and later) writes its build information into every binary: a 16-byte
  aligned ``\\xff Go buildinf:`` header followed by the toolchain version and the
  module list as varint-prefixed strings (``debug/buildinfo``). The module list
  has ``path``, ``mod``, ``dep`` and ``=>`` (replacement) lines.
* ``cargo auditable`` writes the crate graph as zlib-compressed JSON into the
  ``.dep-v0`` ELF section (``rust-audit-info`` format: ``packages`` with ``name``,
  ``version``, ``source``, ``kind`` and ``root``).

:func:`embedded_dependencies` returns what either holds; a binary with neither (a
C program, a Go binary older than 1.18, Rust built without ``cargo auditable``)
yields ``None``.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from typing import Literal

EcosystemKind = Literal["go-module", "crate"]

_GO_MAGIC = b"\xff Go buildinf:"
_GO_INLINE = 0x2
"""Header flag: version and module strings follow the header inline (Go 1.18+)."""
_GO_SENTINEL = 16
"""Bytes of the random sentinel around the module list string."""
_DEP_SECTION = b".dep-v0"
_MAX_JSON = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EmbeddedDependency:
    """One module or crate a binary was built from."""

    kind: EcosystemKind
    name: str
    version: str
    source: str = ""
    """``crates.io``, ``git``, ``local``, ``registry`` for a crate; a Go module's ``h1:`` sum."""
    main: bool = False
    """The binary's own module (Go ``mod``) or root crate."""


def _uvarint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while offset < len(data) and shift < 64:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated varint")


def go_modules(data: bytes) -> tuple[EmbeddedDependency, ...] | None:
    """The modules of the Go binary *data*, its main module first; ``None`` when it has no
    inline build information."""
    at = data.find(_GO_MAGIC)
    while at != -1 and at % 16:
        at = data.find(_GO_MAGIC, at + 1)
    if at == -1 or len(data) < at + 32 or not data[at + 15] & _GO_INLINE:
        return None
    try:
        length, offset = _uvarint(data, at + 32)
        offset += length  # the toolchain version (go1.24.4)
        length, offset = _uvarint(data, offset)
    except ValueError:
        return None
    raw = data[offset : offset + length]
    if len(raw) < 2 * _GO_SENTINEL:
        return ()
    text = raw[_GO_SENTINEL:-_GO_SENTINEL].decode("utf-8", "replace")
    found: list[EmbeddedDependency] = []
    for line in text.splitlines():
        word, _, rest = line.partition("\t")
        fields = rest.split("\t")
        if word in ("mod", "dep") and len(fields) >= 2:
            sum_ = fields[2] if len(fields) > 2 else ""
            found.append(EmbeddedDependency("go-module", fields[0], fields[1], sum_, word == "mod"))
        elif word == "=>" and found and len(fields) >= 2:
            last = found[-1]
            sum_ = fields[2] if len(fields) > 2 else ""
            found[-1] = EmbeddedDependency("go-module", fields[0], fields[1], sum_, last.main)
    return tuple(found)


def _elf_section(data: bytes, name: bytes) -> bytes | None:
    """The bytes of section *name* of the little-endian ELF64 file *data*."""
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    entsize, count, strndx = struct.unpack_from("<HHH", data, 0x3A)
    if entsize < 64 or strndx >= count or shoff + count * entsize > len(data):
        return None

    def header(index: int) -> tuple[int, int, int]:
        base = shoff + index * entsize
        name_at = struct.unpack_from("<I", data, base)[0]
        offset, size = struct.unpack_from("<QQ", data, base + 0x18)
        return name_at, offset, size

    _, strtab, strsize = header(strndx)
    names = data[strtab : strtab + strsize]
    for index in range(count):
        name_at, offset, size = header(index)
        end = names.find(b"\0", name_at)
        if names[name_at:end] == name and offset + size <= len(data):
            return data[offset : offset + size]
    return None


def cargo_crates(data: bytes) -> tuple[EmbeddedDependency, ...] | None:
    """The crates ``cargo auditable`` recorded in the ELF binary *data*, root crates first;
    ``None`` without a readable ``.dep-v0`` section."""
    section = _elf_section(data, _DEP_SECTION)
    if section is None:
        return None
    try:
        inflater = zlib.decompressobj()
        payload = json.loads(inflater.decompress(section, _MAX_JSON))
        packages = payload["packages"]
    except (zlib.error, ValueError, KeyError, TypeError):
        return None
    found = [
        EmbeddedDependency(
            "crate",
            str(package["name"]),
            str(package["version"]),
            str(package.get("source", "")),
            bool(package.get("root", False)),
        )
        for package in packages
        if isinstance(package, dict)
        and "name" in package
        and "version" in package
        and package.get("kind", "runtime") != "build"
    ]
    return tuple(sorted(found, key=lambda dep: (not dep.main, dep.name, dep.version)))


def embedded_dependencies(data: bytes) -> tuple[EmbeddedDependency, ...] | None:
    """What the binary *data* records about its dependencies: Go modules or auditable
    crates; ``None`` when it records neither."""
    found = cargo_crates(data)
    return go_modules(data) if found is None else found
