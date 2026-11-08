"""A Go release for the host, fetched and checked as ``go`` does when it switches toolchains.

The release is the module ``golang.org/toolchain@v0.0.1-<go>.linux-<arch>`` from the Go module
proxy. Nothing in it runs before the Go checksum database vouched for it: the database's tree
head carries an Ed25519 signature by its published key, the module's record is proven in that
tree from the database's tiles, and the zip's ``h1:`` hash (``golang.org/x/mod/sumdb/dirhash``)
is the record's. The release is unpacked into ``$XDG_CACHE_HOME/tundravm/toolchains/<go>``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import platform
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from .errors import SourceError
from .project import host_cache

GO_TOOLCHAIN_MODULE = "golang.org/toolchain"
"""The module the Go proxy serves each Go release as, ``@v0.0.1-<go>.<goos>-<goarch>``."""
GO_PROXY = "https://proxy.golang.org"
"""The Go module proxy every release is fetched from."""
SUMDB = "https://sum.golang.org"
"""The Go checksum database every release is checked against."""
SUMDB_KEY = "sum.golang.org+033de0ae+Ac4zctda0e5eza+HJyk9SxEdh+s3Ux18htTTAD8OuAn8"
"""The checksum database's verifier key, as ``go`` has it built in."""
TOOLCHAINS_DIRNAME = "toolchains"
"""``$XDG_CACHE_HOME/tundravm/toolchains``: one directory per fetched Go release."""
VERIFIED = ".tundravm-verified"
"""The file in a fetched release holding the ``h1:`` hash it was checked against."""

Fetcher = Callable[[str], bytes]
"""The body at a url; raises ``OSError`` (``HTTPError``, ``URLError``) when it cannot."""

_TIMEOUT = 60.0
_TILE_HEIGHT = 8
_HASH_SIZE = 32
_DECIMAL = re.compile(rb"0|[1-9][0-9]{0,17}")
_H1 = re.compile(rb"h1:[A-Za-z0-9+/]{43}=")
_SIGNATURE_PREFIX = "\N{EM DASH} ".encode()
_GOARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class _Refused(Exception):
    """Why a download is not the release it claims to be."""


def toolchains_dir() -> Path:
    """``$XDG_CACHE_HOME/tundravm/toolchains`` (``~/.cache`` without it)."""
    return host_cache() / TOOLCHAINS_DIRNAME


def host_module(name: str) -> str:
    """``golang.org/toolchain@v0.0.1-<name>.linux-<arch>``: release *name* for this host."""
    arch = _GOARCH.get(platform.machine().lower())
    if arch is None:
        raise SourceError(
            f"No Go toolchain is published for this host's {platform.machine()} processor.",
            source=GO_TOOLCHAIN_MODULE,
            reason=f"no Go release for {platform.machine()}",
            hint="Install golang (sudo apt install golang) to prefetch with the host's go.",
        )
    return f"{GO_TOOLCHAIN_MODULE}@v0.0.1-{name}.linux-{arch}"


def fetch_go(name: str, *, fetcher: Fetcher | None = None) -> Path:
    """The ``GOROOT`` of Go release *name* (``go1.24.4``) for this host, fetched once.

    A release that cannot be downloaded or that the checksum database does not vouch for
    raises ``SourceError`` naming the fix, and nothing of it is kept.
    """
    root = toolchains_dir() / name
    if (root / VERIFIED).is_file() and (root / "bin" / "go").is_file():
        return root
    module = host_module(name)
    get = fetcher or _download
    try:
        expected = _sumdb_hash(module, get)
        archive = zipfile.ZipFile(io.BytesIO(get(_zip_url(module))))
        found = zip_hash(archive)
        if found != expected:
            raise _Refused(f"the zip hashes to {found}, not the {expected} sum.golang.org signed")
        _unpack(archive, module, root, expected)
    except HTTPError as exc:
        exc.close()
        reason = f"{urlsplit(exc.url).netloc} answered HTTP {exc.code} for {exc.url}"
        raise _not_fetched(name, module, reason) from None
    except OSError as exc:
        cause = exc.reason if isinstance(exc, URLError) else exc
        raise _not_fetched(name, module, f"cannot download it: {cause}") from None
    except (_Refused, zipfile.BadZipFile, ValueError) as exc:
        raise _not_fetched(name, module, str(exc)) from None
    return root


