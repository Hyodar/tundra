"""Shell fragment builder for $BUILDDIR-based build artifact caching.

Generates shell snippets that modules embed in their build scripts to
cache compiled binaries under ``$BUILDDIR/{name}/``, matching the
upstream nethermind-tdx caching pattern (``make_git_package.sh``,
``build_rust_package.sh``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A single cache entry under ``$BUILDDIR/{name}/``."""

    name: str

    def add_file(self, artifact: str, src: str, *, mode: str = "0755") -> str:
        """Shell command: copy *src* into the cache directory."""
        return (
            f'mkdir -p "$BUILDDIR/{self.name}" && '
            f'install -m {mode} {src} "$BUILDDIR/{self.name}/{artifact}"'
        )

    def add_dir(self, artifact: str, src: str) -> str:
        """Shell command: copy directory *src* into the cache directory."""
        return (
            f'mkdir -p "$BUILDDIR/{self.name}/{artifact}" && '
            f'cp -r {src}/* "$BUILDDIR/{self.name}/{artifact}/"'
        )

    def copy_file(self, artifact: str, dest: str, *, mode: str = "0755") -> str:
        """Shell command: install cached artifact to *dest*."""
        return f'install -m {mode} "$BUILDDIR/{self.name}/{artifact}" {dest}'

    def copy_dir(self, artifact: str, dest: str) -> str:
        """Shell command: copy cached directory to *dest*."""
        return f'mkdir -p {dest} && cp -r "$BUILDDIR/{self.name}/{artifact}"/* {dest}/'


class BuildCaches:
    """Shell fragment builder for ``$BUILDDIR``-based build artifact caching."""

    def has(self, name: str) -> str:
        """Shell expression that evaluates true when *name* cache exists and is non-empty."""
        return f'[ -d "$BUILDDIR/{name}" ] && [ "$(ls -A "$BUILDDIR/{name}" 2>/dev/null)" ]'

    def create(self, name: str) -> CacheEntry:
        """Return a :class:`CacheEntry` for storing artifacts."""
        return CacheEntry(name)

    def get(self, name: str) -> CacheEntry:
        """Return a :class:`CacheEntry` for restoring artifacts."""
        return CacheEntry(name)
