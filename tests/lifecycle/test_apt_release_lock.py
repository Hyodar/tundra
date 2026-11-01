"""``lock`` records each ``AptRepository(sha256=)``'s ``InRelease``; ``lock --check`` reports
a repository whose pin moved since; an offline build hands the recorded digest to the
``sync`` step instead of fetching the file."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

import tundravm
from tundravm import AptRepository, Debian, Image
from tundravm.declarative._apt import locked_releases
from tundravm.declarative.lifecycle import lock_image, write_lock
from tundravm.errors import LockfileError, PolicyError

KEYRING = "/usr/share/keyrings/debian-archive-keyring.gpg"
BODIES = {
    "old": b"Origin: Example\nDate: Sat, 01 Nov 2026 08:00:00 UTC\n",
    "new": b"Origin: Example\nDate: Sun, 02 Nov 2026 08:00:00 UTC\n",
}
DIGESTS = {name: hashlib.sha256(body).hexdigest() for name, body in BODIES.items()}
NAME = "apt.example.com-debian-stable"


def image(body: str) -> Image:
    repository = AptRepository(
        "https://apt.example.com/debian", "stable", key=KEYRING, sha256=DIGESTS[body]
    )
    return Image("apt", base=Debian(repositories=[repository]))


def upstream(body: str | None) -> Callable[[str], bytes]:
    def fetch(url: str) -> bytes:
        assert url == "https://apt.example.com/debian/dists/stable/InRelease"
        if body is None:
            raise AssertionError("fetched a recorded InRelease")
        return BODIES[body]

    return fetch


def outcome(declared: str, served: str | None, previous: str | None, step: str) -> str:
    """What *step* says for an image declaring *declared*'s digest, upstream serving
    *served*, over a lock taken when it declared *previous*."""
    prior = None
    if previous is not None:
        prior = lock_image(tundravm.lower(image(previous)), None, release=upstream(previous))
    lowered = tundravm.lower(image(declared))
    try:
        if step == "check":
            assert prior is not None
            drift = lowered.drift(prior.lockfile, resolver=None, partial=False)
            return " ".join(f"{s}: {drift.details[s]}" for s in drift.changed if "repo" in s)
        if step == "build --offline":
            if prior is not None:
                write_lock(prior, lowered.lock_path)
            return " ".join(locked_releases(lowered))
        locked = lock_image(
            lowered,
            None,
            previous=prior,
            offline=step == "lock --offline",
            release=upstream(served),
        )
    except (LockfileError, PolicyError) as exc:
        return str(exc)
    (record,) = locked.lockfile.repositories
    return f"{record.name} {record.sha256[:7]} {record.date}"


@pytest.mark.parametrize(
    ("declared", "served", "previous", "step", "expected"),
    [
        ("old", "old", None, "lock", f"{NAME} {DIGESTS['old'][:7]} Sat, 01 Nov 2026 08:00:00 UTC"),
        ("old", "new", None, "lock", f"its InRelease is sha256 {DIGESTS['new']}, not"),
        ("old", None, "old", "lock --offline", f"{NAME} {DIGESTS['old'][:7]} Sat, 01 Nov"),
        ("new", None, "old", "lock --offline", "has no InRelease record, and an offline lock"),
        (
            "new",
            None,
            "old",
            "check",
            f"repositories.{NAME}: InRelease {DIGESTS['old'][:7]} -> {DIGESTS['new'][:7]}",
        ),
        (
            "old",
            None,
            "old",
            "build --offline",
            "--environment=TUNDRAVM_INRELEASE_APT_EXAMPLE_COM_DEBIAN_STABLE=" + DIGESTS["old"],
        ),
        ("new", None, "old", "build --offline", "and an offline build cannot fetch it"),
    ],
    ids=[
        "lock-records",
        "lock-refuses-a-moved-archive",
        "offline-lock-keeps-the-record",
        "offline-lock-needs-one",
        "check-reports-the-moved-pin",
        "offline-build-passes-the-record",
        "offline-build-needs-one",
    ],
)
def test_the_lock_records_each_pinned_repositorys_inrelease(
    isolated_cwd: Path,
    declared: str,
    served: str | None,
    previous: str | None,
    step: str,
    expected: str,
) -> None:
    assert expected in outcome(declared, served, previous, step)
