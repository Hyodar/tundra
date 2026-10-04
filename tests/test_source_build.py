"""Source builds: declaration, lockfile pinning, policy, drift, explain and check."""

from __future__ import annotations

import io
import json
import warnings
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.cli import EXIT_OK, main
from tundravm.errors import LockfileError, PolicyError
from tundravm.lockfile import (
    LockedFetch,
    build_lockfile,
    parse_lockfile,
    read_lockfile,
    serialize_lockfile,
    write_lockfile,
)
from tundravm.modules import (
    DiskEncryption,
    DiskSpec,
    KeyGeneration,
    KeySpec,
    SecretDelivery,
    Tdxs,
)
from tundravm.policy import Policy
from tundravm.source import (
    CargoBuild,
    GitSource,
    GoBuild,
    HttpSource,
    Install,
    Source,
    SourceBuild,
)

REPO = "https://example.com/acme/tool.git"
SHA_A = "a" * 40
SHA_B = "b" * 40
QEMU_BASIC = Path(__file__).resolve().parent.parent / "examples" / "qemu_basic.py"
QEMU_BASIC_DIGEST = "571668210b9086615b19fde56a4b86cf0aa65f2ec480b2497a42641739d15c8e"

# The hook Tdxs wrote by hand before source builds existed (default repo and branch).
TDXS_LEGACY_HOOK = (
    'if ! ([ -d "$BUILDDIR/tdxs-2bddc6a617e7-master" ] && '
    '[ "$(ls -A "$BUILDDIR/tdxs-2bddc6a617e7-master" 2>/dev/null)" ]); then '
    "git clone --depth=1 -b master https://github.com/Hyodar/tundra-tools.git "
    '"$BUILDROOT/build/tdxs" && mkosi-chroot bash -c \'cd /build/tdxs && mkdir -p ./build && '
    'go build -trimpath -ldflags "-s -w -buildid=" -o ./build/tdxs ./cmd/tdxs\' && '
    'mkdir -p "$BUILDDIR/tdxs-2bddc6a617e7-master" && install -D -m 0755 '
    '"$BUILDROOT/build/tdxs/build/tdxs" "$BUILDDIR/tdxs-2bddc6a617e7-master"/tdxs; fi && '
    'install -D -m 0755 "$BUILDDIR/tdxs-2bddc6a617e7-master"/tdxs "$DESTDIR/usr/bin/tdxs"'
)


def _legacy_go_hook(name: str, binary: str) -> str:
    """The hook KeyGeneration/DiskEncryption/SecretDelivery wrote by hand (default repo)."""
    key = f"$BUILDDIR/{name}-2bddc6a617e7-master"
    return (
        f'if ! ([ -d "{key}" ] && [ "$(ls -A "{key}" 2>/dev/null)" ]); then '
        "git clone --depth=1 -b master https://github.com/Hyodar/tundra-tools.git "
        f'"$BUILDROOT/build/{name}" && mkosi-chroot bash -c \'cd /build/{name} && '
        'mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" '
        f"-o ./build/{binary} ./cmd/{binary}' && "
        f'mkdir -p "{key}" && install -D -m 0755 "$BUILDROOT/build/{name}/build/{binary}" '
        f'"{key}"/{binary}; fi && install -D -m 0755 "{key}"/{binary} "$DESTDIR/usr/bin/{binary}"'
    )


def _spec(ref: str = "main", **kwargs: object) -> SourceBuild:
    return SourceBuild(
        name="tool",
        source=GitSource(REPO, ref),
        build=GoBuild(package="./cmd/tool", output="tool"),
        install=(Install.artifact("/usr/bin/tool"),),
        **kwargs,  # type: ignore[arg-type]
    )


def _image(tmp_path: Path, spec: SourceBuild | None = None, **kwargs: object) -> Image:
    img = Image(build_dir=tmp_path / "build", **kwargs)  # type: ignore[arg-type]
    img.build_from(spec or _spec())
    return img


def _fixed(digest: str):  # type: ignore[no-untyped-def]
    calls: list[Source] = []

    def resolve(source: Source) -> str:
        calls.append(source)
        return digest

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _build_hooks(img: Image, out: Path) -> str:
    img.compile(out)
    return (out / "default" / "scripts" / "04-build.sh").read_text()


# ── Declaration and payload ─────────────────────────────────────────


def test_source_build_records_spec_packages_and_hook(tmp_path: Path) -> None:
    img = _image(tmp_path)
    profile = img.state.profiles["default"]
    assert profile.source_builds == {"tool": _spec()}
    assert {"git", "golang"} <= profile.build_packages
    hooks = [h.command.argv[0] for h in profile.hooks if h.phase == "build"]
    assert hooks == [_spec().render()]
    assert hooks[0].startswith("# unpinned: main\n")


