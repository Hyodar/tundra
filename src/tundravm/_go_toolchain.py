"""A Go release for the host, fetched and checked as ``go`` does when it switches toolchains.

The release is the module ``golang.org/toolchain@v0.0.1-<go>.linux-<arch>`` from the Go module
proxy. Nothing in it runs before the Go checksum database vouched for it: the database's tree
head carries an Ed25519 signature by its published key, the module's record is proven in that
tree from the database's tiles, and the zip's ``h1:`` hash (``golang.org/x/mod/sumdb/dirhash``)
is the record's. The largest tree head seen is remembered, as ``go`` remembers it, and every
later one must be consistent with it. The release is unpacked into
``$XDG_CACHE_HOME/tundravm/toolchains/<go>``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import io
import os
import platform
import re
import shutil
import ssl
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

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
"""The file in a fetched release naming its module and the ``h1:`` hash it was checked
against, written last before the release is renamed into place."""
TREE_HEAD = "sum.golang.org.latest"
"""The file in the toolchains directory holding the largest tree head sum.golang.org signed
for this user, as ``go`` keeps ``pkg/sumdb/sum.golang.org/latest``: a ``size``, a ``hash``
and a ``seen`` line, replaced whole once a fetch verifies a larger tree."""

Fetcher = Callable[[str], bytes]
"""The body at a url; raises ``OSError`` (``HTTPError``, ``URLError``) when it cannot."""

_HOSTS = frozenset({urlsplit(GO_PROXY).hostname or "", urlsplit(SUMDB).hostname or ""})
"""The only hosts tundravm asks for a Go release."""
_REDIRECTS = {"proxy.golang.org": frozenset({"storage.googleapis.com"})}
"""Where else a host may redirect: the proxy serves its zips from Google Cloud Storage."""
_TIMEOUT = 60.0
"""Seconds a connection may stay silent."""
_TEXT_LIMIT = 1 << 20
"""Bytes a lookup or a tile may take: a tile is 8 KiB, a lookup under 1 KiB."""
_TEXT_SECONDS = 60.0
_ZIP_LIMIT = 200 << 20
"""Bytes a release zip may take: Go 1.25's is 64 MB."""
_ZIP_SECONDS = 1200.0
"""Seconds a release zip may take: 64 MB at 1 Mbit/s with room to spare."""
_TILE_HEIGHT = 8
_HASH_SIZE = 32
_DECIMAL = re.compile(rb"0|[1-9][0-9]{0,17}")
_H1 = re.compile(rb"h1:[A-Za-z0-9+/]{43}=")
_SIGNATURE_PREFIX = "\N{EM DASH} ".encode()
_HEAD = re.compile(rb"size ([1-9][0-9]{0,17})\nhash ([A-Za-z0-9+/]{43}=)\nseen [0-9:TZ-]+\n")
_CHUNK = 1 << 20
_UNZIPPED_LIMIT = 500 << 20
"""Bytes a toolchain zip may unpack to: ``golang.org/x/mod/zip``'s ``MaxZipFile``; Go
1.25's is 190 MB."""
_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    NotImplementedError,
    RuntimeError,
    ValueError,
)
"""What reading a hostile zip raises: a bad CRC or header, a broken deflate stream, a
truncated entry, a method or encryption ``zipfile`` lacks, a name that is not UTF-8."""
_AMD64 = frozenset({"x86_64", "amd64"})
_RELEASE = re.compile(r"go1\.(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)|(rc|beta)[1-9][0-9]*)")
"""A Go release's name: ``go1.25.0``, ``go1.25rc1``; never a path."""


class _Refused(Exception):
    """Why a download is not the release it claims to be."""


class _Forked(_Refused):
    """A tree head that contradicts the remembered one, or a remembered head that cannot be
    read: *hint* is the fix, since no other host or package helps."""

    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(reason)
        self.hint = hint


def toolchains_dir() -> Path:
    """``$XDG_CACHE_HOME/tundravm/toolchains`` (``~/.cache`` without it)."""
    return host_cache() / TOOLCHAINS_DIRNAME