def _not_fetched(name: str, module: str, reason: str) -> SourceError:
    return SourceError(
        f"Cannot fetch {name}, the Go toolchain to prefetch Go sources with: {reason}.",
        source=f"{module} from {urlsplit(GO_PROXY).netloc}",
        reason=reason,
        hint=(
            "Install golang (sudo apt install golang) to prefetch with the host's go, or give "
            "the host access to proxy.golang.org and sum.golang.org."
        ),
        context={"toolchain": name, "module": module},
    )


def _download(url: str) -> bytes:
    with urlopen(url, timeout=_TIMEOUT) as response:
        body: bytes = response.read()
    return body


def _zip_url(module: str) -> str:
    path, version = module.split("@", 1)
    return f"{GO_PROXY}/{path}/@v/{version}.zip"


def zip_hash(archive: zipfile.ZipFile) -> str:
    """``dirhash.HashZip`` with ``Hash1``: the ``h1:`` hash ``go.sum`` records for a module zip."""
    names = sorted(info.filename for info in archive.infolist())
    if len(set(names)) != len(names):
        raise _Refused("the zip names a file twice")
    summary = hashlib.sha256()
    for name in names:
        if "\n" in name:
            raise _Refused("the zip names a file with a newline")
        digest = hashlib.sha256(archive.read(name)).hexdigest()
        summary.update(f"{digest}  {name}\n".encode())
    return "h1:" + base64.b64encode(summary.digest()).decode()


def _unpack(archive: zipfile.ZipFile, module: str, root: Path, checksum: str) -> None:
    """Unpack the module zip *archive* into *root*, its commands executable, as ``go`` does."""
    prefix = f"{module}/"
    root.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=root.parent))
    try:
        staged = scratch / "root"
        for info in archive.infolist():
            rel = info.filename.removeprefix(prefix)
            parts = rel.split("/")
            if rel == info.filename or "\\" in rel or any(p in ("", ".", "..") for p in parts):
                raise _Refused(f"the zip holds {info.filename!r}, outside {prefix}")
            target = staged.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            command = parts[0] == "bin" or parts[:2] == ["pkg", "tool"]
            target.chmod(0o755 if command else 0o644)
        if not (staged / "bin" / "go").is_file():
            raise _Refused("the zip holds no bin/go")
        (staged / VERIFIED).write_text(f"{checksum}\n", encoding="utf-8")
        if root.exists():
            shutil.rmtree(root)
        try:
            staged.rename(root)
        except OSError:
            if not (root / VERIFIED).is_file():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _sumdb_hash(module: str, get: Fetcher) -> str:
    """The ``h1:`` hash of *module*'s zip, as the checksum database's signed tree holds it.

    The lookup is ``tlog.ParseRecord``'s: the record's index, its text up to the first blank
    line, the signed tree head. The record must hash to leaf *index* of that tree and hold
    exactly one ``h1:`` line for *module*'s path and version.
    """
    body = get(f"{SUMDB}/lookup/{module}")
    head, sep, rest = body.partition(b"\n")
    record_end = rest.find(b"\n\n")
    if not sep or _DECIMAL.fullmatch(head) is None or record_end < 0:
        raise _Refused("sum.golang.org's answer is not a record")
    index, text, note = int(head), rest[: record_end + 1], rest[record_end + 2 :]
    size, root = _signed_tree(note)
    if index >= size:
        raise _Refused(f"sum.golang.org's record {index} is not in its tree of {size}")
    leaf = hashlib.sha256(b"\x00" + text).digest()
    if _tree_hash(size, index, leaf, _TileReader(size, get)) != root:
        raise _Refused("sum.golang.org's record is not the one its signed tree holds")
    path, version = module.split("@", 1)
    prefix = f"{path} {version} ".encode()
    hashes = [line[len(prefix) :] for line in text.split(b"\n") if line.startswith(prefix)]
    if not hashes:
        raise _Refused(f"sum.golang.org's record names no hash of {module}")
    if len(hashes) != 1 or _H1.fullmatch(hashes[0]) is None:
        raise _Refused(f"sum.golang.org's record names {module} other than once by an h1: hash")
    return hashes[0].decode()


