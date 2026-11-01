"""Hermetic bakes: dependency prefetch, the ``EfiStub`` package as a source, ``bake --offline``."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from tests.helpers import fetch, source_dir
from tundravm import _source
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend
from tundravm.backends.base import MountSpec, Requirement
from tundravm.declarative import (
    Build,
    Cargo,
    Dotnet,
    Git,
    Go,
    Install,
    Lock,
    Mkosi,
    Package,
    Policy,
    Recipe,
    compile,
    lint,
    lock,
    lock_status,
    lower,
)
from tundravm.declarative.lifecycle import bake_image, check_report, compile_image
from tundravm.declarative.utils import EfiStub
from tundravm.errors import ErrorCode, PolicyError, SourceError, StateError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult

pytestmark = [pytest.mark.usefixtures("isolated_cwd"), pytest.mark.boundary]

EFI_VERSION = "257.8-1~deb13u1"
DEB_BYTES = b"!<arch>\ndebian-binary   fake systemd-boot-efi\n"


def _git(*args: str, cwd: Path) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, str]:
    """A repository with a Go module, a Cargo lockfile and a .NET project: ``(url, commit)``."""
    path = tmp_path / "upstream"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "go.mod").write_text("module example.com/tool\n\ngo 1.22\n", encoding="utf-8")
    (path / "Cargo.toml").write_text('[package]\nname = "tool"\n', encoding="utf-8")
    (path / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
    (path / "App.csproj").write_text("<Project />\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    return path.as_uri(), _git("rev-parse", "HEAD", cwd=path)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def snapshot(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """``(snapshot archive url, sha256)`` serving the ``EFI_VERSION`` systemd-boot-efi package."""
    root = tmp_path / "www"
    pool = root / "archive" / "pool" / "main" / "s" / "systemd"
    pool.mkdir(parents=True)
    (pool / f"systemd-boot-efi_{EFI_VERSION}_amd64.deb").write_bytes(DEB_BYTES)
    server = HTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(root)))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/archive",
            hashlib.sha256(DEB_BYTES).hexdigest(),
        )
    finally:
        server.shutdown()
        server.server_close()


HOST_GO = "go1.24.4"
"""What the fake host ``go`` says it is."""
TOOLCHAIN = "go1.22.0"
"""What the fixtures' ``go 1.22`` selects: the first Go 1.22 release."""
TOOLCHAIN_MODULE = f"golang.org/toolchain@v0.0.1-{TOOLCHAIN}.linux-amd64"
TOOLCHAIN_ZIPHASH = f"cache/download/golang.org/toolchain/@v/v0.0.1-{TOOLCHAIN}.linux-amd64.ziphash"


def _toolchain_download(argv: Sequence[str], env: Mapping[str, str]) -> bool:
    """Fill the module cache as ``go mod download golang.org/toolchain@...`` would."""
    if tuple(argv[:3]) != ("go", "mod", "download") or len(argv) != 4:
        return False
    assert argv[3] == TOOLCHAIN_MODULE and env["GOTOOLCHAIN"] == TOOLCHAIN
    cache = Path(env["GOMODCACHE"])
    (cache / TOOLCHAIN_MODULE / "bin").mkdir(parents=True, exist_ok=True)
    (cache / TOOLCHAIN_MODULE / "bin" / "go").write_text("go\n", encoding="utf-8")
    (cache / TOOLCHAIN_ZIPHASH).parent.mkdir(parents=True, exist_ok=True)
    (cache / TOOLCHAIN_ZIPHASH).write_text("h1:toolchain=\n", encoding="utf-8")
    return True


