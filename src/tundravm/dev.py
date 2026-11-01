"""``tundravm dev``: the inner loop as one verb.

Edit the image, build the dev profile, boot it, read the probe, read the logs, stop it.
``dev`` is ``build --dev`` (the lock written when missing, changed ``Local`` sources
re-pinned, the report printed) and then ``run --mode simulated --probe`` on that build, the
console followed until Ctrl-C stops the instance; the build stays. What keeps the loop
short is :func:`rebuild_reason`: the latest development build stands in for a new one
while nothing it was built from changed, and the build directory already records what
that is (the manifest's ``image_digest`` and the project lock's digest) as the lock does
each ``Local`` source's content digest, so no cache is kept beside them.
"""

from __future__ import annotations

from pathlib import Path

from .builds import Build, repin_local
from .declarative._lowered import Lowered
from .declarative.lifecycle import read_lock
from .manifest import sha256_file


def rebuild_reason(img: Lowered, profile: str, latest: Build | None, lockfile: Path) -> str | None:
    """Why ``dev`` builds *profile* of *img* again instead of running *latest* (the latest
    development build, or none): ``no dev build yet``, ``image changed``, ``Local source
    NAME changed``, ``lock changed``; ``None`` when nothing *latest* was built from changed.

    A changed image is one whose digest is not the manifest's; a changed Local source is
    one the build would re-pin (:func:`~tundravm.builds.repin_local`); a changed lock is
    one whose digest is not the one the manifest records for the project lock (a moved pin
    after ``lock --update``).
    """
    if latest is None:
        return "no dev build yet"
    if latest.profile != profile:
        return f"the latest development build is the {latest.profile} profile"
    if latest.manifest.get("image_digest") != img.select(img.profile_names).digest():
        return "image changed"
    if not lockfile.is_file():
        return "no lockfile"
    _, moved = repin_local(img, read_lock(lockfile))
    if moved:
        return f"Local source {', '.join(moved)} changed"
    if latest.manifest["lock"]["project_sha256"] != sha256_file(lockfile):
        return "lock changed"
    return None


__all__ = ["rebuild_reason"]
