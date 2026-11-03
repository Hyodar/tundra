"""A credential never sits in a source URL, nor in the lock, the manifest or the SBOM."""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tundravm import manifest
from tundravm._source import (
    CargoBuild,
    GitSource,
    GoBuild,
    HttpSource,
    _git_deps_auth_env,
    _request,
    default_resolver,
    fetch_source,
    git_auth_env,
)
from tundravm.declarative.bom import Component, Sbom
from tundravm.errors import SourceError, ValidationError
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


@pytest.mark.parametrize(
    ("make", "url", "auth", "refused"),
    [
        (Git, "https://github.com/org/repo", "GITHUB_TOKEN", None),
        (Git, "https://gitlab.com/org/repo", ("CI_JOB_TOKEN", "basic:gitlab-ci-token"), None),
        (Git, "https://github.com/org/repo", SECRET + "-x", "never the token"),
        (Git, "https://github.com/org/repo", (SECRET + "-x", "bearer"), "never the token"),
        (Git, "https://git.example.com/r", ("TOKEN", "basic"), 'not "bearer" or "basic:USER"'),
        (Git, "https://git.example.com/r", ("TOKEN",), "two strings"),
        (Http, "https://example.com/a.tgz", ("TOKEN", "bearer"), "a bearer header"),
        (Git, "git@github.com:org/repo.git", "GITHUB_TOKEN", "over https only"),
        (Git, "http://example.com/org/repo", ("TOKEN", "bearer"), "over https only"),
    ],
)
def test_auth_names_a_variable_on_an_https_url(
    make: Callable[..., object], url: str, auth: object, refused: str | None
) -> None:
    args = (url, "v1") if make is Git else (url,)
    if refused is None:
        make(*args, auth=auth)
        return
    with pytest.raises(ValidationError, match=refused) as caught:
        make(*args, auth=auth)
    assert SECRET not in str(caught.value) and "\nfix: " in str(caught.value)


def _basic(user: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{SECRET}".encode()).decode()


@pytest.mark.parametrize(
    ("url", "scheme", "count", "index", "header"),
    [
        ("https://github.com/org/repo", None, None, 0, _basic("x-access-token")),
        ("https://gitlab.com/org/repo", None, "2", 2, _basic("oauth2")),
        ("https://git.example.com:8443/repo", None, None, 0, f"Bearer {SECRET}"),
        (
            "https://gitlab.com/org/repo",
            "basic:gitlab-ci-token",
            None,
            0,
            _basic("gitlab-ci-token"),
        ),
        (
            "https://ghe.example.com/org/repo",
            "basic:x-access-token",
            None,
            0,
            _basic("x-access-token"),
        ),
        ("https://code.example.com/org/repo", "basic:oauth2", None, 0, _basic("oauth2")),
        ("https://github.com/org/repo", "bearer", None, 0, f"Bearer {SECRET}"),
    ],
)
def test_git_auth_env_sends_the_header_by_host(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    scheme: str | None,
    count: str | None,
    index: int,
    header: str,
) -> None:
    monkeypatch.setenv("SRC_TOKEN", SECRET)
    if count is None:
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", count)
    scope = url[: url.index("/", len("https://"))]
    auth = "SRC_TOKEN" if scheme is None else ("SRC_TOKEN", scheme)
    assert git_auth_env(GitSource(url, "v1", auth=auth)) == {
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": f"http.{scope}/.extraHeader",
        f"GIT_CONFIG_VALUE_{index}": f"Authorization: {header}",
    }


def test_the_host_git_gets_the_token_in_its_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lock`` and ``build`` run git with the header in ``GIT_CONFIG_*``, never in argv."""
    log = tmp_path / "git.log"
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    commit = "a" * 40
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s\\n" "$*" "$GIT_CONFIG_KEY_0" "$GIT_CONFIG_VALUE_0" >> {log}\n'
        f'[ "$1" = ls-remote ] && printf "{commit}\\trefs/tags/v1\\n"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("SRC_TOKEN", raising=False)
    source = GitSource("https://git.example.com/org/repo", "v1", auth="SRC_TOKEN")
    with pytest.raises(SourceError, match="SRC_TOKEN") as caught:
        default_resolver(source)
    assert "fix: export SRC_TOKEN=" in str(caught.value)
    monkeypatch.setenv("SRC_TOKEN", SECRET)
    assert default_resolver(source) == commit
    fetch_source(source, commit, tmp_path / "checkout")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    for line in lines:
        argv, key, value = line.split("|")
        assert SECRET not in argv
        assert key == "http.https://git.example.com/.extraHeader"
        assert value == f"Authorization: Bearer {SECRET}"


def test_a_download_sends_the_token_to_its_own_host_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRC_TOKEN", SECRET)
    source = HttpSource(
        "https://files.example.com/a.tgz",
        mirrors=("https://mirror.example.org/a.tgz",),
        auth="SRC_TOKEN",
    )
    own, mirror = (_request(source, url) for url in (source.url, *source.mirrors))
    assert own.unredirected_hdrs == {"Authorization": f"Bearer {SECRET}"}
    assert not own.headers and not mirror.unredirected_hdrs and not mirror.headers


@pytest.mark.parametrize(
    ("recipe", "cli"), [(GoBuild(output="app"), None), (CargoBuild(output="app"), "true")]
)
def test_prefetch_gives_the_git_dependencies_the_token(
    monkeypatch: pytest.MonkeyPatch, recipe: GoBuild | CargoBuild, cli: str | None
) -> None:
    monkeypatch.setenv("SRC_TOKEN", SECRET)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = _git_deps_auth_env(recipe, GitSource("https://github.com/o/r", "v1", auth="SRC_TOKEN"))
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert env.get("CARGO_NET_GIT_FETCH_WITH_CLI") == cli
