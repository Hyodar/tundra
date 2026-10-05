"""Hermetic bakes: dependency prefetch, the ``EfiStub`` package as a source, ``bake --offline``."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from tests.helpers import source_dir, write_recipe_file
from tundravm import _source
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend
from tundravm.backends.base import MountSpec, Requirement
from tundravm.cli import EXIT_OK, main
from tundravm.declarative import (
    Build,
    Cargo,
    Dotnet,
    Fragment,
    Git,
    Go,
    Install,
    Lock,
    Mkosi,
    Package,
    Policy,
    Recipe,
    compile,
    lock,
    lock_status,
    lower,
)
from tundravm.declarative.lifecycle import bake_image, compile_image, fetch
from tundravm.declarative.utils import EfiStub
from tundravm.errors import ErrorCode, PolicyError, StateError
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult

pytestmark = pytest.mark.usefixtures("isolated_cwd")

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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/archive",
            hashlib.sha256(DEB_BYTES).hexdigest(),
        )
    finally:
        server.shutdown()
        server.server_close()


class _Toolchain:
    """Fake host toolchains: ``which`` finds *present*; ``run`` records and fills the cache."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, present: Sequence[str], code: int = 0):
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.code = code
        found = set(present)
        monkeypatch.setattr(
            shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}" if name in found else None
        )
        monkeypatch.setattr(_source, "run_tool", self.run)

    def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), cwd, dict(env)))
        assert cwd.is_dir() and not (cwd / ".git").exists()  # a scratch copy, not the checkout
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


def _recipe(*items: object, dialect: str = "current", policy: Policy | None = None) -> Recipe:
    return Recipe(
        "tool",
        Fragment("tool", items=(Package("linux-image-amd64"), *items)),  # type: ignore[arg-type]
        mkosi=Mkosi(dialect=dialect),  # type: ignore[arg-type]
        policy=policy,
    )


def _build(url: str, kind: str = "go") -> Build:
    return Build("tool", Git(url, "main"), recipe=RECIPES[kind], install=(Install("tool", "/t"),))


def _locked(recipe: Recipe, pin: str, deb: str | None = None) -> Lock:
    return lock(recipe, resolver=lambda s: deb if deb and s.kind == "http" else pin)


