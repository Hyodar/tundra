"""A credential never sits in a source URL, nor in the lock, the manifest or the SBOM."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tundravm import manifest
from tundravm.declarative.bom import Component, Sbom
from tundravm.errors import ValidationError
from tundravm.lockfile import LockedFetch, serialize_lockfile
from tundravm.lockfile.model import LOCKFILE_VERSION, Lockfile
from tundravm.sources import Git, Http

SECRET = "s3cr3tT0kenValue42"


@pytest.mark.parametrize(
    ("make", "url", "refused"),
    [
        (Git, "https://github.com/org/repo", None),
        (Git, "git@github.com:org/repo.git", None),
        (Git, "ssh://git@github.com/org/repo.git", None),
        (Git, f"https://user:{SECRET}@github.com/org/repo", "a user name or token"),
        (Git, f"https://{SECRET}@github.com/org/repo", "a user name or token"),
        (Git, f"ssh://git:{SECRET}@example.com/repo.git", "a password"),
        (Http, "https://example.com/tool-1.0.tar.gz?download=1", None),
        (Http, f"https://example.com/tool.tar.gz?token={SECRET}", "a token= query"),
        (Http, f"https://example.com/tool.tar.gz?X-Amz-Signature={SECRET}", "Signature="),
    ],
)
def test_source_url_credential_is_refused(
    make: Callable[..., object], url: str, refused: str | None
) -> None:
    args = (url, "v1.0") if make is Git else (url,)
    if refused is None:
        make(*args)
        return
    with pytest.raises(ValidationError) as caught:
        make(*args)
    text = str(caught.value)
    assert refused in text
    assert SECRET not in text
    assert 'fix: Git("<url without the credential>", ..., auth="GITHUB_TOKEN")' in text or (
        'fix: Http("<url without the credential>", ..., auth="GITHUB_TOKEN")' in text
    )


def _lock(url: str, _: Path) -> None:
    fetch = LockedFetch(source=url, kind="git", digest="0" * 40, name="app", ref="v1")
    serialize_lockfile(Lockfile(LOCKFILE_VERSION, "sha256:0", {}, {}, fetches=[fetch]))


def _manifest(url: str, out: Path) -> None:
    manifest.write_canonical(out / manifest.MANIFEST, {"sources": [{"url": url}]})


def _sbom(url: str, _: Path) -> None:
    component = Component("build", "app", version="0" * 40, url=url, ref="v1")
    Sbom("default", (component,)).render()


@pytest.mark.parametrize("write", [_lock, _manifest, _sbom])
@pytest.mark.parametrize(
    "url",
    [f"https://user:{SECRET}@github.com/org/repo", f"https://example.com/a.tgz?token={SECRET}"],
)
def test_artifacts_never_hold_a_credential(
    write: Callable[[str, Path], None], url: str, tmp_path: Path
) -> None:
    with pytest.raises(ValidationError, match="a credential is never written") as caught:
        write(url, tmp_path)
    assert SECRET not in str(caught.value)
    assert not (tmp_path / manifest.MANIFEST).exists()
