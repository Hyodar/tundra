"""Kernel sources join the host-side fetch: lock, fetch, the emitted script, bake, inspect, lint."""

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

from tundravm._source import GitSource
from tundravm.backends.base import MountSpec, Requirement
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Http,
    Install,
    Kernel,
    Lock,
    Mkosi,
    Recipe,
    Variant,
    compile,
    lint,
    lock,
    lock_status,
    lower,
)
from tundravm.declarative.lifecycle import FetchedSource, bake_image, fetch, write_lock
from tundravm.errors import ErrorCode, StateError, ValidationError
from tundravm.explain import describe, render
from tundravm.models import BakeRequest, BakeResult, ProfileBuildResult

pytestmark = pytest.mark.usefixtures("isolated_cwd")

SCRIPT = "default/scripts/04-build.sh"


def _git(*args: str, cwd: Path) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@pytest.fixture
def linux(tmp_path: Path) -> tuple[str, str, str]:
    """A tiny kernel repository: ``(file:// url, commit of annotated tag v6.1, tag object)``.

    ``main`` moves one commit past the tag.
    """
    path = tmp_path / "linux"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "Makefile").write_text("all:\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "v6.1", cwd=path)
    _git("tag", "-a", "v6.1", "-m", "Linux 6.1", cwd=path)
    commit = _git("rev-parse", "HEAD", cwd=path)
    tag = _git("rev-parse", "v6.1", cwd=path)
    (path / "later.c").write_text("int later;\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "later", cwd=path)
    return path.as_uri(), commit, tag


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "kernel.config"
    path.write_text("CONFIG_TDX_GUEST_DRIVER=y\n", encoding="utf-8")
    return path


@pytest.fixture
def tarball(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """``(url, sha256)`` of a ``linux-6.1.tar.gz`` a local HTTP server serves."""
    root = tmp_path / "www"
    root.mkdir()
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        payload = b"all:\n"
        info = tarfile.TarInfo("linux-6.1/Makefile")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    (root / "linux-6.1.tar.gz").write_bytes(data.getvalue())
    server = HTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/linux-6.1.tar.gz"
        yield url, hashlib.sha256(data.getvalue()).hexdigest()
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _recipe(
    kernel: Kernel,
    *,
    dialect: str = "current",
    variants: tuple[Variant, ...] = (Variant("default"),),
    builds: tuple[Build, ...] = (),
) -> Recipe:
    return Recipe(
        "k",
        Fragment("k", items=(kernel, *builds)),
        variants=variants,
        mkosi=Mkosi(dialect=dialect),  # type: ignore[arg-type]
    )


def _script(recipe: Recipe, out: Path, locked: Lock | None = None) -> str:
    compile(recipe, lock=locked).write(out)
    return (out / SCRIPT).read_text(encoding="utf-8")


# ── lock ─────────────────────────────────────────────────────────────


def test_lock_pins_the_kernel_ref(config: Path) -> None:
    calls: list[object] = []

    def resolver(source: object) -> str:
        calls.append(source)
        return "c" * 40

    locked = lock(
        _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config)),
        resolver=resolver,
    )
    (pin,) = locked.lockfile.fetches
    assert (pin.name, pin.kind, pin.source, pin.ref, pin.digest) == (
        "kernel",
        "git",
        "https://k.example/linux",
        "v6.1",
        "c" * 40,
    )
    assert calls == [GitSource("https://k.example/linux", "v6.1")]


def test_kernels_without_a_config_or_under_nethermind_v1_are_not_locked(config: Path) -> None:
    kernel = Kernel("6.1", Git("https://k.example/linux", "v6.1"))
    assert lock(_recipe(kernel), resolver=lambda s: "c" * 40).lockfile.fetches == []
    built = Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config)
    historical = _recipe(built, dialect="nethermind-v1")
    assert lock(historical, resolver=lambda s: "c" * 40).lockfile.fetches == []


def test_a_variant_whose_kernel_source_differs_gets_its_own_pin(config: Path) -> None:
    shared = Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config)
    other = Kernel("6.2", Git("https://k.example/linux", "v6.2"), config=config)
    same_source = Kernel(
        "6.1", Git("https://k.example/linux", "v6.1"), cmdline="quiet", config=config
    )
    variants = (
        Variant("default"),
        Variant("azure", replace=(other,)),
        Variant("gcp", replace=(same_source,)),
    )
    recipe = _recipe(shared, variants=variants)
    locked = lock(recipe, resolver=lambda s: "c" * 40)
    assert lock_status(recipe, locked, variants=("gcp",)) == ()  # kernel-azure is not "removed"
    assert [(f.name, f.ref) for f in locked.lockfile.fetches] == [
        ("kernel", "v6.1"),
        ("kernel-azure", "v6.2"),
    ]