def host_module(name: str) -> str:
    """``golang.org/toolchain@v0.0.1-<name>.linux-amd64``: release *name* for this host.

    A *name* that is not a release, or a host other than linux/amd64 (the platform
    tundravm builds on), raises ``SourceError``.
    """
    if _RELEASE.fullmatch(name) is None:
        raise SourceError(
            f"{name!r} is not a Go release, so tundravm fetches no toolchain for it.",
            source=GO_TOOLCHAIN_MODULE,
            reason=f"{name!r} is not a Go release",
            hint="Build a revision whose go.mod names a Go release (go1.N.P, go1.NrcM).",
        )
    host = f"{platform.system().lower()}/{platform.machine()}"
    if platform.system() != "Linux" or platform.machine().lower() not in _AMD64:
        raise SourceError(
            f"tundravm fetches Go only for linux/amd64, the platform it builds on, and this "
            f"host is {host}.",
            source=GO_TOOLCHAIN_MODULE,
            reason=f"no fetched Go for {host}",
            hint="Install golang (sudo apt install golang) to prefetch with the host's go.",
        )
    return f"{GO_TOOLCHAIN_MODULE}@v0.0.1-{name}.linux-amd64"


def fetch_go(name: str, *, fetcher: Fetcher | None = None) -> Path:
    """The ``GOROOT`` of Go release *name* (``go1.24.4``) for this host, fetched once.

    A release that cannot be downloaded or that the checksum database does not vouch for
    raises ``SourceError`` naming the fix, and nothing of it is kept. One fetched before
    is used again only as :func:`_verified` finds it.
    """
    module = host_module(name)
    directory = _toolchains()
    root = directory / name
    if _verified(root, module):
        return root
    get = fetcher or _download
    head = directory / TREE_HEAD
    try:
        expected, newer = _sumdb_hash(module, get, head)
        archive = zipfile.ZipFile(io.BytesIO(get(_zip_url(module))))
        members = _members(archive)
        found = _dirhash(archive, members)
        if found != expected:
            raise _Refused(f"the zip hashes to {found}, not the {expected} sum.golang.org signed")
        try:
            _unpack(archive, members, module, root, expected)
        except OSError as exc:
            raise _Refused(f"cannot unpack it into {directory}: {exc.strerror or exc}") from None
        if newer is not None:
            try:
                _remember(head, *newer)
            except OSError as exc:
                raise _Refused(f"cannot write {head}: {exc.strerror or exc}") from None
    except HTTPError as exc:
        exc.close()
        reason = f"{urlsplit(exc.url).netloc} answered HTTP {exc.code} for {exc.url}"
        raise _not_fetched(name, module, reason) from None
    except OSError as exc:
        cause = exc.reason if isinstance(exc, URLError) else exc
        raise _not_fetched(name, module, f"cannot download it: {cause}") from None
    except _Forked as exc:
        raise _not_fetched(name, module, str(exc), exc.hint) from None
    except (_Refused, *_ZIP_ERRORS) as exc:
        raise _not_fetched(name, module, str(exc) or type(exc).__name__) from None
    return root


def _toolchains() -> Path:
    """:func:`toolchains_dir`, created, once it is this user's and no one else can write it.

    Another user's directory there (a shared ``XDG_CACHE_HOME``) could hold a release with
    its marker that this user would run.
    """
    directory = toolchains_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if not _owned(directory.stat()):
        raise SourceError(
            f"{directory} belongs to another user or others can write it, so a Go toolchain "
            "in it cannot be trusted.",
            source=GO_TOOLCHAIN_MODULE,
            reason=f"{directory} is not this user's alone",
            hint=f"Remove {directory} (or set XDG_CACHE_HOME to a directory of your own).",
        )
    return directory


def _owned(st: os.stat_result) -> bool:
    return st.st_uid == os.geteuid() and not st.st_mode & stat.S_IWOTH


def _verified(root: Path, module: str) -> bool:
    """Whether *root* is release *module* as :func:`_unpack` left it: a directory (not a
    link) of this user's whose :data:`VERIFIED` marker, written only after the zip hashed
    to the checksum database's record, names *module* and an ``h1:`` hash."""
    try:
        directory, marker, go = (path.lstat() for path in (root, root / VERIFIED, root / "bin/go"))
        words = (root / VERIFIED).read_bytes().split(b" ")
    except OSError:
        return False
    return (
        stat.S_ISDIR(directory.st_mode)
        and stat.S_ISREG(marker.st_mode)
        and stat.S_ISREG(go.st_mode)
        and _owned(directory)
        and _owned(marker)
        and len(words) == 2
        and words[0] == module.encode()
        and _H1.fullmatch(words[1].removesuffix(b"\n")) is not None
        and words[1].endswith(b"\n")
    )