def test_payload_has_source_builds_only_when_declared(tmp_path: Path) -> None:
    bare = Image(build_dir=tmp_path / "build")
    payload = bare._recipe_payload(profile_names=("default",))
    assert "source_builds" not in payload["profiles"]["default"]  # type: ignore[index]
    img = _image(tmp_path)
    profiles = img._recipe_payload(profile_names=("default",))["profiles"]
    entry = profiles["default"]["source_builds"]["tool"]  # type: ignore[index]
    assert entry["source"] == {
        "kind": "git",
        "url": REPO,
        "ref": "main",
        "subdir": None,
        "submodules": False,
    }
    assert entry["build"]["kind"] == "go"


def test_existing_digest_is_unchanged() -> None:
    out = io.StringIO()
    assert main(["inspect", str(QEMU_BASIC), "--json"], stdout=out) == EXIT_OK
    assert json.loads(out.getvalue())["digest"] == QEMU_BASIC_DIGEST


def test_duplicate_source_build_name_is_rejected(tmp_path: Path) -> None:
    img = _image(tmp_path)
    with pytest.raises(Exception, match="already declared"):
        img.build_from(_spec("dev"))


def test_profile_inherits_default_source_builds(tmp_path: Path) -> None:
    img = _image(tmp_path)
    img.profile("azure")
    assert "tool" in img.source_builds(profile="azure")


# ── Rendering ───────────────────────────────────────────────────────


def test_unpinned_and_pinned_rendering() -> None:
    spec = _spec()
    unpinned = spec.render()
    assert f"git clone --depth=1 -b main {REPO}" in unpinned
    assert "tool-" in unpinned and "-main" in unpinned
    pinned = spec.render(SHA_A)
    assert "unpinned" not in pinned
    assert "git clone" not in pinned
    assert f"fetch -q --depth=1 {REPO} {SHA_A}" in pinned
    assert "checkout -q FETCH_HEAD" in pinned
    assert f"-{SHA_A[:12]}" in pinned  # the cache key carries the pin


def test_commit_ref_is_pinned_inline() -> None:
    spec = _spec(SHA_B)
    assert not spec.mutable
    assert spec.render() == spec.render(SHA_A)
    assert SHA_B in spec.render()


def test_subdir_submodules_and_quoting() -> None:
    spec = SourceBuild(
        name="tool",
        source=GitSource(REPO, "main", subdir="pkg/tool", submodules=True),
        build=GoBuild(output="tool", env={"CGO_CFLAGS": "-O -D__X__", "GO111MODULE": "on"}),
        install=(Install.artifact("/usr/local/bin/tool"),),
        mark_unpinned=False,
    )
    hook = spec.render()
    assert "--recurse-submodules --shallow-submodules" in hook
    assert 'cd /build/tool/pkg/tool && mkdir -p ./build && CGO_CFLAGS="-O -D__X__" ' in hook
    assert '"$BUILDROOT/build/tool/pkg/tool/build/tool"' in hook
    assert "submodule update" in spec.render(SHA_A)


def test_cargo_and_http_rendering() -> None:
    spec = SourceBuild(
        name="prover",
        source=HttpSource("https://example.com/prover-1.0.tar.gz"),
        build=CargoBuild(output="prover", bin="prover", features=("a", "b")),
        install=(Install.artifact("/usr/bin/prover"),),
    )
    assert spec.packages == ("curl", "cargo")
    hook = spec.render()
    assert hook.startswith("# unpinned: https://example.com/prover-1.0.tar.gz\n")
    assert "cargo build --release --frozen --features a,b --bin prover" in hook
    assert "sha256sum" not in hook
    pinned = spec.render("c" * 64)
    assert f'echo "{"c" * 64}  "' in pinned
    assert "--strip-components=1" in pinned


def test_tdxs_hook_is_byte_identical_to_legacy_bash(tmp_path: Path) -> None:
    img = Image(build_dir=tmp_path / "build")
    Tdxs().apply(img)
    hooks = [h.command.argv[0] for h in img.state.profiles["default"].hooks if h.phase == "build"]
    assert TDXS_LEGACY_HOOK in hooks
    assert Tdxs().source_spec().render() == TDXS_LEGACY_HOOK


def _init_modules() -> tuple[KeyGeneration, DiskEncryption, SecretDelivery]:
    key = KeySpec("key_persistent", strategy="tpm", output="/tmp/key_persistent")
    disk = DiskSpec("disk_persistent", device=None, key=key, mount_at="/persistent")
    return (
        KeyGeneration(keys=(key,)),
        DiskEncryption(disks=(disk,)),
        SecretDelivery(method="http_post", store_at=disk),
    )


