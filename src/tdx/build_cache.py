"""Declarative build cache and typed mkosi path helpers.

Provides ``Cache.declare()`` for defining cached build artifacts and
``Cache.wrap()`` for generating shell scripts with automatic cache
check/restore/store logic matching the upstream nethermind-tdx pattern.

Path helpers (``Build.build_path``, ``Build.dest_path``, ``Build.output_path``)
produce typed wrappers for ``$BUILDROOT/build/``, ``$DESTDIR/``, and
``$BUILDDIR/`` respectively, enforced in ``Cache.file()`` / ``Cache.dir()``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Typed mkosi path helpers ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SrcPath:
    """A path under ``$BUILDROOT/build/``."""

    rel: str

    def __str__(self) -> str:
        return f"$BUILDROOT/build/{self.rel}"


@dataclass(frozen=True, slots=True)
class DestPath:
    """A path under ``$DESTDIR/``."""

    rel: str

    def __str__(self) -> str:
        return f"$DESTDIR/{self.rel}"


@dataclass(frozen=True, slots=True)
class OutPath:
    """A path under ``$BUILDDIR/``."""

    rel: str

    def __str__(self) -> str:
        return f"$BUILDDIR/{self.rel}"


class Build:
    """Namespace for typed mkosi path constructors."""

    @staticmethod
    def build_path(path: str) -> SrcPath:
        """Return a typed ``$BUILDROOT/build/{path}`` reference."""
        return SrcPath(path)

    @staticmethod
    def dest_path(path: str) -> DestPath:
        """Return a typed ``$DESTDIR/{path}`` reference."""
        return DestPath(path)

    @staticmethod
    def output_path(path: str) -> OutPath:
        """Return a typed ``$BUILDDIR/{path}`` reference."""
        return OutPath(path)


# ── Cache artifact declarations ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CacheFile:
    """A single file artifact to cache."""

    src: SrcPath | OutPath
    dest: DestPath
    name: str
    mode: str = "0755"


@dataclass(frozen=True, slots=True)
class CacheDir:
    """A directory artifact to cache."""

    src: SrcPath | OutPath
    dest: DestPath
    name: str


@dataclass(frozen=True, slots=True)
class CacheDecl:
    """A declared cache with a key and artifact list.

    Use :meth:`wrap` to generate a shell script that checks the cache,
    restores on hit, or runs the build command and stores on miss.
    """

    key: str
    artifacts: tuple[CacheFile | CacheDir, ...]

    def wrap(self, build_cmd: str) -> str:
        """Wrap *build_cmd* with cache check/store/restore logic.

        Generates::

            if ! (cache_exists); then
                {build_cmd} && store artifacts
            fi && restore artifacts
        """
        cache_dir = f'"$BUILDDIR/{self.key}"'
        check = f'[ -d {cache_dir} ] && [ "$(ls -A {cache_dir} 2>/dev/null)" ]'

        store_parts: list[str] = [f"mkdir -p {cache_dir}"]
        for a in self.artifacts:
            if isinstance(a, CacheFile):
                store_parts.append(f'install -D -m {a.mode} "{a.src}" {cache_dir}/{a.name}')
            else:
                store_parts.append(
                    f'mkdir -p {cache_dir}/{a.name} && cp -r "{a.src}"/* {cache_dir}/{a.name}/'
                )
        store_cmd = " && ".join(store_parts)

        restore_parts: list[str] = []
        for a in self.artifacts:
            if isinstance(a, CacheFile):
                restore_parts.append(f'install -D -m {a.mode} {cache_dir}/{a.name} "{a.dest}"')
            else:
                restore_parts.append(
                    f'mkdir -p "{a.dest}" && cp -r {cache_dir}/{a.name}/* "{a.dest}"/'
                )
        restore_cmd = " && ".join(restore_parts)

        return f"if ! ({check}); then {build_cmd} && {store_cmd}; fi && {restore_cmd}"


class Cache:
    """Declarative build cache API."""

    @staticmethod
    def file(
        src: SrcPath | OutPath,
        dest: DestPath,
        *,
        name: str,
        mode: str = "0755",
    ) -> CacheFile:
        """Declare a file artifact to cache."""
        return CacheFile(src=src, dest=dest, name=name, mode=mode)

    @staticmethod
    def dir(
        src: SrcPath | OutPath,
        dest: DestPath,
        *,
        name: str,
    ) -> CacheDir:
        """Declare a directory artifact to cache."""
        return CacheDir(src=src, dest=dest, name=name)

    @staticmethod
    def declare(
        key: str,
        artifacts: tuple[CacheFile | CacheDir, ...],
    ) -> CacheDecl:
        """Create a cache declaration with the given *key* and *artifacts*."""
        return CacheDecl(key=key, artifacts=artifacts)