def _not_fetched(name: str, module: str, reason: str, hint: str | None = None) -> SourceError:
    return SourceError(
        f"Cannot fetch {name}, the Go toolchain to prefetch Go sources with: {reason}.",
        source=f"{module} from {urlsplit(GO_PROXY).netloc}",
        reason=reason,
        hint=hint
        or (
            "Install golang (sudo apt install golang) to prefetch with the host's go, or give "
            "the host access to proxy.golang.org and sum.golang.org."
        ),
        context={"toolchain": name, "module": module},
    )


class _Redirect(HTTPRedirectHandler):
    """Follows a redirect only over HTTPS, to the same host or one :data:`_REDIRECTS` allows."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> Request | None:
        origin, target = urlsplit(req.full_url).hostname or "", urlsplit(newurl)
        allowed = _REDIRECTS.get(origin, frozenset()) | {origin}
        if target.scheme != "https" or target.hostname not in allowed:
            fp.close()
            where = f"{target.scheme}://{target.netloc}{target.path}"
            raise _Refused(f"{origin} redirected to {where}, which tundravm does not fetch from")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_Redirect(), HTTPSHandler(context=ssl.create_default_context()))


def _download(url: str) -> bytes:
    """The body at *url*, on one of :data:`_HOSTS` over HTTPS with certificates checked.

    A lookup or tile within :data:`_TEXT_LIMIT` bytes and :data:`_TEXT_SECONDS`, a zip
    within :data:`_ZIP_LIMIT` and :data:`_ZIP_SECONDS`, and never :data:`_TIMEOUT` seconds
    without a byte; a body cut short raises ``ConnectionError``.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or host not in _HOSTS:
        raise _Refused(f"{url} is not on {' or '.join(sorted(_HOSTS))}")
    zipped = parts.path.endswith(".zip")
    limit, seconds = (_ZIP_LIMIT, _ZIP_SECONDS) if zipped else (_TEXT_LIMIT, _TEXT_SECONDS)
    try:
        with _OPENER.open(url, timeout=_TIMEOUT) as response:
            return _read(response, host, limit, seconds)
    except http.client.HTTPException as exc:
        raise ConnectionError(f"{host} broke off its answer ({type(exc).__name__})") from None