def test_a_build_named_kernel_collides_with_the_kernels_pin(config: Path) -> None:
    build = Build(
        "kernel", Git("https://x.example/t", "main"), script="make", install=(Install("t", "/t"),)
    )
    recipe = _recipe(
        Kernel("6.1", Git("https://k.example/l", "v6.1"), config=config), builds=(build,)
    )
    with pytest.raises(ValidationError, match="'kernel' takes the name of the kernel's source"):
        lock(recipe, resolver=lambda s: "c" * 40)


def test_drift_reports_a_moved_or_changed_kernel_ref(config: Path) -> None:
    recipe = _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config))
    locked = lock(recipe, resolver=lambda s: "c" * 40)
    assert lock_status(recipe, locked) == ()
    moved = lock_status(recipe, locked, resolver=lambda s: "d" * 40)
    assert [(d.code, d.subject, d.message) for d in moved] == [
        (
            "lock-changed",
            "sources.kernel",
            "sources.kernel changed since the lock: ccccccc -> ddddddd",
        )
    ]
    bumped = _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1.1"), config=config))
    assert [(d.code, d.subject) for d in lock_status(bumped, locked)] == [
        ("lock-changed", "sources.kernel")
    ]
    no_config = _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1")))
    unlocked = lock(no_config, resolver=lambda s: "c" * 40)
    assert [(d.code, d.message) for d in lock_status(recipe, unlocked)] == [
        ("lock-added", "source kernel is not pinned")
    ]


# ── fetch ────────────────────────────────────────────────────────────


def test_fetch_checks_out_a_tagged_kernel(
    tmp_path: Path, linux: tuple[str, str, str], config: Path
) -> None:
    url, commit, tag = linux
    recipe = _recipe(Kernel("6.1", Git(url, "v6.1"), config=config))
    (fetched,) = fetch(recipe, locked=None, out=tmp_path / "out", resolver=lambda s: tag)
    checkout = tmp_path / "out" / ".sources" / f"kernel-{tag[:12]}"
    assert fetched == FetchedSource("kernel", "git", url, tag, checkout, ref="v6.1")
    assert _git("rev-parse", "HEAD", cwd=checkout) == commit
    assert (checkout / "Makefile").is_file() and not (checkout / "later.c").exists()
    (again,) = fetch(recipe, locked=lock(recipe, resolver=lambda s: tag), out=tmp_path / "out")
    assert again.cached


def test_fetch_checks_out_a_kernel_commit_without_resolving(
    tmp_path: Path, linux: tuple[str, str, str], config: Path
) -> None:
    url, commit, _ = linux
    recipe = _recipe(Kernel("6.1", Git(url, commit), config=config))
    (fetched,) = fetch(recipe, locked=None, out=tmp_path)  # an inline pin: no resolver runs
    assert fetched.path == tmp_path / ".sources" / f"kernel-{commit[:12]}"
    assert _git("rev-parse", "HEAD", cwd=fetched.path) == commit


def test_fetch_downloads_and_unpacks_a_kernel_tarball(
    tmp_path: Path, tarball: tuple[str, str], config: Path
) -> None:
    url, digest = tarball
    recipe = _recipe(Kernel("6.1", Http(url, sha256=digest), config=config))
    (fetched,) = fetch(recipe, locked=None, out=tmp_path)
    assert (fetched.name, fetched.kind, fetched.pin) == ("kernel", "http", digest)
    assert (fetched.path / "Makefile").read_text(encoding="utf-8") == "all:\n"
    script = _script(recipe, tmp_path / "tree")
    assert "! -name linux-6.1.tar.gz" in script and "curl" not in script


# ── the emitted script ───────────────────────────────────────────────


def test_the_current_script_copies_the_fetched_kernel(tmp_path: Path, config: Path) -> None:
    recipe = _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config))
    pin = "c" * 40
    script = _script(recipe, tmp_path / "tree", lock(recipe, resolver=lambda s: pin))
    checkout = f'"$SRCDIR/tundravm-sources/kernel-{pin[:12]}"'
    assert f"[ -f {checkout}/.tundravm-complete ] ||" in script
    assert (
        f"find {checkout} -mindepth 1 -maxdepth 1 ! -name .git ! -name .tundravm-complete \\\n"
        '        -exec cp -a --no-preserve=ownership -t "$KERNEL_CACHE/src" {} +\n'
    ) in script
    cache = script.split("\n")[3]
    assert cache.startswith('KERNEL_CACHE="${BUILDDIR:-$BUILDROOT/build}/kernel-6.1-')
    assert cache.endswith(f'-{pin[:12]}"')
    assert "git clone" not in script and "k.example" not in script
    moved = _script(recipe, tmp_path / "moved", lock(recipe, resolver=lambda s: "d" * 40))
    assert moved.split("\n")[3] == cache.replace(pin[:12], "d" * 12)