# ── dependency prefetch ──────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["go", "cargo", "dotnet"])
def test_fetch_prefetches_each_toolchains_dependencies_next_to_the_checkout(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    url, first = repo
    tools = _Toolchain(monkeypatch, ["go", "cargo", "dotnet"])
    recipe = _recipe(_build(url, kind))
    out = tmp_path / "out"

    (fetched,) = fetch(recipe, lock=_locked(recipe, first), out=out)

    cache = (out / ".sources" / "deps" / CACHES[kind]).resolve()
    assert fetched.deps == f"{CACHES[kind]}: prefetched"
    assert (cache / "downloaded").is_file()
    ((argv, cwd, env),) = tools.calls
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
    if kind == "cargo":
        assert env["CARGO_HOME"] == str(cache)
    assert not cwd.exists()  # the scratch copy is gone
    marker = out / ".sources" / "deps" / f"{source_dir('tool', first, url)}.{CACHES[kind]}.json"
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["build"] == "tool" and recorded["spec"]["toolchain"] == kind

    (again,) = fetch(recipe, lock=_locked(recipe, first), out=out)
    assert again.cached and again.deps == f"{CACHES[kind]}: kept"
    assert len(tools.calls) == 1


def test_fetch_without_the_host_toolchain_skips_the_deps_and_succeeds(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    tools = _Toolchain(monkeypatch, [])
    recipe = _recipe(_build(url, "go"))
    (fetched,) = fetch(recipe, lock=_locked(recipe, first), out=tmp_path)
    assert fetched.deps == "skipped (no host go)"
    assert tools.calls == []
    assert not (tmp_path / ".sources" / "deps").exists()


def test_a_failing_prefetch_is_recorded_and_the_build_downloads_online(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    _Toolchain(monkeypatch, ["go"], code=1)
    recipe = _recipe(_build(url, "go"))
    (fetched,) = fetch(recipe, lock=_locked(recipe, first), out=tmp_path)
    assert fetched.deps == "failed (go mod: boom)"
    assert list((tmp_path / ".sources" / "deps").glob("*.json")) == []


def test_script_builds_and_kernels_have_no_dependency_cache(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    tools = _Toolchain(monkeypatch, ["go"])
    build = Build("tool", Git(url, "main"), script="make", install=(Install("tool", "/t"),))
    recipe = _recipe(build)
    (fetched,) = fetch(recipe, lock=_locked(recipe, first), out=tmp_path)
    assert fetched.deps is None and tools.calls == []


def test_cli_fetch_lists_the_dependency_outcome(
    tmp_path: Path,
    repo: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, first = repo
    _Toolchain(monkeypatch, [])
    path = write_recipe_file(
        tmp_path,
        "from tundravm.declarative import Build, Fragment, Git, Go, Install, Package, Recipe\n"
        "recipe = Recipe('tool', Fragment('tool', items=(Package('linux-image-amd64'), "
        f"Build('tool', Git({url!r}, {first!r}), recipe=Go(output='tool'), "
        "install=(Install('tool', '/t'),)))))\n",
    )
    assert main(["fetch", str(path), "--out", str(tmp_path / "out")]) == EXIT_OK
    captured = capsys.readouterr()
    assert "deps: skipped (no host go)" in captured.out
    assert "tool: deps: skipped (no host go)" in captured.err


# ── bake --offline ───────────────────────────────────────────────────


def test_offline_bake_refuses_a_build_without_prefetched_deps(
    tmp_path: Path, repo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, first = repo
    _Toolchain(monkeypatch, [])
    recipe = _recipe(_build(url, "go"))
    out = tmp_path / "out"
    fetch(recipe, lock=_locked(recipe, first), out=out)
    backend = _Recording()
    with pytest.raises(StateError, match="'tool' has no prefetched go dependencies") as missing:
        bake_image(
            lower(recipe),
            None,
            locked=_locked(recipe, first),
            backend=backend,
            out=out,
            offline=True,
        )
    assert missing.value.code == ErrorCode.STATE
    assert "tundravm fetch" in str(missing.value.hint) and "`go`" in str(missing.value.hint)
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
    fetch(recipe, lock=_locked(recipe, first), out=out)
    backend = _Recording()
    bake_image(
        lower(_recipe(_build(url, "go"), policy=policy)),
        None,
        locked=_locked(recipe, first),
        backend=backend,
        out=out,
        offline=how == "flag",
    )
    (request,) = backend.requests
    assert request.network is False and request.sources_dir == out / ".sources"
    hooks = (out / "mkosi" / "default" / "scripts" / "04-build.sh").read_text(encoding="utf-8")
    assert '"$SRCDIR/tundravm-sources/deps/go"' in hooks
    assert "export GOMODCACHE=/build/.tundravm-deps/go GOFLAGS=-mod=mod" in hooks
    assert '[ "$WITH_NETWORK" != 0 ] || export GOPROXY=off' in hooks


def test_online_bake_keeps_the_network(tmp_path: Path, repo: tuple[str, str]) -> None:
    url, first = repo
    recipe = _recipe(_build(url, "go"))
    backend = _Recording()
    bake_image(lower(recipe), None, locked=_locked(recipe, first), backend=backend, out=tmp_path)
    assert backend.requests[0].network is True


def test_offline_bake_refuses_nethermind_v1_source_builds(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, first = repo
    recipe = _recipe(_build(url, "go"), dialect="nethermind-v1")
    backend = _Recording()
    with pytest.raises(PolicyError, match="fetch their sources in the sandbox"):
        bake_image(
            lower(recipe),
            None,
            locked=_locked(recipe, first),
            backend=backend,
            out=tmp_path,
            offline=True,
        )
    assert backend.requests == []


def test_backends_pass_with_network_no_for_an_offline_bake(tmp_path: Path) -> None:
    variant = tmp_path / "build" / "mkosi" / "default"
    variant.mkdir(parents=True)
    (variant / "mkosi.conf").write_text("[Build]\nWithNetwork=true\n", encoding="utf-8")
    request = BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "build" / "mkosi",
        network=False,
    )
    assert "--with-network=no" in LocalLinuxBackend(privilege="none").command(request)
    output = tmp_path / "build" / "default" / "output"
    assert "--with-network=no" in NixMkosiBackend()._build_mkosi_args(request, output)
    lima = LimaMkosiBackend(cpus=2, memory="4GiB", disk="40GiB")._build_mkosi_command(request)
    assert " --with-network=no " in lima
    online = BakeRequest(profile="default", build_dir=request.build_dir, emit_dir=request.emit_dir)
    assert "--with-network=no" not in LocalLinuxBackend(privilege="none").command(online)


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

    (fetched,) = fetch(recipe, lock=locked, out=tmp_path)
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
    bake_image(lower(recipe), None, locked=locked, backend=backend, out=tmp_path)
    assert backend.requests[0].sources_dir == tmp_path / ".sources"


def test_unpinned_efi_stub_still_compiles_to_the_download(snapshot: tuple[str, str]) -> None:
    archive, _ = snapshot
    tree = compile(_recipe(_stub(archive)))
    (postinst,) = (e for e in tree.entries if e.path.endswith("-postinst.sh"))
    assert (
        'curl -sSfL -o "$BUILDROOT/systemd-boot-efi.deb" "$DEB_URL"'
        in (postinst.content or b"").decode()
    )


def test_efi_stub_drifts_until_locked_and_stays_out_of_nethermind_v1(
    snapshot: tuple[str, str],
) -> None:
    archive, digest = snapshot
    recipe = _recipe(_stub(archive))
    old = lock(_recipe())
    stale = lock(recipe, resolver=lambda source: digest)
    drift = {(d.code, d.subject) for d in lock_status(recipe, Lock.of(old.lockfile))}
    assert ("lock-added", "sources.efi-stub") in drift
    assert lock_status(recipe, stale) == ()
    assert lower(_recipe(_stub(archive), dialect="nethermind-v1")).lock_sources() == {}


def test_a_build_named_efi_stub_collides_with_the_package(
    repo: tuple[str, str], snapshot: tuple[str, str]
) -> None:
    url, _ = repo
    archive, _ = snapshot
    build = Build("efi-stub", Git(url, "main"), script="make", install=(Install("t", "/t"),))
    with pytest.raises(Exception, match="takes the name of the EfiStub package"):
        lower(_recipe(build, _stub(archive))).lock_sources()