class _Toolchain:
    """Fake host toolchains: ``which`` finds *present*; ``run`` records and fills the cache."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, present: Sequence[str], code: int = 0):
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.code = code
        found = set(present)
        monkeypatch.setattr(
            shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}" if name in found else None
        )
        monkeypatch.setattr(_source, "host_go_version", lambda: HOST_GO if "go" in found else None)
        monkeypatch.setattr(_source, "run_tool", self.run)

    def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), cwd, dict(env)))
        assert cwd.is_dir() and not (cwd / ".git").exists()  # a scratch copy, not the checkout
        if _toolchain_download(argv, env):
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        cache = env.get("GOMODCACHE") or env.get("CARGO_HOME") or argv[-1]
        (Path(cache) / "downloaded").write_text("dep\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            list(argv), self.code, "", "" if not self.code else "boom"
        )


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


RECIPES: dict[str, Go | Cargo | Dotnet] = {
    "go": Go(package="./cmd/tool", output="tool"),
    "cargo": Cargo(output="tool", bin="tool"),
    "dotnet": Dotnet(project="App.csproj", output="App"),
}
CACHES = {"go": "go", "cargo": "cargo", "dotnet": "nuget"}


def _recipe(*items: object, policy: Policy | None = None) -> Recipe:
    return Recipe(
        "tool",
        items=(Package("linux-image-amd64"), *items),  # type: ignore[arg-type]
        policy=policy,
    )


def _build(url: str, kind: str = "go") -> Build:
    return Build("tool", Git(url, "main"), using=RECIPES[kind], install=(Install("tool", "/t"),))


def _locked(recipe: Recipe, pin: str, deb: str | None = None) -> Lock:
    return lock(recipe, resolver=lambda s: deb if deb and s.kind == "http" else pin)


# ── dependency prefetch ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("go_mod", "toolchain"),
    [
        ("module m\n\ngo 1.25.0\n", "go1.25.0"),
        ("go 1.22 // a language version\n", "go1.22.0"),
        ("go 1.21rc1\n", "go1.21rc1"),
        ("go 1.25.0\ntoolchain go1.25.3\n", "go1.25.3"),
        ("go 1.25.0\ntoolchain go1.24.2\n", "go1.25.0"),
        ("go 1.25.0\ntoolchain default\n", "go1.25.0"),
        ("go 1.20\n", "local"),
        ("module m\n", "local"),
    ],
    ids=[
        "go",
        "language",
        "prerelease",
        "toolchain",
        "older-toolchain",
        "default",
        "pre-1.21",
        "none",
    ],
)
def test_the_toolchain_is_the_one_go_mod_names(go_mod: str, toolchain: str) -> None:
    """The newer of the toolchain line and the go line, as ``go`` selects it; ``go 1.N`` is
    ``go1.N.0`` from 1.21 on, and older Go has no toolchain module, so Debian's builds it."""
    assert _source.go_toolchain(go_mod) == toolchain


@pytest.mark.parametrize(
    ("host", "refused"),
    [(None, "there is no go on PATH"), ("go1.19.8", "the host's go is go1.19.8"), (HOST_GO, None)],
    ids=["missing", "too-old", "fine"],
)
def test_a_go_source_needs_go_1_21_on_the_host_before_anything_runs(
    tmp_path: Path,
    go_repo: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    host: str | None,
    refused: str | None,
) -> None:
    url, first = go_repo
    tools = _GoModules(monkeypatch, ["go"])
    monkeypatch.setattr(_source, "host_go_version", lambda: host)
    recipe = _recipe(_build(url, "go"))
    if refused is None:
        (fetched,) = fetch(recipe, lock=_locked(recipe, first), build=tmp_path)
        assert fetched.deps == "go: prefetched"
        return
    with pytest.raises(StateError) as error:
        fetch(recipe, lock=_locked(recipe, first), build=tmp_path)
    message = str(error.value)
    assert (
        f"Go source build 'tool' needs Go 1.21 or newer on the host to fetch the toolchain "
        f"and modules each go.mod names: {refused}." in message
    )
    assert "golang" in message and "https://go.dev/dl" in message
    assert "tundravm doctor --for build" in message
    assert tools.calls == [] and not (tmp_path / ".sources").exists()


def test_a_toolchain_that_is_not_a_release_is_refused() -> None:
    with pytest.raises(SourceError, match="`toolchain go1.25.0-corp` is not a Go release"):
        _source.go_toolchain("go 1.25.0\ntoolchain go1.25.0-corp\n")


RUST = "1.91.0-x86_64-unknown-linux-gnu"
"""The toolchain ``channel = "1.91.0"`` installs: the build sandbox's host triple."""


