"""Artifact sha256s, and the cache ``status`` keeps of them (internal).

:func:`file_sha256` hashes a file's bytes. :data:`ARTIFACT_HASHER` is what
:func:`~tundravm.declarative.lifecycle.verify_artifact` hashes an artifact with:
:func:`file_sha256` unless a :class:`HashCache` is in use. Only ``status`` (without
``--verify``) uses one, so ``evidence``, ``verify``, ``measure``, ``deploy`` and
``sbom`` always hash the bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

HASH_CACHE_NAME = ".tundravm-hashes.json"
"""``BUILD/.tundravm-hashes.json``: the sha256s ``status`` computed, by path."""
HASH_CACHE_SCHEMA = "tundravm.hashes/1"

ARTIFACT_HASHER: ContextVar[Callable[[Path], str] | None] = ContextVar(
    "ARTIFACT_HASHER", default=None
)
"""The hasher artifact checks use; ``None``: :func:`file_sha256`."""


def file_sha256(path: Path) -> str:
    """The sha256 of *path*'s bytes, hex."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def artifact_sha256(path: Path) -> str:
    """*path*'s sha256 through :data:`ARTIFACT_HASHER`."""
    hasher = ARTIFACT_HASHER.get()
    return file_sha256(path) if hasher is None else hasher(path)


class HashCache:
    """The sha256 of each file by absolute path, kept while its size, mtime (ns) and inode
    are the ones recorded; read from and saved to ``BUILD/.tundravm-hashes.json``."""

    def __init__(self, build: Path) -> None:
        self.path = Path(build) / HASH_CACHE_NAME
        self._files: dict[str, dict[str, object]] = {}
        self._changed = False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        files = payload.get("files") if isinstance(payload, dict) else None
        if payload.get("schema") == HASH_CACHE_SCHEMA and isinstance(files, dict):
            self._files = {str(k): v for k, v in files.items() if isinstance(v, dict)}

    def sha256(self, path: Path) -> str:
        """*path*'s sha256: the recorded one while the file is unchanged, else hashed now."""
        info = path.stat()
        stamp = [info.st_size, info.st_mtime_ns, info.st_ino]
        key = str(path.resolve())
        found = self._files.get(key)
        if found is not None and found.get("stamp") == stamp:
            return str(found["sha256"])
        digest = file_sha256(path)
        self._files[key] = {"stamp": stamp, "sha256": digest}
        self._changed = True
        return digest

    def save(self) -> None:
        """Write the cache atomically when it changed; a build directory that cannot be
        written to keeps none."""
        if not self._changed or not self.path.parent.is_dir():
            return
        text = json.dumps({"schema": HASH_CACHE_SCHEMA, "files": self._files}, sort_keys=True)
        try:
            handle, scratch = tempfile.mkstemp(prefix=f"{self.path.name}.", dir=self.path.parent)
        except OSError:
            return
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text + "\n")
            os.replace(scratch, self.path)
        except OSError:
            Path(scratch).unlink(missing_ok=True)
            return
        self._changed = False

    @contextmanager
    def using(self) -> Iterator[HashCache]:
        """Hash artifacts through this cache within the block, then save it."""
        token = ARTIFACT_HASHER.set(self.sha256)
        try:
            yield self
        finally:
            ARTIFACT_HASHER.reset(token)
            self.save()


__all__ = [
    "ARTIFACT_HASHER",
    "HASH_CACHE_NAME",
    "HASH_CACHE_SCHEMA",
    "HashCache",
    "artifact_sha256",
    "file_sha256",
]