def _init_scripts(img: Image, prefix: str) -> list[str]:
    return [e.script for e in img.init_scripts() if e.script.startswith(prefix)]


def _build_phase_hooks(img: Image) -> list[str]:
    return [h.command.argv[0] for h in img.state.profiles["default"].hooks if h.phase == "build"]


def test_key_generation_hook_is_byte_identical_to_legacy_bash(tmp_path: Path) -> None:
    legacy = _legacy_go_hook("key-generation", "key-gen")
    keys, disks, delivery = _init_modules()
    img = Image(build_dir=tmp_path / "build")
    img.apply(keys, disks, delivery)
    assert legacy in _build_phase_hooks(img)
    assert keys.source_spec().render() == legacy
    assert len(_init_scripts(img, "/usr/bin/key-gen setup ")) == 1
    assert not [d for d in keys.check(img, "default") if d.level == "error"]


def test_disk_encryption_hook_is_byte_identical_to_legacy_bash(tmp_path: Path) -> None:
    legacy = _legacy_go_hook("disk-encryption", "disk-setup")
    keys, disks, delivery = _init_modules()
    img = Image(build_dir=tmp_path / "build")
    img.apply(keys, disks, delivery)
    assert legacy in _build_phase_hooks(img)
    assert disks.source_spec().render() == legacy
    assert len(_init_scripts(img, "/usr/bin/disk-setup setup ")) == 1
    assert not [d for d in disks.check(img, "default") if d.level == "error"]


def test_secret_delivery_hook_is_byte_identical_to_legacy_bash(tmp_path: Path) -> None:
    legacy = _legacy_go_hook("secret-delivery", "secret-delivery")
    keys, disks, delivery = _init_modules()
    img = Image(build_dir=tmp_path / "build")
    img.apply(keys, disks, delivery)
    assert legacy in _build_phase_hooks(img)
    assert delivery.source_spec().render() == legacy
    assert len(_init_scripts(img, "/usr/bin/secret-delivery setup ")) == 1
    assert not [d for d in delivery.check(img, "default") if d.level == "error"]


# ── Locking ─────────────────────────────────────────────────────────


def test_lock_writes_fetches_and_compile_uses_pin(tmp_path: Path) -> None:
    img = _image(tmp_path)
    resolver = _fixed(SHA_A)
    lock_path = img.lock(resolver=resolver)
    assert resolver.calls == [GitSource(REPO, "main")]
    lock = read_lockfile(lock_path)
    assert lock.fetches == [
        LockedFetch(source=REPO, kind="git", digest=SHA_A, name="tool", ref="main")
    ]
    assert img.source_pins() == {"tool": lock.fetches[0]}
    assert img.unpinned_sources() == []
    script = _build_hooks(img, tmp_path / "out")
    assert SHA_A in script
    assert "unpinned" not in script
    # The recipe payload keeps the symbolic declaration: the lock stays fresh.
    assert img.lock_status().is_clean


def test_unpinned_compile_emits_marker(tmp_path: Path) -> None:
    script = _build_hooks(_image(tmp_path), tmp_path / "out")
    assert "# unpinned: main" in script
    assert "git clone --depth=1 -b main" in script


def test_pin_for_another_ref_is_ignored(tmp_path: Path) -> None:
    img = _image(tmp_path)
    img.lock(resolver=_fixed(SHA_A))
    moved = _image(tmp_path / "x", _spec("dev"))
    moved.build_dir = img.build_dir
    assert moved.unpinned_sources() == ["tool"]


def test_lock_offline_reuses_pins_and_fails_without_them(tmp_path: Path) -> None:
    img = _image(tmp_path)
    with pytest.raises(LockfileError, match="need the network to resolve: tool"):
        img.lock(offline=True)
    img.lock(resolver=_fixed(SHA_A))
    resolver = _fixed(SHA_B)
    img.lock(offline=True, resolver=resolver)
    assert resolver.calls == []
    assert img.source_pins()["tool"].digest == SHA_A


def test_offline_lock_accepts_inline_pins(tmp_path: Path) -> None:
    img = _image(tmp_path, _spec(SHA_B))
    lock = read_lockfile(img.lock(offline=True))
    assert [f.digest for f in lock.fetches] == [SHA_B]


def test_http_source_without_sha256_is_hashed_by_resolver(tmp_path: Path) -> None:
    spec = SourceBuild(
        name="blob",
        source=HttpSource("https://example.com/blob.bin"),
        build=GoBuild(output="blob"),
        install=(Install.artifact("/usr/bin/blob"),),
    )
    img = _image(tmp_path, spec)
    lock = read_lockfile(img.lock(resolver=_fixed("d" * 64)))
    assert lock.fetches[0].kind == "http"
    assert lock.fetches[0].digest == "d" * 64
    assert lock.fetches[0].ref is None