@pytest.mark.parametrize(
    ("files", "pinned"),
    [
        ({}, None),
        (
            {
                "rust-toolchain.toml": '[toolchain]\nchannel = "1.91.0"\n'
                'components = ["rustfmt", "clippy"]\ntargets = ["wasm32-unknown-unknown"]\n'
            },
            ("1.91.0", ("rustfmt", "clippy"), ("wasm32-unknown-unknown",), "minimal"),
        ),
        (
            {
                "rust-toolchain.toml": '[toolchain]\nchannel = "nightly-2026-09-01"\n'
                'profile = "default"\n'
            },
            ("nightly-2026-09-01", (), (), "default"),
        ),
        ({"../rust-toolchain": "1.91.1\n"}, ("1.91.1", (), (), "minimal")),
        (
            {
                "rust-toolchain": "1.89.0\n",
                "rust-toolchain.toml": '[toolchain]\nchannel = "1.91.0"\n',
            },
            ("1.89.0", (), (), "minimal"),
        ),
        (
            {"rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n'},
            "channel 'stable', which is not pinned",
        ),
        ({"rust-toolchain": "1.91\n"}, "channel '1.91', which is not pinned"),
        (
            {"rust-toolchain.toml": '[toolchain]\nchannel = "nightly"\n'},
            "channel 'nightly', which is not pinned",
        ),
        ({"rust-toolchain.toml": '[toolchain]\npath = "/opt/rust"\n'}, "custom toolchain"),
        ({"rust-toolchain.toml": '[toolchain]\ncomponents = ["clippy"]\n'}, "names no channel"),
    ],
    ids=[
        "none",
        "release",
        "nightly",
        "legacy-in-a-parent",
        "legacy-first",
        "stable",
        "minor",
        "undated-nightly",
        "path",
        "no-channel",
    ],
)
def test_the_rust_toolchain_is_the_one_its_file_pins(
    tmp_path: Path, files: dict[str, str], pinned: tuple[object, ...] | str | None
) -> None:
    """As rustup looks it up, from the build's directory up to the checkout, the legacy
    ``rust-toolchain`` first; a channel that names a different Rust as releases come is
    refused with the fix."""
    workdir = tmp_path / "checkout" / "crate"
    workdir.mkdir(parents=True)
    for name, text in files.items():
        (workdir / name).write_text(text, encoding="utf-8")
    if isinstance(pinned, str):
        with pytest.raises(SourceError, match=pinned) as refused:
            _source.rust_toolchain(workdir, tmp_path / "checkout")
        assert 'channel = "1.91.0"' in str(refused.value.hint)
        return
    found = _source.rust_toolchain(workdir, tmp_path / "checkout")
    shape = (
        None if found is None else (found.channel, found.components, found.targets, found.profile)
    )
    assert shape == pinned


@pytest.fixture
def rust_repo(tmp_path: Path) -> tuple[str, str]:
    """A crate whose ``rust-toolchain.toml`` pins 1.91.0 with clippy: ``(url, commit)``."""
    path = tmp_path / "rust-upstream"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "Cargo.toml").write_text('[package]\nname = "tool"\n', encoding="utf-8")
    (path / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (path / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.91.0"\ncomponents = ["clippy"]\n', encoding="utf-8"
    )
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    return path.as_uri(), _git("rev-parse", "HEAD", cwd=path)


RUSTC_VV = (
    "rustc 1.91.0 (f8297e351 2025-10-28)\nbinary: rustc\n"
    "commit-hash: f8297e351a40c1439a467bbbb6879088047f50b3\nhost: x86_64-unknown-linux-gnu\n"
)


class _Rustup(_Toolchain):
    """A fake ``rustup toolchain install`` that lays the toolchain out, its ``rustc -vV``,
    and a ``cargo fetch`` that must be the toolchain's own."""

    def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), cwd, dict(env)))
        toolchain = Path(env["RUSTUP_HOME"]) / "toolchains" / RUST
        if argv[0] == "rustup":
            for rel in ("bin/cargo", "bin/rustc", "lib/rustlib/multirust-channel-manifest.toml"):
                (toolchain / rel).parent.mkdir(parents=True, exist_ok=True)
                (toolchain / rel).write_text(rel, encoding="utf-8")
            (toolchain / "lib/rustlib/multirust-config.toml").write_text("clippy\n")
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        if argv[0].endswith("/rustc"):
            return subprocess.CompletedProcess(list(argv), 0, RUSTC_VV, "")
        assert env["PATH"].split(":")[0] == str(toolchain / "bin")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def test_a_pinned_cargo_source_fetches_with_the_toolchain_rustup_installs(
    tmp_path: Path, rust_repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's rustup installs the pinned toolchain into the workspace's RUSTUP_HOME,
    whose own cargo fetches the crates; the marker records what its rustc says, and a
    toolchain that lost a file is installed again."""
    url, first = rust_repo
    tools = _Rustup(monkeypatch, ["rustup"])
    monkeypatch.setattr(_source, "host_rustup_version", lambda: "1.28.2")
    recipe = _recipe(_build(url, "cargo"))
    out = tmp_path / "out"

    (fetched,) = fetch(recipe, lock=_locked(recipe, first), build=out)

    deps = out / ".sources" / "deps"
    home = str((deps / "rustup").resolve())
    assert fetched.deps == "cargo: prefetched"
    install, version, fetch_crates = tools.calls
    assert install[0] == (
        "rustup",
        "toolchain",
        "install",
        RUST,
        "--profile",
        "minimal",
        "--no-self-update",
        "--component",
        "clippy",
    )
    assert install[2]["RUSTUP_HOME"] == home
    assert version[0] == (f"{home}/toolchains/{RUST}/bin/rustc", "-vV")
    assert fetch_crates[0] == ("cargo", "fetch", "--locked")
    assert fetch_crates[2]["RUSTUP_TOOLCHAIN"] == RUST and fetch_crates[2]["RUSTUP_HOME"] == home
    (marker,) = deps.glob("*.cargo.json")
    assert json.loads(marker.read_text(encoding="utf-8"))["toolchain"] == {
        "name": RUST,
        "channel": "1.91.0",
        "host": "x86_64-unknown-linux-gnu",
        "rustc": "rustc 1.91.0 (f8297e351 2025-10-28)",
        "commit": "f8297e351a40c1439a467bbbb6879088047f50b3",
        "profile": "minimal",
        "components": ["clippy"],
        "targets": [],
    }
    assert marker.with_suffix(".toolchain").read_text(encoding="utf-8") == f"{RUST}\n"

    (kept,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert kept.deps == "cargo: kept" and len(tools.calls) == 3

    (deps / "rustup" / "toolchains" / RUST / "bin" / "cargo").unlink()
    (repaired,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert repaired.deps == (
        "cargo: prefetched (was incomplete: "
        f"deps/rustup/toolchains/{RUST}/bin/cargo is missing or changed)"
    )


def test_a_pinned_cargo_source_needs_rustup_on_the_host_before_any_toolchain_runs(
    tmp_path: Path, rust_repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = rust_repo
    tools = _Rustup(monkeypatch, ["cargo"])
    monkeypatch.setattr(_source, "host_rustup_version", lambda: None)
    recipe = _recipe(_build(url, "cargo"))
    with pytest.raises(StateError) as error:
        fetch(recipe, lock=_locked(recipe, first), build=tmp_path)
    message = str(error.value)
    assert (
        "Cargo source build 'tool' needs rustup on the host to install the Rust toolchain its "
        "rust-toolchain.toml pins (1.91.0): there is no rustup on PATH." in message
    )
    assert "https://rustup.rs" in message and "tundravm doctor --for build" in message
    assert tools.calls == [] and not (tmp_path / ".sources" / "deps").exists()


@pytest.mark.parametrize("kind", ["go", "cargo", "dotnet"])
def test_fetch_prefetches_each_toolchains_dependencies_next_to_the_checkout(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    url, first = repo
    tools = _Toolchain(monkeypatch, ["go", "cargo", "dotnet"])
    recipe = _recipe(_build(url, kind))
    out = tmp_path / "out"

    (fetched,) = fetch(recipe, lock=_locked(recipe, first), build=out)

    cache = (out / ".sources" / "deps" / CACHES[kind]).resolve()
    assert fetched.deps == f"{CACHES[kind]}: prefetched"
    assert (cache / "downloaded").is_file()
    *_, (argv, cwd, env) = tools.calls
    expected = {
        "go": ("go", "mod", "download"),
        "cargo": ("cargo", "fetch", "--locked"),
        "dotnet": (
            "dotnet",
            "restore",
            "App.csproj",
            "--runtime",
            "linux-x64",
            "--packages",
            str(cache),
        ),
    }[kind]
    assert argv == expected
    if kind == "go":
        assert env["GOMODCACHE"] == str(cache) and "-mod=mod" in env["GOFLAGS"].split()
        assert env["GOTOOLCHAIN"] == TOOLCHAIN
    if kind == "cargo":
        assert env["CARGO_HOME"] == str(cache)
    assert not cwd.exists()  # the scratch copy is gone
    marker = out / ".sources" / "deps" / f"{source_dir('tool', first, url)}.{CACHES[kind]}.json"
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["build"] == "tool" and recorded["spec"]["toolchain"] == kind

    calls = len(tools.calls)
    (again,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert again.cached and again.deps == f"{CACHES[kind]}: kept"
    assert len(tools.calls) == calls


def _script_build(url: str) -> Build:
    return Build("tool", Git(url, "main"), script="make", install=(Install("tool", "/t"),))


@pytest.mark.parametrize(
    ("make", "present", "code", "deps", "calls", "deps_dir"),
    [
        (_build, ["go"], 1, "failed (go mod: boom)", 2, True),
        (_script_build, [], 0, None, 0, False),
    ],
    ids=["go-fails", "script-build"],
)
def test_a_prefetch_that_cannot_run_records_why(
    tmp_path: Path,
    repo: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[str], Build],
    present: list[str],
    code: int,
    deps: str | None,
    calls: int,
    deps_dir: bool,
) -> None:
    """A failing prefetch is recorded and leaves no marker (the build downloads online), and
    a script build (or a kernel) has no dependency cache, nor needs a host go; the fetch
    succeeds either way."""
    url, first = repo
    tools = _Toolchain(monkeypatch, present, code=code)
    recipe = _recipe(make(url))
    (fetched,) = fetch(recipe, lock=_locked(recipe, first), build=tmp_path)
    assert fetched.deps == deps
    assert len(tools.calls) == calls
    assert (tmp_path / ".sources" / "deps").exists() == deps_dir
    assert list((tmp_path / ".sources" / "deps").glob("*.json")) == []


@pytest.mark.parametrize(
    ("subdir", "refused"),
    [("tools", None), ("missing", "no directory missing"), ("out", "no directory out")],
    ids=["a-module", "absent", "a-link-out"],
)
def test_a_subdirectory_builds_inside_the_checkout_with_its_go_mod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subdir: str, refused: str | None
) -> None:
    """The prefetch runs in the subdirectory with the toolchain its go.mod names (the root's
    is older), and a subdirectory the commit lacks or that leaves the checkout is refused."""
    path = tmp_path / "mono"
    (path / "tools").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "go.mod").write_text("module example.com/root\n\ngo 1.21\n", encoding="utf-8")
    (path / "tools" / "go.mod").write_text("module example.com/t\n\ngo 1.22\n", encoding="utf-8")
    (path / "out").symlink_to(tmp_path)
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    tools = _Toolchain(monkeypatch, ["go"])
    build = Build(
        "tool",
        Git(path.as_uri(), "main", subdir=subdir),
        using=RECIPES["go"],
        install=(Install("tool", "/t"),),
    )
    recipe = _recipe(build)
    locked = _locked(recipe, _git("rev-parse", "HEAD", cwd=path))
    if refused is not None:
        with pytest.raises(SourceError, match=refused):
            fetch(recipe, lock=locked, build=tmp_path / "out-dir")
        return
    (fetched,) = fetch(recipe, lock=locked, build=tmp_path / "out-dir")
    assert fetched.deps == "go: prefetched"
    *_, (argv, cwd, env) = tools.calls
    assert argv == ("go", "mod", "download") and cwd.name == "tools"
    assert env["GOTOOLCHAIN"] == TOOLCHAIN


MODULE = "example.com/Dep"
MODULE_DIR = "example.com/!dep@v1.2.0"
"""The module ``go.sum`` names, and its directory in the module cache (case-encoded)."""


@pytest.fixture
def go_repo(tmp_path: Path) -> tuple[str, str]:
    """A Go module with one dependency in ``go.sum``: ``(url, commit)``."""
    path = tmp_path / "go-upstream"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "go.mod").write_text("module example.com/tool\n\ngo 1.22\n", encoding="utf-8")
    (path / "go.sum").write_text(
        f"{MODULE} v1.2.0 h1:zip=\n{MODULE} v1.2.0/go.mod h1:mod=\n", encoding="utf-8"
    )
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    return path.as_uri(), _git("rev-parse", "HEAD", cwd=path)


class _GoModules(_Toolchain):
    """A fake ``go mod download`` that fills the module cache from ``go.sum``."""

    def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), cwd, dict(env)))
        if _toolchain_download(argv, env):
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        cache = Path(env["GOMODCACHE"])
        assert (cwd / "go.sum").is_file()
        (cache / MODULE_DIR).mkdir(parents=True, exist_ok=True)
        (cache / MODULE_DIR / "dep.go").write_text("package dep\n", encoding="utf-8")
        download = cache / "cache/download/example.com/!dep/@v"
        download.mkdir(parents=True, exist_ok=True)
        (download / "v1.2.0.mod").write_text("module example.com/Dep\n", encoding="utf-8")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def test_fetch_keeps_the_deps_only_while_their_manifest_is_complete(
    tmp_path: Path, go_repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = go_repo
    tools = _GoModules(monkeypatch, ["go"])
    recipe = _recipe(_build(url, "go"))
    out = tmp_path / "out"
    (fetched,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert fetched.deps == "go: prefetched"
    deps = out / ".sources" / "deps"
    (marker,) = deps.glob("*.go.json")
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    contents = recorded["contents"]
    assert sorted(contents) == [
        "cache/download/example.com/!dep/@v/v1.2.0.mod",
        TOOLCHAIN_ZIPHASH,
        MODULE_DIR,
        TOOLCHAIN_MODULE,
    ]
    assert contents[MODULE_DIR] == contents[TOOLCHAIN_MODULE] == "dir"
    assert recorded["toolchain"] == {
        "name": TOOLCHAIN,
        "module": TOOLCHAIN_MODULE,
        "sum": "h1:toolchain=",
    }
    toolchain_file = marker.with_name(marker.name.removesuffix(".json") + ".toolchain")
    assert toolchain_file.read_text(encoding="utf-8") == f"{TOOLCHAIN}\n"

    (kept,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert kept.deps == "go: kept" and len(tools.calls) == 2

    shutil.rmtree(deps / "go" / MODULE_DIR)
    (repaired,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert repaired.deps == (
        f"go: prefetched (was incomplete: deps/go/{MODULE_DIR} is missing or changed)"
    )
    assert len(tools.calls) == 4 and (deps / "go" / MODULE_DIR / "dep.go").is_file()


def test_a_toolchain_the_host_cannot_fetch_names_the_release_and_the_fix(
    tmp_path: Path, go_repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch fails on the host, with the release, its module and what to do, never as a
    build in the sandbox; nothing is recorded, so the next fetch tries again."""
    url, first = go_repo

    def offline(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        lookup = "dial tcp: lookup proxy.golang.org: no such host"
        return subprocess.CompletedProcess(list(argv), 1, "", f"go: download {TOOLCHAIN}: {lookup}")

    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(_source, "host_go_version", lambda: HOST_GO)
    monkeypatch.setattr(_source, "run_tool", offline)
    recipe = _recipe(_build(url, "go"))
    with pytest.raises(SourceError, match=f"Cannot fetch {TOOLCHAIN}, the Go toolchain") as failed:
        fetch(recipe, lock=_locked(recipe, first), build=tmp_path)
    assert failed.value.code == ErrorCode.SOURCE
    assert "no such host" in failed.value.reason
    assert failed.value.context == {"toolchain": TOOLCHAIN, "module": TOOLCHAIN_MODULE}
    assert "proxy" in str(failed.value.hint) and "mkosi" not in str(failed.value)
    assert list((tmp_path / ".sources" / "deps").glob("*.go.*")) == []


def test_a_marker_without_a_manifest_is_prefetched_again(
    tmp_path: Path, go_repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = go_repo
    tools = _GoModules(monkeypatch, ["go"])
    recipe = _recipe(_build(url, "go"))
    out = tmp_path / "out"
    fetch(recipe, lock=_locked(recipe, first), build=out)
    (marker,) = (out / ".sources" / "deps").glob("*.go.json")
    record = json.loads(marker.read_text(encoding="utf-8"))
    del record["contents"]
    marker.write_text(json.dumps(record), encoding="utf-8")
    (again,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert again.deps == "go: prefetched (was incomplete: the marker has no content manifest)"
    assert len(tools.calls) == 4


@pytest.mark.parametrize("kind", ["cargo", "dotnet"])
def test_cargo_and_nuget_manifests_come_from_the_lock_and_the_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "upstream"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "Cargo.toml").write_text('[package]\nname = "tool"\n', encoding="utf-8")
    (path / "Cargo.lock").write_text(
        'version = 3\n\n[[package]]\nname = "tool"\nversion = "0.1.0"\n\n'
        '[[package]]\nname = "serde"\nversion = "1.0.0"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n',
        encoding="utf-8",
    )
    (path / "App.csproj").write_text("<Project />\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    url, first = path.as_uri(), _git("rev-parse", "HEAD", cwd=path)
    crate = "registry/cache/index.crates.io-6f17d22bba15001f/serde-1.0.0.crate"

    def run(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if kind == "cargo":
            target = Path(env["CARGO_HOME"]) / crate
            target.parent.mkdir(parents=True)
            target.write_bytes(b"crate")
        else:
            (Path(argv[-1]) / "newtonsoft.json" / "13.0.1").mkdir(parents=True)
            (Path(argv[-1]) / "newtonsoft.json" / "13.0.1" / ".nupkg.metadata").write_text("{}")
            (cwd / "obj").mkdir()
            assets = {
                "libraries": {
                    "Newtonsoft.Json/13.0.1": {"type": "package", "path": "newtonsoft.json/13.0.1"}
                }
            }
            (cwd / "obj" / "project.assets.json").write_text(json.dumps(assets))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    _Toolchain(monkeypatch, ["cargo", "dotnet"])
    monkeypatch.setattr(_source, "run_tool", run)
    recipe = _recipe(_build(url, kind))
    out = tmp_path / "out"
    fetch(recipe, lock=_locked(recipe, first), build=out)
    (marker,) = (out / ".sources" / "deps").glob(f"*.{CACHES[kind]}.json")
    contents = json.loads(marker.read_text(encoding="utf-8"))["contents"]
    if kind == "cargo":
        assert contents == {crate: f"5 {hashlib.sha256(b'crate').hexdigest()}"}
        (out / ".sources" / "deps" / "cargo" / crate).write_bytes(b"other")
    else:
        assert contents == {"newtonsoft.json/13.0.1": "dir"}
        shutil.rmtree(out / ".sources" / "deps" / "nuget" / "newtonsoft.json")
    _Toolchain(monkeypatch, [])
    (fetched,) = fetch(recipe, lock=_locked(recipe, first), build=out)
    assert fetched.deps is not None and fetched.deps.startswith(f"{CACHES[kind]}: incomplete (")


# ── bake --offline ───────────────────────────────────────────────────


def test_offline_bake_refuses_a_build_without_prefetched_deps(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    _Toolchain(monkeypatch, ["go"], code=1)
    recipe = _recipe(_build(url, "go"))
    out = tmp_path / "out"
    fetch(recipe, lock=_locked(recipe, first), build=out)
    backend = _Recording()
    with pytest.raises(StateError, match="'tool' has no prefetched go dependencies") as missing:
        bake_image(
            lower(recipe),
            None,
            locked=_locked(recipe, first),
            backend=backend,
            build=out,
            offline=True,
        )
    assert missing.value.code == ErrorCode.STATE
    assert "tundravm build" in str(missing.value.hint) and "`go`" in str(missing.value.hint)
    assert backend.requests == []


def test_offline_bake_names_the_missing_cache_entry(
    tmp_path: Path, go_repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = go_repo
    _GoModules(monkeypatch, ["go"])
    recipe = _recipe(_build(url, "go"))
    out = tmp_path / "out"
    fetch(recipe, lock=_locked(recipe, first), build=out)
    shutil.rmtree(out / ".sources" / "deps" / "go" / MODULE_DIR)
    backend = _Recording()
    with pytest.raises(StateError, match="incomplete go dependency cache") as incomplete:
        bake_image(
            lower(recipe),
            None,
            locked=_locked(recipe, first),
            backend=backend,
            build=out,
            offline=True,
            fetch=False,
        )
    assert f"deps/go/{MODULE_DIR} is missing or changed" in str(incomplete.value)
    assert "tundravm build --clean-cache" in str(incomplete.value.hint)
    assert backend.requests == []


@pytest.mark.parametrize("how", ["flag", "policy"])
def test_offline_bake_after_fetch_builds_without_network(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    url, first = repo
    _Toolchain(monkeypatch, ["go"])
    policy = Policy(network_mode="offline") if how == "policy" else None
    recipe = _recipe(_build(url, "go"))
    out = tmp_path / "out"
    fetch(recipe, lock=_locked(recipe, first), build=out)
    backend = _Recording()
    bake_image(
        lower(_recipe(_build(url, "go"), policy=policy)),
        None,
        locked=_locked(recipe, first),
        backend=backend,
        build=out,
        offline=how == "flag",
    )
    (request,) = backend.requests
    assert request.sources_dir == out / ".sources"
    # the tree owns the network: WithNetwork=no (Mkosi(build_network=False), the default)
    assert "\nWithNetwork=no\n" in (out / "mkosi" / "mkosi.conf").read_text(encoding="utf-8")
    assert not any(arg.startswith("--with-network") for arg in request.mkosi_args)
    hooks = (out / "mkosi" / "payload" / "mkosi.build.d" / "50-build.sh").read_text(
        encoding="utf-8"
    )
    assert '"$SRCDIR/tundravm-sources/deps/go"' in hooks
    assert "export GOMODCACHE=/build/.tundravm-deps/go GOFLAGS=-mod=mod" in hooks
    assert "export GOPROXY=file:///build/.tundravm-deps/go/cache/download GOSUMDB=off" in hooks


def test_online_bake_keeps_the_trees_network(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    _Toolchain(monkeypatch, ["go"])
    recipe = replace(_recipe(_build(url, "go")), mkosi=Mkosi(build_network=True))
    backend = _Recording()
    bake_image(lower(recipe), None, locked=_locked(recipe, first), backend=backend, build=tmp_path)
    assert "\nWithNetwork=yes\n" in (tmp_path / "mkosi" / "mkosi.conf").read_text(encoding="utf-8")
    assert backend.requests[0].mkosi_args == ()


def test_an_offline_bake_refuses_a_tree_with_network(tmp_path: Path, repo: tuple[str, str]) -> None:
    url, first = repo
    recipe = replace(
        _recipe(_build(url, "go"), policy=Policy(network_mode="offline")),
        mkosi=Mkosi(build_network=True),
    )
    backend = _Recording()
    locked = _locked(replace(recipe, policy=None), first)
    with pytest.raises(PolicyError, match="offline build cannot build a tree"):
        bake_image(lower(recipe), None, locked=locked, backend=backend, build=tmp_path, fetch=False)
    assert backend.requests == []


def test_backends_pass_no_network_setting(tmp_path: Path) -> None:
    (tmp_path / "build" / "mkosi").mkdir(parents=True)
    request = BakeRequest(
        profile="default", build_dir=tmp_path / "build", emit_dir=tmp_path / "build" / "mkosi"
    )
    local = LocalLinuxBackend(privilege="none").arguments(request)
    lima = LimaMkosiBackend(cpus=2, memory="4GiB", disk="40GiB")._mkosi_args(request)
    assert not any(arg.startswith("--with-network") for arg in (*local, *lima))


# ── EfiStub ──────────────────────────────────────────────────────────


def _stub(archive: str) -> EfiStub:
    return EfiStub(snapshot=archive, version=EFI_VERSION)


def test_efi_stub_package_is_locked_fetched_and_installed_from_the_mount(
    tmp_path: Path, snapshot: tuple[str, str]
) -> None:
    archive, digest = snapshot
    recipe = _recipe(_stub(archive))
    deb_url = f"{archive}/pool/main/s/systemd/systemd-boot-efi_{EFI_VERSION}_amd64.deb"

    locked = lock(recipe, resolver=_source._download_digest)  # type: ignore[arg-type]
    (entry,) = locked.lockfile.fetches
    assert (entry.name, entry.kind, entry.source, entry.digest) == (
        "efi-stub",
        "http",
        deb_url,
        digest,
    )

    (fetched,) = fetch(recipe, lock=locked, build=tmp_path)
    directory = source_dir("efi-stub", digest, deb_url, http=True)
    deb = tmp_path / ".sources" / directory / f"systemd-boot-efi_{EFI_VERSION}_amd64.deb"
    assert fetched.name == "efi-stub" and fetched.deps is None
    assert deb.read_bytes() == DEB_BYTES

    tree = compile_image(lower(recipe), None, locked=locked)
    (postinst,) = (e for e in tree.entries if e.path.endswith("-postinst.sh"))
    script = (postinst.content or b"").decode()
    assert "curl" not in script
    assert f'[ ! -f "$SRCDIR/tundravm-sources/{directory}/.tundravm-complete" ]' in script
    mounted = f"tundravm-sources/{directory}/systemd-boot-efi_{EFI_VERSION}_amd64.deb"
    assert f'echo "{digest}  $SRCDIR/{mounted}" | sha256sum -c --quiet -' in script
    assert f'mkosi-chroot dpkg -i "$CHROOT_SRCDIR/{mounted}"' in script

    backend = _Recording()
    bake_image(lower(recipe), None, locked=locked, backend=backend, build=tmp_path)
    assert backend.requests[0].sources_dir == tmp_path / ".sources"


def test_unpinned_efi_stub_still_compiles_to_the_download(snapshot: tuple[str, str]) -> None:
    archive, _ = snapshot
    tree = compile(_recipe(_stub(archive)))
    (postinst,) = (e for e in tree.entries if e.path.endswith("-postinst.sh"))
    assert (
        'curl -sSfL -o "$BUILDROOT/systemd-boot-efi.deb" "$DEB_URL"'
        in (postinst.content or b"").decode()
    )


def test_efi_stub_drifts_until_locked(
    snapshot: tuple[str, str],
) -> None:
    archive, digest = snapshot
    recipe = _recipe(_stub(archive))
    old = lock(_recipe())
    stale = lock(recipe, resolver=lambda source: digest)
    drift = {(d.code, d.subject) for d in lock_status(recipe, Lock.of(old.lockfile))}
    assert ("lock-added", "sources.efi-stub") in drift
    assert lock_status(recipe, stale) == ()


def test_unpinned_efi_stub_is_reported_until_locked() -> None:
    recipe = _recipe(_stub("20251113T083151Z"))
    found = [d for d in lint(recipe) if d.code == "source-unpinned"]
    assert [(d.severity, d.subject) for d in found] == [("warning", "sources.efi-stub")]
    assert "systemd-boot-efi_257.8-1~deb13u1_amd64.deb" in found[0].message
    strict = _recipe(_stub("20251113T083151Z"), policy=Policy(mutable_ref_policy="error"))
    assert [d.severity for d in lint(strict) if d.code == "source-unpinned"] == ["error"]
    locked = lock(recipe, resolver=lambda source: "b" * 64)
    assert not [d for d in lint(recipe, lock=locked) if d.code == "source-unpinned"]
    report = check_report(recipe, None, variants=None)
    (unpinned,) = (d for d in report if d.code == "source-unpinned")
    assert unpinned.hint is not None and "`tundravm lock`" in unpinned.hint


def test_a_build_named_efi_stub_collides_with_the_package(
    repo: tuple[str, str], snapshot: tuple[str, str]
) -> None:
    url, _ = repo
    archive, _ = snapshot
    build = Build("efi-stub", Git(url, "main"), script="make", install=(Install("t", "/t"),))
    with pytest.raises(Exception, match="takes the name of the EFI stub package"):
        lower(_recipe(build, _stub(archive))).lock_sources()