def _read(response: http.client.HTTPResponse, host: str, limit: int, seconds: float) -> bytes:
    """*response*'s body from *host*: refused past *limit* bytes, declared or sent, and a
    ``TimeoutError`` once it takes more than *seconds* (each read returns after one
    ``recv``, so a server trickling bytes cannot stretch it)."""
    declared = response.getheader("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _Refused(f"{host} answers {declared} bytes, more than {limit >> 20} MiB")
    deadline = monotonic() + seconds
    body = bytearray()
    while chunk := response.read1(_CHUNK):
        body += chunk
        if len(body) > limit:
            raise _Refused(f"{host} answers more than {limit >> 20} MiB")
        if monotonic() > deadline:
            raise TimeoutError(f"{host} took more than {seconds:.0f} s to answer")
    if response.length:
        raise ConnectionError(f"{host} broke off its answer, {response.length} bytes short")
    return bytes(body)


def _zip_url(module: str) -> str:
    path, version = module.split("@", 1)
    return f"{GO_PROXY}/{path}/@v/{version}.zip"


Members = list[tuple[bytes, zipfile.ZipInfo]]
"""A module zip's files, ``(name, entry)`` sorted by name, the name as ``archive/zip`` reads
it: the central directory's bytes, whatever the UTF-8 flag or a Unicode path field says."""


def _members(archive: zipfile.ZipFile) -> Members:
    """*archive*'s files, refused when ``go`` could read them otherwise or unpack too much.

    Two entries of one name, a name holding a newline or NUL, an encrypted entry, one
    neither stored nor deflated (all ``archive/zip`` reads), or files adding up past
    :data:`_UNZIPPED_LIMIT` (``modzip.MaxZipFile``).
    """
    members: dict[bytes, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        raw = info.orig_filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
        if raw in members:
            raise _Refused(f"the zip names {raw!r} twice")
        if b"\n" in raw or b"\0" in raw:
            raise _Refused(f"the zip names a file {raw!r}")
        if info.flag_bits & 0x1 or info.compress_type not in _ZIP_METHODS:
            raise _Refused(f"the zip's {raw!r} is encrypted or compressed as go cannot read")
        total += info.file_size
        if total > _UNZIPPED_LIMIT:
            raise _Refused(f"the zip unpacks to more than {_UNZIPPED_LIMIT >> 20} MiB")
        members[raw] = info
    return sorted(members.items(), key=lambda member: member[0])


def zip_hash(archive: zipfile.ZipFile) -> str:
    """``dirhash.HashZip`` with ``Hash1``: the ``h1:`` hash ``go.sum`` records for a module zip."""
    return _dirhash(archive, _members(archive))


def _dirhash(archive: zipfile.ZipFile, members: Members) -> str:
    """``dirhash.Hash1`` of *members*: the sha256 of ``<sha256 hex>  <name>\\n`` per file,
    in name order, each file read in chunks."""
    summary = hashlib.sha256()
    for raw, info in members:
        digest = hashlib.sha256()
        with archive.open(info) as member:
            while chunk := member.read(_CHUNK):
                digest.update(chunk)
        summary.update(digest.hexdigest().encode() + b"  " + raw + b"\n")
    return "h1:" + base64.b64encode(summary.digest()).decode()


def _unpack(
    archive: zipfile.ZipFile, members: Members, module: str, root: Path, checksum: str
) -> None:
    """Unpack *members* of the verified module zip *archive* into *root*, as ``go`` does.

    Every name must lie under ``<module>/`` and name a plain path below it; files are
    written new (``O_EXCL``, never through a link), mode 0755 under ``bin/`` and
    ``pkg/tool/`` and 0644 elsewhere, whatever the zip says. All of it is staged in a
    scratch directory beside *root* with the :data:`VERIFIED` marker, then renamed into
    place, so a crash leaves nothing :func:`_verified` accepts.
    """
    prefix = f"{module}/".encode()
    scratch = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=root.parent))
    try:
        staged = scratch / "root"
        staged.mkdir()
        for raw, info in members:
            parts = raw.removeprefix(prefix).split(b"/")
            if not raw.startswith(prefix) or any(p in (b"", b".", b"..") for p in parts):
                raise _Refused(f"the zip holds {raw!r}, outside {module}/")
            target = staged.joinpath(*(os.fsdecode(part) for part in parts))
            target.parent.mkdir(parents=True, exist_ok=True)
            command = parts[0] == b"bin" or parts[:2] == [b"pkg", b"tool"]
            mode = 0o755 if command else 0o644
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, "wb") as out, archive.open(info) as member:
                shutil.copyfileobj(member, out, _CHUNK)
                os.fchmod(out.fileno(), mode)
        if not (staged / "bin" / "go").is_file():
            raise _Refused("the zip holds no bin/go")
        (staged / VERIFIED).write_text(f"{module} {checksum}\n", encoding="utf-8")
        _install(staged, root, module, scratch / "stale")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _install(staged: Path, root: Path, module: str, aside: Path) -> None:
    """Rename *staged* to *root*, first moving to *aside* whatever *root* holds that is not
    a verified *module* (a half copy, a link, an older layout); a concurrent fetch's
    verified *root* is kept, never removed under the ``go`` running from it."""
    try:
        staged.rename(root)
        return
    except OSError:
        if not root.is_symlink() and not root.exists():
            raise
    if _verified(root, module):
        return
    root.rename(aside)
    staged.rename(root)


