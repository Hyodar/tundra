"""The ``InRelease`` of each ``AptRepository(sha256=)``: what ``lock`` records and an offline
build checks instead of fetching it."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from tundravm._source import _NETWORK_TIMEOUT, _network_reason
from tundravm.errors import LockfileError, PolicyError, SourceError
from tundravm.lockfile import LockedRepository, read_lockfile
from tundravm.models import RepositorySpec
from tundravm.project import lock_command

if TYPE_CHECKING:
    from ._lowered import Lowered
    from .lifecycle import Lock

ReleaseFetcher = Callable[[str], bytes]
"""Reads an apt archive's ``InRelease`` at a URL: :func:`fetch_release`, or a test's fake."""


def fetch_release(url: str) -> bytes:
    """The bytes at *url*, an ``InRelease`` file; :class:`~tundravm.errors.SourceError`
    naming the reason when it cannot be read."""
    try:
        with urlopen(url, timeout=_NETWORK_TIMEOUT) as response:
            return bytes(response.read())
    except HTTPError as exc:
        exc.close()
        reason = f"HTTP {exc.code}"
    except OSError as exc:
        reason = _network_reason(exc.reason if isinstance(exc, URLError) else exc)
    raise SourceError(
        f"Cannot fetch {url}: {reason}.",
        source=url,
        reason=reason,
        hint="Check the URL and the network path to its host.",
    )


def lock_repositories(
    declared: Mapping[str, RepositorySpec],
    previous: Lock | None,
    *,
    offline: bool = False,
    release: ReleaseFetcher | None = None,
) -> list[LockedRepository]:
    """The ``InRelease`` record of each digest-pinned repository in *declared*.

    A record *previous* holds for the same url, suite and sha256 is kept, so locking again
    needs no network. Any other is fetched (refused *offline*) and must hash to the declared
    ``sha256=``: an archive that moved fails, naming both digests, for review before the
    declaration changes. The record keeps the file's ``Date:``.
    """
    prior = {} if previous is None else {r.name: r for r in previous.lockfile.repositories}
    found: list[LockedRepository] = []
    for name, spec in sorted(declared.items()):
        kept = prior.get(name)
        if kept is not None and (kept.url, kept.suite, kept.sha256) == (
            spec.url,
            spec.suite,
            spec.sha256,
        ):
            found.append(kept)
            continue
        described = f"AptRepository {spec.url} {spec.suite}"
        if offline:
            raise LockfileError(
                f"{described} has no InRelease record, and an offline lock fetches nothing.",
                hint=f"Run `{lock_command()}` with network once, and commit the lockfile.",
            )
        body = (release or fetch_release)(spec.release_url)
        digest = hashlib.sha256(body).hexdigest()
        if digest != spec.sha256:
            raise LockfileError(
                f"{described} moved: its InRelease is sha256 {digest}, not {spec.sha256}.",
                hint="Review what the archive changed, then pass that sha256= and lock again.",
            )
        date = next(
            (
                line.removeprefix("Date:").strip()
                for line in body.decode("utf-8", "replace").splitlines()
                if line.startswith("Date:")
            ),
            "unknown",
        )
        found.append(LockedRepository(name, spec.url, spec.suite or "", digest, date))
    return found


def locked_releases(lowered: Lowered) -> tuple[str, ...]:
    """The mkosi ``--environment=`` arguments that give each digest-pinned repository's
    ``sync`` step the ``InRelease`` digest the lock recorded, so an offline build fetches
    nothing; :class:`~tundravm.errors.PolicyError` for one the lock has no record of."""
    declared = lowered.pinned_repositories()
    if not declared:
        return ()
    path = lowered.lock_path
    recorded = (
        {item.name: item for item in read_lockfile(path).repositories} if path.is_file() else {}
    )
    arguments: list[str] = []
    for name, spec in sorted(declared.items()):
        item = recorded.get(name)
        if item is None or item.sha256 != spec.sha256:
            raise PolicyError(
                f"AptRepository {spec.url} {spec.suite} has no InRelease record for sha256 "
                f"{spec.sha256} in {path}, and an offline build cannot fetch it.",
                hint=f"Run `{lock_command()}` with the network, then build --offline.",
                context={"operation": "bake"},
            )
        arguments.append(f"--environment={spec.locked_variable}={item.sha256}")
    return tuple(arguments)
