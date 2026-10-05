"""Host-side source fetch: ``fetch()``, the ``.sources`` checkouts, their mount and the CLI."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import threading
from collections.abc import Iterator
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from tests.helpers import write_recipe_file
from tundravm._source import GitSource, fetch_source
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend
from tundravm.backends.base import MountSpec, Requirement
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Http,
    Install,
    Lock,
    Mkosi,
    Package,
    Policy,
    Recipe,
    lifecycle,
    lock,
    lower,
    write_lock,
)
from tundravm.declarative.lifecycle import FetchedSource, bake_image, fetch
from tundravm.errors import ErrorCode, PolicyError, SourceError, StateError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult
from tundravm.recipe import load_recipe

pytestmark = pytest.mark.usefixtures("isolated_cwd")


def _git(*args: str, cwd: Path) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, str]:
    """A local repository with two commits on ``main``: ``(file:// url, first commit)``."""
    path = tmp_path / "upstream"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "main.go").write_text("package main\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    first = _git("rev-parse", "HEAD", cwd=path)
    (path / "later.txt").write_text("later\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "two", cwd=path)
    return path.as_uri(), first


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """``(url, sha256)`` of a ``tool-1.0.tar.gz`` a local HTTP server serves."""
    root = tmp_path / "www"
    root.mkdir()
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        payload = b"#!/bin/sh\necho tool\n"
        info = tarfile.TarInfo("tool-1.0/bin/tool")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    (root / "tool-1.0.tar.gz").write_bytes(data.getvalue())
    handler = partial(_QuietHandler, directory=str(root))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/tool-1.0.tar.gz"
        yield url, hashlib.sha256(data.getvalue()).hexdigest()
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _recipe(*builds: Build, dialect: str = "current", policy: Policy | None = None) -> Recipe:
    return Recipe(
        "tool",
        Fragment("tool", items=(Package("linux-image-amd64"), *builds)),
        mkosi=Mkosi(dialect=dialect),  # type: ignore[arg-type]
        policy=policy,
    )


def _git_build(url: str, ref: str = "main") -> Build:
    return Build("tool", Git(url, ref), script="make", install=(Install("tool", "/usr/bin/tool"),))


def _locked(recipe: Recipe, pin: str) -> Lock:
    return lock(recipe, resolver=lambda source: pin)


# ── fetch() ──────────────────────────────────────────────────────────


def test_fetch_checks_the_pinned_commit_out_under_sources(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    recipe = _recipe(_git_build(url))
    (fetched,) = fetch(recipe, locked=_locked(recipe, first), out=tmp_path / "out")
    checkout = tmp_path / "out" / ".sources" / f"tool-{first[:12]}"
    assert fetched == FetchedSource("tool", "git", url, first, checkout, ref="main")
    assert (checkout / "main.go").is_file()
    assert not (checkout / "later.txt").exists()  # the pin, not the branch head
    assert _git("rev-parse", "HEAD", cwd=checkout) == first
    assert (checkout / ".tundravm-complete").read_text(encoding="utf-8") == first + "\n"
    assert [p.name for p in (tmp_path / "out" / ".sources").iterdir()] == [checkout.name]


def test_fetch_is_idempotent_and_refetches_an_incomplete_checkout(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    recipe = _recipe(_git_build(url))
    locked = _locked(recipe, first)
    fetch(recipe, locked=locked, out=tmp_path)
    upstream = Path(url.removeprefix("file://"))
    upstream.rename(upstream.with_name("gone"))
    (again,) = fetch(recipe, locked=locked, out=tmp_path)  # no network, nothing touched
    assert again.cached
    (again.path / ".tundravm-complete").unlink()
    with pytest.raises(SourceError, match=f"Cannot fetch git {url} @ main"):
        fetch(recipe, locked=locked, out=tmp_path)
    upstream.with_name("gone").rename(upstream)
    (refetched,) = fetch(recipe, locked=locked, out=tmp_path)
    assert not refetched.cached
    assert (refetched.path / ".tundravm-complete").is_file()


def test_builds_of_one_source_and_pin_share_one_fetch(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    other = Build("other", Git(url, "main"), script="make", install=(Install("o", "/usr/bin/o"),))
    recipe = _recipe(_git_build(url), other)
    fetched_from: list[str] = []
    real = fetch_source

    def counting(source: object, pin: str, dest: Path) -> None:
        fetched_from.append(pin)
        real(source, pin, dest)  # type: ignore[arg-type]

    monkeypatch.setattr(lifecycle, "fetch_source", counting)
    fetched = fetch(recipe, locked=_locked(recipe, first), out=tmp_path)
    assert fetched_from == [first]
    assert sorted(p.path.name for p in fetched) == [f"other-{first[:12]}", f"tool-{first[:12]}"]
    assert all((p.path / "main.go").is_file() for p in fetched)


def test_fetch_downloads_checks_and_unpacks_http_sources(
    tmp_path: Path, served: tuple[str, str]
) -> None:
    url, digest = served
    build = Build("tool", Http(url), script="true", install=(Install("bin/tool", "/usr/bin/tool"),))
    recipe = _recipe(build)
    (fetched,) = fetch(recipe, locked=_locked(recipe, digest), out=tmp_path)
    assert fetched.path == tmp_path / ".sources" / f"tool-{digest[:12]}"
    assert (fetched.path / "bin" / "tool").read_bytes() == b"#!/bin/sh\necho tool\n"
    assert (fetched.path / "tool-1.0.tar.gz").is_file()
    with pytest.raises(SourceError, match="does not match the pin"):
        fetch(recipe, locked=_locked(recipe, "0" * 64), out=tmp_path / "other")
    assert not (tmp_path / "other" / ".sources" / f"tool-{'0' * 12}").exists()


def test_fetch_without_a_lock_resolves_the_refs_first(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    calls: list[object] = []

    def resolver(source: object) -> str:
        calls.append(source)
        return first

    (fetched,) = fetch(_recipe(_git_build(url)), locked=None, out=tmp_path, resolver=resolver)
    assert calls == [GitSource(url, "main")]
    assert fetched.pin == first
    strict = _recipe(_git_build(url), policy=Policy(mutable_ref_policy="error"))
    with pytest.raises(PolicyError, match="not allowed by policy: tool@main"):
        fetch(strict, locked=None, out=tmp_path / "strict", resolver=resolver)


# ── bake ─────────────────────────────────────────────────────────────


class _Recording:
    """A backend that builds nothing and keeps each request."""

    name = "recording"

    def __init__(self) -> None:
        self.requests: list[BakeRequest] = []

    def requirements(self) -> tuple[Requirement, ...]:
        return ()

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return ()

    def prepare(self, request: BakeRequest) -> None:
        pass

    def execute(self, request: BakeRequest) -> BakeResult:
        self.requests.append(request)
        return BakeResult(profiles={request.profile: ProfileBuildResult(profile=request.profile)})

    def cleanup(self, request: BakeRequest) -> None:
        pass


def test_bake_fetches_then_hands_the_backend_the_sources(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    recipe = _recipe(_git_build(url))
    backend = _Recording()
    out = tmp_path / "out"
    img = lower(recipe)
    bake_image(img, None, locked=_locked(recipe, first), backend=backend, out=out)
    (request,) = backend.requests
    assert request.sources_dir == out / ".sources"
    assert (out / ".sources" / f"tool-{first[:12]}" / "main.go").is_file()
    hooks = (out / "mkosi" / "default" / "scripts" / "04-build.sh").read_text(encoding="utf-8")
    assert f'"$SRCDIR/tundravm-sources/tool-{first[:12]}"' in hooks


def test_unlocked_bake_builds_the_pins_its_fetch_resolved(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    backend = _Recording()
    out = tmp_path / "out"
    img = lower(_recipe(_git_build(url)))
    bake_image(img, None, locked=None, backend=backend, out=out, resolver=lambda source: first)
    hooks = (out / "mkosi" / "default" / "scripts" / "04-build.sh").read_text(encoding="utf-8")
    assert f"tool-{first[:12]}" in hooks and "unpinned" not in hooks
    assert img.fetched_pins == {}  # restored after the bake


def test_bake_without_fetch_fails_fast_naming_tundravm_fetch(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    recipe = _recipe(_git_build(url))
    backend = _Recording()
    with pytest.raises(StateError, match="'tool' is not fetched") as missing:
        bake_image(
            lower(recipe),
            None,
            locked=_locked(recipe, first),
            backend=backend,
            out=tmp_path / "out",
            fetch=False,
        )
    assert missing.value.code == ErrorCode.STATE
    assert "tundravm fetch" in str(missing.value.hint)
    assert backend.requests == []
    fetch(recipe, locked=_locked(recipe, first), out=tmp_path / "out")
    bake_image(
        lower(recipe),
        None,
        locked=_locked(recipe, first),
        backend=backend,
        out=tmp_path / "out",
        fetch=False,
    )
    assert [r.sources_dir for r in backend.requests] == [tmp_path / "out" / ".sources"]


def test_nethermind_v1_bakes_fetch_nothing(tmp_path: Path, repo: tuple[str, str]) -> None:
    url, first = repo
    recipe = _recipe(_git_build(url), dialect="nethermind-v1")
    backend = _Recording()
    bake_image(lower(recipe), None, locked=_locked(recipe, first), backend=backend, out=tmp_path)
    assert backend.requests[0].sources_dir is None
    assert not (tmp_path / ".sources").exists()
    hooks = (tmp_path / "mkosi" / "default" / "scripts" / "04-build.sh").read_text()
    assert f"fetch -q --depth=1 {url} {first}" in hooks


# ── backend mounts ───────────────────────────────────────────────────


def _project(tmp_path: Path, conf: str = "") -> BakeRequest:
    variant = tmp_path / "build" / "mkosi" / "default"
    variant.mkdir(parents=True)
    (variant / "mkosi.conf").write_text("[Output]\nFormat=directory\n" + conf, encoding="utf-8")
    return BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "build" / "mkosi",
        sources_dir=tmp_path / "build" / ".sources",
    )


def test_local_backend_mounts_the_sources_beside_the_config_directory(tmp_path: Path) -> None:
    request = _project(tmp_path)
    cmd = LocalLinuxBackend(privilege="none").command(request)
    mkosi_dir = (tmp_path / "build" / "mkosi" / "default").resolve()
    sources = (tmp_path / "build" / ".sources").resolve()
    assert cmd[cmd.index(f"--build-sources={mkosi_dir}") :][:3] == [
        f"--build-sources={mkosi_dir}",
        f"--build-sources={sources}:tundravm-sources",
        "--build-sources-ephemeral=yes",
    ]
    plain = LocalLinuxBackend(privilege="none").command(
        BakeRequest(profile="default", build_dir=request.build_dir, emit_dir=request.emit_dir)
    )
    assert not [arg for arg in plain if arg.startswith("--build-sources")]


def test_local_backend_keeps_the_recipes_build_sources(tmp_path: Path) -> None:
    request = _project(tmp_path, "[Build]\nBuildSources=../src\nBuildSourcesEphemeral=no\n")
    cmd = LocalLinuxBackend(privilege="none").command(request)
    sources = (tmp_path / "build" / ".sources").resolve()
    assert [arg for arg in cmd if arg.startswith("--build-sources")] == [
        f"--build-sources={sources}:tundravm-sources"
    ]


def test_nix_and_lima_backends_mount_the_sources(tmp_path: Path) -> None:
    request = _project(tmp_path)
    output = tmp_path / "build" / "default" / "output"
    nix = NixMkosiBackend()._build_mkosi_args(request, output)
    assert f"--build-sources={request.sources_dir.resolve()}:tundravm-sources" in nix  # type: ignore[union-attr]
    lima = LimaMkosiBackend(cpus=2, memory="4GiB", disk="40GiB")._build_mkosi_command(request)
    assert "--build-sources=/home/debian/mnt/mkosi/default " in lima
    assert "--build-sources=/home/debian/mnt/.sources:tundravm-sources" in lima


# ── CLI ──────────────────────────────────────────────────────────────

RECIPE = """
from tundravm.declarative import Build, Fragment, Git, Install, Package, Recipe

recipe = Recipe(
    "tool",
    Fragment(
        "tool",
        items=(
            Package("linux-image-amd64"),
            Build(
                "tool",
                Git({url!r}, "main"),
                script="make",
                install=(Install("tool", "/usr/bin/tool"),),
            ),
        ),
    ),
)
"""


def test_cli_fetch_and_bake_no_fetch(
    tmp_path: Path,
    repo: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, first = repo
    path = write_recipe_file(tmp_path, RECIPE.format(url=url), monkeypatch)
    write_lock(_locked(load_recipe(path), first), tmp_path / "build" / "tundravm.lock")
    out = io.StringIO()
    assert main(["fetch", str(path)], stdout=out) == EXIT_OK
    assert out.getvalue().splitlines() == [
        f"fetched {Path('build') / '.sources'}",
        f"  tool  {first[:12]}  fetched",
    ]
    out = io.StringIO()
    assert main(["fetch", str(path)], stdout=out) == EXIT_OK
    assert out.getvalue().splitlines()[1] == f"  tool  {first[:12]}  kept"
    bake = ["bake", str(path), "--backend", "local", "--no-fetch", "--out", "elsewhere", "-q"]
    assert main(bake, stdout=io.StringIO()) == EXIT_SDK_ERROR
    error = capsys.readouterr().err
    assert "error [E_STATE]: Source build 'tool' is not fetched" in error
    assert "tundravm fetch RECIPE --out elsewhere" in error