def test_lockfile_roundtrips_fetch_name_and_ref() -> None:
    lock = build_lockfile(
        recipe={"base": "x"},
        fetches=[LockedFetch(source=REPO, kind="git", digest=SHA_A, name="tool", ref="main")],
    )
    raw = serialize_lockfile(lock)
    assert json.loads(raw)["fetches"][0]["ref"] == "main"
    assert parse_lockfile(raw).fetches == lock.fetches
    plain = build_lockfile(recipe={}, fetches=[LockedFetch(source="u", kind="http", digest="d")])
    assert "name" not in json.loads(serialize_lockfile(plain))["fetches"][0]


def test_frozen_bake_refuses_unpinned_sources(tmp_path: Path) -> None:
    img = _image(tmp_path)
    payload = img._recipe_payload(profile_names=("default",))
    write_lockfile(build_lockfile(recipe=payload), img.build_dir / "tundravm.lock")
    with pytest.raises(LockfileError, match="unpinned: tool") as excinfo:
        img.bake(frozen=True)
    assert excinfo.value.hint is not None
    assert "run tundravm lock" in excinfo.value.hint


# ── Policy ──────────────────────────────────────────────────────────


def test_policy_warn_compiles_silently_and_error_refuses(tmp_path: Path) -> None:
    for mode in ("warn", "allow"):
        img = _image(tmp_path / mode, policy=Policy(mutable_ref_policy=mode))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            img.compile(tmp_path / mode / "out")
    strict = _image(tmp_path / "e", policy=Policy(mutable_ref_policy="error"))
    with pytest.raises(PolicyError, match="tool@main"):
        strict.compile(tmp_path / "e" / "out")
    strict.lock(resolver=_fixed(SHA_A))
    strict.compile(tmp_path / "e" / "out")  # a lockfile pin satisfies the policy


# ── Drift, explain, check ───────────────────────────────────────────


def test_drift_reports_new_and_moved_sources(tmp_path: Path) -> None:
    img = Image(build_dir=tmp_path / "build")
    img.lock()
    img.build_from(_spec())
    assert "+ sources.tool" in img.lock_status().render().splitlines()
    img.lock(resolver=_fixed(SHA_A))
    assert img.lock_status().is_clean
    moved = img.lock_status(resolver=_fixed(SHA_B))
    assert f"~ sources.tool: {SHA_A[:7]} -> {SHA_B[:7]}" in moved.render().splitlines()


def test_drift_reports_removed_sources(tmp_path: Path) -> None:
    img = _image(tmp_path)
    img.lock(resolver=_fixed(SHA_A))
    bare = Image(build_dir=img.build_dir)
    assert "- sources.tool" in bare.lock_status().render().splitlines()


def test_explain_lists_sources_with_pin(tmp_path: Path) -> None:
    img = _image(tmp_path)
    assert f"tool  git {REPO}  ref=main  pinned=-" in img.summary()
    img.lock(resolver=_fixed(SHA_A))
    info = img.explain()
    assert info["sources"] == [
        {
            "build": "go",
            "install": ["/usr/bin/tool"],
            "kind": "git",
            "name": "tool",
            "pinned": SHA_A[:7],
            "ref": "main",
            "url": REPO,
        }
    ]
    assert "Sources (1):" in img.summary()
    assert f"pinned={SHA_A[:7]}" in img.summary()


def test_check_rule_source_unpinned(tmp_path: Path) -> None:
    img = _image(tmp_path)
    found = [d for d in img.check() if d.code == "source-unpinned"]
    assert [(d.level, d.subject) for d in found] == [("warning", "tool")]
    img.set_policy(Policy(mutable_ref_policy="error"))
    assert [d.level for d in img.check() if d.code == "source-unpinned"] == ["error"]
    img.lock(resolver=_fixed(SHA_A))
    assert not [d for d in img.check() if d.code == "source-unpinned"]


def test_cli_lock_offline_fails_clearly(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe.py"
    recipe.write_text(
        "from tundravm import Image\n"
        "from tundravm.source import GitSource, GoBuild, Install, SourceBuild\n"
        f"img = Image(build_dir={str(tmp_path / 'build')!r})\n"
        "img.build_from(SourceBuild(name='tool', source=GitSource('https://x/y.git', 'main'),"
        " build=GoBuild(output='tool'), install=(Install.artifact('/usr/bin/tool'),)))\n"
    )
    out = io.StringIO()
    code = main(["lock", str(recipe), "--offline"], stdout=out)
    assert code != EXIT_OK
    assert not (tmp_path / "build" / "tundravm.lock").exists()
