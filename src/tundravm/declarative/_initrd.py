"""The sha256 of files inside a UKI's initrd: what ``sbom --binaries`` binds binaries to.

A tundravm image is a UKI whose ``.initrd`` holds the root filesystem: one or more cpio
(``newc``) archives, each uncompressed, gzip, xz or zstd. :func:`installed_digests`
streams them and hashes the regular files at the paths asked for. zstd (mkosi's default
for a UKI) needs the host's ``zstd``; without it, or for an artifact that is not a UKI,
it returns ``None``: the bake then records nothing rather than something unverified.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Protocol, cast

from tundravm.errors import MeasurementError
from tundravm.measure import pe

_NEWC = (b"070701", b"070702")
_HEADER = 110
_TRAILER = "TRAILER!!!"
_GZIP = b"\x1f\x8b"
_XZ = b"\xfd7zXZ\x00"
_ZSTD = b"\x28\xb5\x2f\xfd"
_FORMAT_MASK = 0o170000
_REGULAR = 0o100000
_CHUNK = 1 << 20


class _Source(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def installed_digests(uki: Path, paths: Iterable[str]) -> dict[str, str] | None:
    """``{path: sha256}`` of the regular files at the image *paths* inside *uki*'s initrd.

    A path the initrd does not hold as a regular file is left out. ``None`` when the
    initrd cannot be read: not a UKI, no ``.initrd``, a compression this host cannot
    undo, or a malformed archive.
    """
    wanted = {path.lstrip("/"): path for path in paths}
    try:
        section = pe.parse(Path(uki).read_bytes()).section(".initrd")
    except (OSError, MeasurementError):
        return None
    if not section:
        return None
    found: dict[str, str] = {}
    try:
        if not _archives(_Reader(io.BytesIO(section)), wanted, found):
            return None
    except (OSError, ValueError, EOFError, lzma.LZMAError):
        return None
    return found


class _Reader:
    """A byte stream with ``peek``; the buffer is compacted only when it is refilled."""

    def __init__(self, raw: _Source) -> None:
        self.raw = raw
        self.buffer = bytearray()
        self.pos = 0

    def peek(self, size: int) -> bytes:
        if len(self.buffer) - self.pos < size:
            del self.buffer[: self.pos]
            self.pos = 0
            while len(self.buffer) < size:
                chunk = self.raw.read(max(size - len(self.buffer), _CHUNK))
                if not chunk:
                    break
                self.buffer += chunk
        return bytes(self.buffer[self.pos : self.pos + size])

    def read(self, size: int = -1, /) -> bytes:
        data = self.peek(_CHUNK if size < 0 else size)
        self.pos += len(data)
        return data

    def exact(self, size: int) -> bytes:
        data = self.read(size)
        if len(data) != size:
            raise EOFError("truncated cpio archive")
        return data

    def next_magic(self) -> bytes:
        """The first six bytes after any NUL padding; empty at the end of the stream."""
        while True:
            ahead = self.peek(_CHUNK)
            if not ahead:
                return b""
            pad = len(ahead) - len(ahead.lstrip(b"\0"))
            self.pos += pad
            if pad < len(ahead):
                return self.peek(6)


def _archives(reader: _Reader, wanted: dict[str, str], found: dict[str, str]) -> bool:
    """Read every archive in *reader*; ``False`` for a compression this host cannot undo."""
    while magic := reader.next_magic():
        if magic in _NEWC:
            _archive(reader, wanted, found)
            continue
        with _decompressed(reader, magic) as inner:
            return inner is not None and _archives(_Reader(inner), wanted, found)
    return True


@contextmanager
def _decompressed(reader: _Reader, magic: bytes) -> Iterator[_Source | None]:
    if magic.startswith(_GZIP):
        yield gzip.GzipFile(fileobj=cast("IO[bytes]", reader))
    elif magic.startswith(_XZ):
        yield lzma.LZMAFile(cast("IO[bytes]", reader))
    elif magic.startswith(_ZSTD):
        with _zstd(reader) as stream:
            yield stream
    else:
        raise ValueError("not a cpio archive")


@contextmanager
def _zstd(reader: _Reader) -> Iterator[IO[bytes] | None]:
    tool = shutil.which("zstd")
    if tool is None:
        yield None
        return
    with tempfile.TemporaryFile() as spool:
        while chunk := reader.read(_CHUNK):
            spool.write(chunk)
        spool.seek(0)
        with subprocess.Popen(
            [tool, "-dcq"], stdin=spool, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as proc:
            assert proc.stdout is not None
            yield proc.stdout
            proc.stdout.read()
        if proc.returncode != 0:
            raise ValueError("zstd could not decompress the initrd")


def _archive(reader: _Reader, wanted: dict[str, str], found: dict[str, str]) -> None:
    """One newc archive, up to its trailer: hash each wanted regular file."""
    while True:
        header = reader.exact(_HEADER)
        fields = [int(header[6 + 8 * i : 14 + 8 * i], 16) for i in range(13)]
        mode, nlink, size, namesize = fields[1], fields[4], fields[6], fields[11]
        name = reader.exact(namesize).rstrip(b"\0").decode("utf-8", "surrogateescape")
        reader.exact(-(_HEADER + namesize) % 4)
        if name == _TRAILER:
            return
        path = wanted.get(name.removeprefix("./").lstrip("/"))
        regular = mode & _FORMAT_MASK == _REGULAR and (size > 0 or nlink <= 1)
        digest = hashlib.sha256() if path is not None and regular else None
        left = size
        while left:
            chunk = reader.exact(min(left, _CHUNK))
            left -= len(chunk)
            if digest is not None:
                digest.update(chunk)
        if digest is not None and path is not None:
            found[path] = digest.hexdigest()
        reader.exact(-size % 4)