def _verifier() -> tuple[bytes, bytes, bytes]:
    """``(name, key hash, Ed25519 public key)`` of :data:`SUMDB_KEY`, as ``note.NewVerifier``
    reads a verifier key: its hash must be that of its name and key."""
    name, key_id, key = SUMDB_KEY.split("+", 2)
    public = _b64(key.encode())
    hashed = hashlib.sha256(f"{name}\n".encode() + (public or b"")).digest()[:4]
    if public is None or len(public) != 33 or public[0] != 1 or hashed.hex() != key_id:
        raise _Refused(f"tundravm's key for {name} is not an Ed25519 verifier key")
    return name.encode(), hashed, public[1:]


def _open_note(note: bytes) -> bytes:
    """The text of the signed note *note* once :data:`SUMDB_KEY` signed it (``note.Open``).

    UTF-8 without control characters but newlines; the text ends at the last blank line;
    every signature line after it is ``— <name> <base64>``; exactly one is by the key, and
    it verifies. Lines by other keys (a witness's cosignature) are left unchecked.
    """
    name, key_hash, public = _verifier()
    try:
        decoded = note.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refused("sum.golang.org's tree head is not UTF-8") from None
    split = note.rfind(b"\n\n")
    if split < 0 or not note.endswith(b"\n") or any(c < " " and c != "\n" for c in decoded):
        raise _Refused("sum.golang.org's tree head is not a signed note")
    text, block = note[: split + 1], note[split + 2 : -1]
    ours = 0
    for line in block.split(b"\n"):
        signer, _, encoded = line.removeprefix(_SIGNATURE_PREFIX).partition(b" ")
        blob = _b64(encoded)
        if not line.startswith(_SIGNATURE_PREFIX) or not signer or blob is None or len(blob) < 5:
            raise _Refused("sum.golang.org's tree head holds a line that is no signature")
        if signer != name or blob[:4] != key_hash:
            continue
        ours += 1
        if not ed25519_verify(public, text, blob[4:]):
            raise _Refused(f"sum.golang.org's tree head carries an invalid signature by {name!r}")
    if ours != 1:
        raise _Refused(
            f"sum.golang.org's tree head carries {ours} signatures by {name.decode()}, not one"
        )
    return text


def _signed_tree(note: bytes) -> tuple[int, bytes]:
    """``(size, root hash)`` of the tree head *note*, its text exactly ``tlog.FormatTree``'s:
    ``go.sum database tree``, the size in decimal, the hash in base64, nothing more."""
    lines = _open_note(note).split(b"\n")
    if len(lines) != 4 or lines[0] != b"go.sum database tree" or not _DECIMAL.fullmatch(lines[1]):
        raise _Refused("sum.golang.org's signed note is not a tree head")
    root = _b64(lines[2])
    if root is None or len(root) != _HASH_SIZE:
        raise _Refused("sum.golang.org's tree head holds no hash")
    return int(lines[1]), root


def _b64(data: bytes) -> bytes | None:
    """*data* decoded as strict, padded base64, else ``None``."""
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error:
        return None


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _tree_hash(size: int, index: int, leaf: bytes, read: Callable[[int, int], bytes]) -> bytes:
    """The root of a tree of *size* records whose record *index* hashes to *leaf*.

    Every other hash is ``read(level, n)``, the complete subtree of ``2**level`` records
    from ``n * 2**level``, as RFC 6962 (and ``golang.org/x/mod/sumdb/tlog``) combines them.
    """

    def subtree(lo: int, hi: int) -> bytes:
        count = hi - lo
        if lo <= index < hi:
            if count == 1:
                return leaf
        elif count & (count - 1) == 0:
            level = count.bit_length() - 1
            return read(level, lo >> level)
        half = 1 << ((count - 1).bit_length() - 1)
        return _node(subtree(lo, lo + half), subtree(lo + half, hi))

    return subtree(0, size)