def _sumdb_hash(module: str, get: Fetcher, latest: Path) -> tuple[str, tuple[int, bytes] | None]:
    """The ``h1:`` hash of *module*'s zip, as the checksum database's signed tree holds it,
    and that tree's ``(size, root)`` when it is larger than the one *latest* remembers.

    The lookup is ``tlog.ParseRecord``'s: the record's index, its text up to the first blank
    line, the signed tree head. The record must hash to leaf *index* of that tree and hold
    exactly one ``h1:`` line for *module*'s path and version; the tree must pass
    :func:`_check_head`.
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
    tiles = _TileReader(size, get)
    if _tree_hash(size, index, leaf, tiles) != root:
        raise _Refused("sum.golang.org's record is not the one its signed tree holds")
    newer = _check_head(latest, size, root, tiles)
    path, version = module.split("@", 1)
    prefix = f"{path} {version} ".encode()
    hashes = [line[len(prefix) :] for line in text.split(b"\n") if line.startswith(prefix)]
    if not hashes:
        raise _Refused(f"sum.golang.org's record names no hash of {module}")
    if len(hashes) != 1 or _H1.fullmatch(hashes[0]) is None:
        raise _Refused(f"sum.golang.org's record names {module} other than once by an h1: hash")
    return hashes[0].decode(), (size, root) if newer else None


def _check_head(head: Path, size: int, root: bytes, tiles: _TileReader) -> bool:
    """Whether the signed tree of *size* records hashing to *root* is larger than the one
    *head* remembers, or *head* holds none: ``go``'s sumdb client merging a new head.

    Another hash at the remembered size, a larger tree that does not begin with the
    remembered one, or a smaller tree the remembered one does not begin with is a fork,
    refused; the consistency proof is built from the larger tree's tiles.
    """
    remembered = _remembered(head)
    if remembered is None:
        return True
    known, known_root = remembered
    if size == known:
        agree = root == known_root
    elif size > known:
        agree = _consistent(known, known_root, size, root, tiles)
    else:
        agree = _consistent(size, root, known, known_root, _TileReader(known, tiles.get))
    if not agree:
        raise _Forked(
            f"sum.golang.org signs a tree of {size} records that disagrees with the one of "
            f"{known} it signed before (remembered in {head}), so this network shows a forked "
            "checksum database",
            f"Fetch from another network; remove {head} only if you have reason to trust "
            "this view of sum.golang.org.",
        )
    return size > known


def _remembered(head: Path) -> tuple[int, bytes] | None:
    """``(size, root)`` of the tree head *head* holds in :data:`TREE_HEAD`'s format, or
    ``None`` when there is no *head*: a first fetch, or a cache wiped since."""
    try:
        found = _HEAD.fullmatch(head.read_bytes())
    except FileNotFoundError:
        return None
    except OSError:
        found = None
    root = _b64(found.group(2)) if found else None
    if found is None or root is None:
        raise _Forked(
            f"{head} is not a tree head tundravm remembered",
            f"Remove {head}; the next fetch remembers the tree head sum.golang.org signs then.",
        )
    return int(found.group(1)), root


def _remember(head: Path, size: int, root: bytes) -> None:
    """Replace *head* with the tree of *size* records hashing to *root*, seen now: written
    beside it and renamed over it, so a reader finds the old head or the new one whole."""
    seen = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = f"size {size}\nhash {base64.b64encode(root).decode()}\nseen {seen}\n"
    fd, scratch = tempfile.mkstemp(prefix=f".{head.name}.", dir=head.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(scratch, head)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


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


def _subtree(lo: int, hi: int, read: Callable[[int, int], bytes]) -> bytes:
    """``MTH(D[lo:hi])``, the hash of records *lo* to *hi*, every complete subtree from *read*."""
    count = hi - lo
    if count & (count - 1) == 0:
        level = count.bit_length() - 1
        return read(level, lo >> level)
    half = 1 << ((count - 1).bit_length() - 1)
    return _node(_subtree(lo, lo + half, read), _subtree(lo + half, hi, read))


def _consistent(
    old: int, old_root: bytes, new: int, new_root: bytes, read: Callable[[int, int], bytes]
) -> bool:
    """Whether the tree of *old* records hashing to *old_root* is the first *old* records of
    the one of *new* (``0 < old < new``) hashing to *new_root*.

    The proof is RFC 6962's ``SUBPROOF(old, D[0:new], true)`` from *read*'s hashes of the
    larger tree, checked as RFC 9162 2.1.4.2 checks one: both roots are recomputed from it,
    so a forged hash misses one of them.
    """

    def subproof(m: int, lo: int, hi: int, complete: bool) -> list[bytes]:
        if m == hi - lo:
            return [] if complete else [_subtree(lo, hi, read)]
        k = 1 << ((hi - lo - 1).bit_length() - 1)
        if m <= k:
            return [*subproof(m, lo, lo + k, complete), _subtree(lo + k, hi, read)]
        return [*subproof(m - k, lo + k, hi, False), _subtree(lo, lo + k, read)]

    proof = subproof(old, 0, new, True)
    if old & (old - 1) == 0:
        proof.insert(0, old_root)
    fn, sn = old - 1, new - 1
    while fn & 1:
        fn, sn = fn >> 1, sn >> 1
    fr = sr = proof[0]
    for c in proof[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr, sr = _node(c, fr), _node(c, sr)
            while not fn & 1 and fn:
                fn, sn = fn >> 1, sn >> 1
        else:
            sr = _node(sr, c)
        fn, sn = fn >> 1, sn >> 1
    return fr == old_root and sr == new_root and sn == 0


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


__all__ = [
    "GO_TOOLCHAIN_MODULE",
    "TREE_HEAD",
    "VERIFIED",
    "fetch_go",
    "host_module",
    "toolchains_dir",
]