def test_an_unpinned_current_kernel_script_only_fails(tmp_path: Path, config: Path) -> None:
    recipe = _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config))
    script = _script(recipe, tmp_path / "tree")
    assert (
        "echo 'tundravm: kernel 6.1 is not pinned: run tundravm lock, then tundravm fetch' >&2\n"
    ) in script
    assert "git clone" not in script and "tundravm-sources" not in script


def test_a_subdirectory_kernel_links_its_copy(tmp_path: Path, config: Path) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    kernel = Kernel("6.1", Git("https://k.example/l", commit, subdir="linux"), config=config)
    script = _script(_recipe(kernel), tmp_path / "tree")
    assert '-exec cp -a --no-preserve=ownership -t "$KERNEL_CACHE/repo" {} +' in script
    assert 'ln -s repo/linux "$KERNEL_CACHE/src"' in script


def test_nethermind_v1_keeps_the_sandbox_clone(tmp_path: Path, config: Path) -> None:
    recipe = _recipe(
        Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config),
        dialect="nethermind-v1",
    )
    script = _script(recipe, tmp_path / "tree", lock(recipe, resolver=lambda s: "c" * 40))
    assert 'git clone --depth 1 --branch "v${KERNEL_VERSION}" \\' in script
    assert 'KERNEL_CACHE="${BUILDDIR}/kernel-6.1-' in script
    assert "tundravm-sources" not in script


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


def test_bake_refuses_a_missing_kernel_checkout_then_builds_the_fetched_one(
    tmp_path: Path, linux: tuple[str, str, str], config: Path
) -> None:
    url, _, tag = linux
    recipe = _recipe(Kernel("6.1", Git(url, "v6.1"), config=config))
    locked = lock(recipe, resolver=lambda s: tag)
    backend = _Recording()
    out = tmp_path / "out"
    with pytest.raises(StateError, match="Kernel source 'kernel' is not fetched") as missing:
        bake_image(lower(recipe), None, locked=locked, backend=backend, out=out, fetch=False)
    assert missing.value.code == ErrorCode.STATE
    assert "tundravm fetch" in str(missing.value.hint)
    assert backend.requests == []
    bake_image(lower(recipe), None, locked=locked, backend=backend, out=out)
    (request,) = backend.requests
    assert request.sources_dir == out / ".sources"
    assert (out / ".sources" / f"kernel-{tag[:12]}" / "Makefile").is_file()
    script = (out / "mkosi" / SCRIPT).read_text(encoding="utf-8")
    assert f'"$SRCDIR/tundravm-sources/kernel-{tag[:12]}"' in script


# ── inspect and lint ─────────────────────────────────────────────────


def test_inspect_and_lint_report_the_kernel_pin(tmp_path: Path, config: Path) -> None:
    recipe = _recipe(Kernel("6.1", Git("https://k.example/linux", "v6.1"), config=config))
    info = describe(lower(recipe), profile="default")
    assert info["kernel"]["pinned"] is None  # type: ignore[index]
    assert "Kernel: 6.1  tdx=yes  pinned=-" in render(info)
    (unpinned,) = [d for d in lint(recipe) if d.code == "source-unpinned"]
    assert unpinned.subject == "kernel"
    assert unpinned.message == (
        "kernel 6.1 (git https://k.example/linux @ v6.1) is not pinned in the lockfile"
    )
    write_lock(lock(recipe, resolver=lambda s: "c" * 40), tmp_path / "build" / "tundravm.lock")
    info = describe(lower(recipe), profile="default")
    assert info["kernel"]["pinned"] == "ccccccc"  # type: ignore[index]
    assert "Kernel: 6.1  tdx=yes  pinned=ccccccc" in render(info)
    assert not [d for d in lint(recipe) if d.code == "source-unpinned"]
    historical = _recipe(
        Kernel("6.1", Git("https://k.example/linux", "v6.2"), config=config),
        dialect="nethermind-v1",
    )
    assert not [d for d in lint(historical) if d.code == "source-unpinned"]