class _TileReader:
    """The stored hashes of a tree of *size* records, from the checksum database's tiles."""

    def __init__(self, size: int, get: Fetcher) -> None:
        self.size = size
        self.get = get
        self.tiles: dict[tuple[int, int], list[bytes]] = {}

    def __call__(self, level: int, n: int) -> bytes:
        tile_level, rest = divmod(level, _TILE_HEIGHT)
        first = n << rest
        hashes = self._tile(tile_level, first >> _TILE_HEIGHT)
        offset = first & ((1 << _TILE_HEIGHT) - 1)
        row = hashes[offset : offset + (1 << rest)]
        if len(row) != 1 << rest:
            raise _Refused("a sum.golang.org tile is shorter than its tree")
        while len(row) > 1:
            row = [_node(row[i], row[i + 1]) for i in range(0, len(row), 2)]
        return row[0]

    def _tile(self, level: int, n: int) -> list[bytes]:
        if (level, n) not in self.tiles:
            full = 1 << _TILE_HEIGHT
            width = min(full, (self.size >> (level * _TILE_HEIGHT)) - n * full)
            path = f"{SUMDB}/tile/{_TILE_HEIGHT}/{level}/{_tile_index(n)}"
            try:
                data = self.get(path)[: width * _HASH_SIZE]
            except HTTPError as exc:
                if width == full or exc.code != 404:
                    raise
                exc.close()
                data = self.get(f"{path}.p/{width}")
            if len(data) != width * _HASH_SIZE:
                raise _Refused(f"sum.golang.org's tile {level}/{n} is not {width} hashes")
            hashes = [data[i : i + _HASH_SIZE] for i in range(0, len(data), _HASH_SIZE)]
            self.tiles[(level, n)] = hashes
        return self.tiles[(level, n)]


def _tile_index(n: int) -> str:
    """A tile number as ``tlog.Tile.Path`` spells it: ``1234067`` is ``x001/x234/067``."""
    path = f"{n % 1000:03d}"
    while n >= 1000:
        n //= 1000
        path = f"x{n % 1000:03d}/{path}"
    return path


_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_Point = tuple[int, int, int, int]
_IDENTITY: _Point = (0, 1, 1, 0)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if x & 1 != sign else x


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0) or 0
BASE: _Point = (_BX, _BY, 1, _BX * _BY % _P)
"""Ed25519's base point, in extended coordinates."""


def point_add(p: _Point, q: _Point) -> _Point:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def point_mul(s: int, p: _Point) -> _Point:
    q = _IDENTITY
    while s > 0:
        if s & 1:
            q = point_add(q, p)
        p = point_add(p, p)
        s >>= 1
    return q


def point_compress(p: _Point) -> bytes:
    inverse = pow(p[2], _P - 2, _P)
    x, y = p[0] * inverse % _P, p[1] * inverse % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _small_order(p: _Point) -> bool:
    """Whether ``[8]p`` is the identity: *p* lies in the curve's torsion, eight points whose
    multiples ``[k]p`` a forger predicts without the secret key."""
    x, y, z, _ = point_mul(8, p)
    return x % _P == 0 and (y - z) % _P == 0


def _decompress(data: bytes) -> _Point | None:
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Whether *signature* is *public*'s Ed25519 signature of *message* (RFC 8032 5.1.7).

    Stricter than the RFC where it costs an honest signer nothing: a public key or ``R``
    of small order (the all-zero key among them) is refused, as are non-canonical
    encodings and ``S >= L``.
    """
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _decompress(public), _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L or _small_order(a) or _small_order(r):
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little")
    left, right = point_mul(s, BASE), point_add(r, point_mul(k % _L, a))
    return point_compress(left) == point_compress(right)


__all__ = ["GO_TOOLCHAIN_MODULE", "VERIFIED", "fetch_go", "host_module", "toolchains_dir"]
