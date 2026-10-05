"""Source builds: declaration, lockfile pinning, policy, drift, explain and check."""

from __future__ import annotations

import io
import json
import re
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from tests.helpers import source_dir, write_recipe_file
from tundravm._source import (
    GitSource,
    ScriptBuild,
    Source,
    SourceBuild,
    source_drift,
)
from tundravm._source import Install as SourceInstall
from tundravm.cli import EXIT_OK, main
from tundravm.declarative import (
    Backend,
    Build,
    Declaration,
    Diagnostic,
    Disk,
    Fragment,
    Git,
    Http,
    Install,
    Key,
    Lock,
    Mkosi,
    Package,
    Pin,
    Policy,
    Recipe,
    Secrets,
    Tree,
    Variant,
    bake,
    compile,
    lint,
    lock,
    lock_status,
    lower,
    write_lock,
)
from tundravm.declarative.utils import Tdxs
from tundravm.errors import LockfileError, PolicyError, ValidationError
from tundravm.lockfile import LockedFetch, build_lockfile, parse_lockfile, serialize_lockfile
from tundravm.policy import MutableRefPolicy
from tundravm.recipe import load_recipe

pytestmark = pytest.mark.usefixtures("isolated_cwd")

REPO = "https://example.com/acme/tool.git"
SHA_A = "a" * 40
SHA_B = "b" * 40
# The former examples/qemu_basic.py, kept verbatim: its digest predates source builds.
QEMU_BASIC = '''\
"""Minimal QEMU-focused recipe.

tundravm inspect examples/qemu_basic.py
tundravm bake examples/qemu_basic.py
"""

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import File, Fragment, Package, Recipe

recipe = Recipe(
    name="qemu-basic",
    base="debian/bookworm",
    common=Fragment(
        "qemu-basic",
        items=(Package("curl"), Package("jq"), File("/etc/motd", "QEMU profile\\n")),
    ),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
'''
QEMU_BASIC_DIGEST = "e5b76ccba49652aaa80470a44e57c8c9acb94562c8d96f3cc0513b17830f1e1a"
GO_SCRIPT = (
    'mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/tool ./cmd/tool'
)

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

RECIPE_FILE = """
from tundravm.declarative import Build, Fragment, Git, Install, Recipe

recipe = Recipe(
    "tool",
    Fragment(
        "tool",
        items=(
            Build(
                "tool",
                Git({repo!r}, "main"),
                script="make",
                install=(Install("tool", "/usr/bin/tool"),),
            ),
        ),
    ),
)
"""


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


def _build(ref: str = "main") -> Build:
    return Build(
        "tool",
        Git(REPO, ref),
        script=GO_SCRIPT,
        install=(Install("build/tool", "/usr/bin/tool"),),
        packages=("golang",),
    )


def _spec() -> SourceBuild:
    """What ``_build()`` lowers to."""
    return SourceBuild(
        name="tool",
        source=GitSource(REPO, "main"),
        build=ScriptBuild(script=GO_SCRIPT, output="build/tool", packages=("golang",)),
        install=(SourceInstall("file", "/usr/bin/tool", "build/tool", "0755"),),
    )


def _recipe(
    *items: Declaration | Fragment,
    policy: Policy | None = None,
    variants: tuple[Variant, ...] = (Variant("default", target="qemu"),),
    dialect: Literal["current", "nethermind-v1"] = "current",
) -> Recipe:
    return Recipe(
        "tool",
        Fragment("tool", items=items),
        variants=variants,
        mkosi=Mkosi(dialect=dialect),
        policy=policy,
    )


class _Fixed:
    """A resolver that pins every source to *digest* and records what it resolved."""

    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls: list[Source] = []

    def __call__(self, source: Source) -> str:
        self.calls.append(source)
        return self.digest


def _read(tree: Tree, path: str) -> str:
    content = next(entry.content for entry in tree.entries if entry.path == path)
    assert content is not None
    return content.decode()


def _hooks(tree: Tree, variant: str = "default") -> str:
    """The build hooks of *variant*'s ``04-build.sh``, after its shebang header."""
    return _read(tree, f"{variant}/scripts/04-build.sh").split("\n\n", 1)[1].rstrip("\n")


# ── Declaration and payload ─────────────────────────────────────────


def test_build_lowers_to_source_build_packages_and_hook() -> None:
    recipe = _recipe(_build())
    assert lower(recipe).source_builds() == {"tool": _spec()}
    tree = compile(recipe)
    assert "BuildPackages=\n    git\n    golang\n" in _read(tree, "default/mkosi.conf")
    hooks = _hooks(tree)
    assert hooks == _spec().render(mounted=True)
    assert hooks.startswith("# unpinned: main\n")


def test_payload_has_source_builds_only_when_declared() -> None:
    bare = lock(_recipe(Package("curl"))).lockfile.recipe
    assert "source_builds" not in bare["profiles"]["default"]
    payload = lock(_recipe(_build()), resolver=_Fixed(SHA_A)).lockfile.recipe
    entry = payload["profiles"]["default"]["source_builds"]["tool"]
    assert entry["source"] == {
        "kind": "git",
        "url": REPO,
        "ref": "main",
        "subdir": None,
        "submodules": False,
    }
    assert entry["build"]["kind"] == "script"


def test_existing_digest_is_unchanged(tmp_path: Path) -> None:
    recipe = write_recipe_file(tmp_path, QEMU_BASIC)
    out = io.StringIO()
    assert main(["inspect", str(recipe), "--json"], stdout=out) == EXIT_OK
    assert json.loads(out.getvalue())["digest"] == QEMU_BASIC_DIGEST


def test_duplicate_source_build_name_is_rejected() -> None:
    recipe = _recipe(_build(), _build("dev"))
    found = lint(recipe)
    assert [(d.code, d.subject) for d in found] == [("identity-collision", "Build(tool)")]
    with pytest.raises(ValidationError, match="identity-collision"):
        lower(recipe)


def test_variant_inherits_common_source_builds() -> None:
    variants = (Variant("default", target="qemu"), Variant("azure", target="azure"))
    recipe = _recipe(_build(), variants=variants)
    assert "tool" in lower(recipe).source_builds(profile="azure")
    assert _hooks(compile(recipe), "azure") == _spec().render(mounted=True)


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


def test_current_dialect_hooks_copy_the_fetched_checkout() -> None:
    recipe = _recipe(_build())
    unpinned = _hooks(compile(recipe))
    assert unpinned == (
        "# unpinned: main\n"
        "echo 'tundravm: source build tool is not pinned: run tundravm lock, then "
        "tundravm fetch' >&2 && exit 1"
    )
    pinned = _hooks(compile(recipe, lock=lock(recipe, resolver=_Fixed(SHA_A))))
    directory = source_dir("tool", SHA_A, REPO)
    checkout = f'"$SRCDIR/tundravm-sources/{directory}"'
    cache = '"${BUILDDIR:-$BUILDROOT/build}/tool-114cfef2b31621d2"'
    assert pinned == (
        f'if ! ([ -d {cache} ] && [ "$(ls -A {cache} 2>/dev/null)" ]); then '
        f"[ -f {checkout}/.tundravm-complete ] || {{ echo 'tundravm: {directory} is not "
        "fetched: run tundravm fetch RECIPE' >&2; exit 1; } && "
        f'mkdir -p "$BUILDROOT/build/tool" && cp -a --no-preserve=ownership {checkout}/. '
        '"$BUILDROOT/build/tool"/ && rm -f "$BUILDROOT/build/tool"/.tundravm-complete && '
        f"mkosi-chroot bash -c 'cd /build/tool && {GO_SCRIPT}' && mkdir -p {cache} && "
        f'install -D -m 0755 "$BUILDROOT/build/tool/build/tool" {cache}/tool; fi && '
        f'install -D -m 0755 {cache}/tool "$DESTDIR/usr/bin/tool"'
    )
    assert "git" not in pinned and "$BUILDDIR/" not in pinned
    moved = _hooks(compile(recipe, lock=lock(recipe, resolver=_Fixed(SHA_B))))
    assert f"tool-{SHA_B[:12]}" in moved and SHA_A[:12] not in moved


def test_commit_ref_is_pinned_inline() -> None:
    recipe = _recipe(_build(SHA_B))
    assert not lower(recipe).source_builds()["tool"].mutable
    elsewhere = lock(_recipe(_build()), resolver=_Fixed(SHA_A))  # pins tool@main
    tree = compile(recipe)
    assert compile(recipe, lock=elsewhere).digest == tree.digest
    assert f"tool-{SHA_B[:12]}" in _hooks(tree)
    assert "unpinned" not in _hooks(tree)


def test_subdir_submodules_env_and_quoting() -> None:
    build = Build(
        "tool",
        Git(REPO, "main", subdir="pkg/tool", submodules=True),
        script="mkdir -p ./build && go build -o ./build/tool './cmd/tool'",
        install=(Install("build/tool", "/usr/local/bin/tool"),),
        env=(("CGO_CFLAGS", "-O -D__X__"), ("GO111MODULE", "on")),
    )
    recipe = _recipe(build)
    spec = lower(recipe).source_builds()["tool"]
    hook = spec.render()
    assert "--recurse-submodules --shallow-submodules" in hook
    command = (
        '\'export CGO_CFLAGS="-O -D__X__" GO111MODULE=on && cd /build/tool/pkg/tool && '
        "mkdir -p ./build && go build -o ./build/tool '\\''./cmd/tool'\\'''"
    )
    assert command in hook
    assert '"$BUILDROOT/build/tool/pkg/tool/build/tool"' in hook
    assert "submodule update" in spec.render(SHA_A)
    mounted = _hooks(compile(recipe, lock=lock(recipe, resolver=_Fixed(SHA_A))))
    assert command in mounted  # the whole checkout is copied; the build enters the subdir
    assert '"$BUILDROOT/build/tool/pkg/tool/build/tool"' in mounted


def test_http_source_rendering() -> None:
    url = "https://example.com/prover-1.0.tar.gz"
    build = Build(
        "prover",
        Http(url),
        script="cargo build --release --frozen --features a,b --bin prover",
        install=(Install("target/release/prover", "/usr/bin/prover"),),
        packages=("cargo",),
    )
    recipe = _recipe(build)
    spec = lower(recipe).source_builds()["prover"]
    assert spec.packages == ("curl", "cargo")
    assert _hooks(compile(recipe)).startswith(f"# unpinned: {url}\n")
    hook = spec.render()
    assert hook.startswith(f"# unpinned: {url}\n")
    assert "cargo build --release --frozen --features a,b --bin prover" in hook
    assert "sha256sum" not in hook
    pinned = spec.render("c" * 64)
    assert f'echo "{"c" * 64}  "' in pinned
    assert "--strip-components=1" in pinned
    inline = _hooks(compile(_recipe(replace(build, source=Http(url, sha256="c" * 64)))))
    assert f'"$SRCDIR/tundravm-sources/{source_dir("prover", "c" * 64, url, http=True)}"' in inline
    assert "curl" not in inline and "sha256sum" not in inline


def test_tdxs_hook_is_byte_identical_to_legacy_bash() -> None:
    historical = _recipe(Tdxs(), dialect="nethermind-v1")
    assert TDXS_LEGACY_HOOK in _hooks(compile(historical)).splitlines()
    assert lower(historical).source_builds()["tdxs"].render() == TDXS_LEGACY_HOOK
    current = _hooks(compile(_recipe(Tdxs())))
    assert current.startswith("# unpinned: master\n")
    assert "run tundravm lock, then tundravm fetch" in current


def _runtime_recipe(dialect: Literal["current", "nethermind-v1"] = "current") -> Recipe:
    key = Key("key_persistent", output="/tmp/key_persistent")
    disk = Disk("disk_persistent", "/persistent", key=key)
    return _recipe(Package("linux-image-amd64"), key, disk, Secrets(store=disk), dialect=dialect)


def _assert_runtime_tool_hook(name: str, binary: str) -> None:
    legacy = _legacy_go_hook(name, binary)
    assert legacy in _hooks(compile(_runtime_recipe("nethermind-v1"))).splitlines()
    recipe = _runtime_recipe()
    tree = compile(recipe)
    assert lower(recipe).source_builds()[name].render() == legacy
    runtime_init = _read(tree, "default/mkosi.extra/usr/bin/runtime-init")
    assert runtime_init.count(f"/usr/bin/{binary} setup ") == 1
    assert not [d for d in lint(recipe) if d.level == "error"]


def test_key_generation_hook_is_byte_identical_to_legacy_bash() -> None:
    _assert_runtime_tool_hook("key-generation", "key-gen")


def test_disk_encryption_hook_is_byte_identical_to_legacy_bash() -> None:
    _assert_runtime_tool_hook("disk-encryption", "disk-setup")


def test_secret_delivery_hook_is_byte_identical_to_legacy_bash() -> None:
    _assert_runtime_tool_hook("secret-delivery", "secret-delivery")


# ── Locking ─────────────────────────────────────────────────────────


def test_lock_records_fetches_and_compile_uses_pin() -> None:
    recipe = _recipe(_build())
    resolver = _Fixed(SHA_A)
    locked = lock(recipe, resolver=resolver)
    assert resolver.calls == [GitSource(REPO, "main")]
    assert locked.lockfile.fetches == [
        LockedFetch(source=REPO, kind="git", digest=SHA_A, name="tool", ref="main")
    ]
    assert locked.pins == (Pin("tool", Git(REPO, "main"), SHA_A),)
    script = _hooks(compile(recipe, lock=locked))
    assert f"tool-{SHA_A[:12]}" in script
    assert "unpinned" not in script
    # The recipe payload keeps the symbolic declaration: the lock stays fresh.
    assert lock_status(recipe, locked) == ()


def test_unpinned_compile_emits_marker() -> None:
    script = _hooks(compile(_recipe(_build())))
    assert "# unpinned: main" in script
    assert "tundravm fetch" in script
    assert "git clone --depth=1 -b main" in _spec().render()


def test_pin_for_another_ref_is_ignored() -> None:
    locked = lock(_recipe(_build()), resolver=_Fixed(SHA_A))
    hook = _hooks(compile(_recipe(_build("dev")), lock=locked))
    assert hook.startswith("# unpinned: dev\n")
    assert SHA_A not in hook


def test_lock_offline_reuses_pins_and_fails_without_them() -> None:
    recipe = _recipe(_build())
    with pytest.raises(LockfileError, match="tool: git .* @ main: not pinned in the lockfile"):
        lock(recipe, offline=True)
    previous = lock(recipe, resolver=_Fixed(SHA_A))
    resolver = _Fixed(SHA_B)
    again = lock(recipe, previous=previous, offline=True, resolver=resolver)
    assert resolver.calls == []
    assert [pin.digest for pin in again.pins] == [SHA_A]


def test_offline_lock_accepts_inline_pins() -> None:
    locked = lock(_recipe(_build(SHA_B)), offline=True)
    assert [f.digest for f in locked.lockfile.fetches] == [SHA_B]


def test_http_source_without_sha256_is_hashed_by_resolver() -> None:
    url = "https://example.com/blob.bin"
    build = Build("blob", Http(url), script="make", install=(Install("blob", "/usr/bin/blob"),))
    locked = lock(_recipe(build), resolver=_Fixed("d" * 64))
    fetch = locked.lockfile.fetches[0]
    assert fetch.kind == "http"
    assert fetch.digest == "d" * 64
    assert fetch.ref is None
    assert locked.pins == (Pin("blob", Http(url, sha256="d" * 64), "d" * 64),)


def test_lockfile_roundtrips_fetch_name_and_ref() -> None:
    lockfile = build_lockfile(
        recipe={"base": "x"},
        fetches=[LockedFetch(source=REPO, kind="git", digest=SHA_A, name="tool", ref="main")],
    )
    raw = serialize_lockfile(lockfile)
    assert json.loads(raw)["fetches"][0]["ref"] == "main"
    assert parse_lockfile(raw).fetches == lockfile.fetches
    plain = build_lockfile(recipe={}, fetches=[LockedFetch(source="u", kind="http", digest="d")])
    assert "name" not in json.loads(serialize_lockfile(plain))["fetches"][0]


def test_frozen_bake_refuses_unpinned_sources(tmp_path: Path) -> None:
    recipe = _recipe(_build(), Package("linux-image-amd64"))
    pinned = lock(recipe, resolver=_Fixed(SHA_A))
    unpinned = Lock.of(build_lockfile(recipe=pinned.lockfile.recipe))
    with pytest.raises(LockfileError, match="unpinned: tool") as excinfo:
        bake(recipe, lock=unpinned, backend=Backend("inprocess"), out=tmp_path / "out")
    assert excinfo.value.hint is not None
    assert "run tundravm lock" in excinfo.value.hint


# ── Policy ──────────────────────────────────────────────────────────


def test_policy_warn_compiles_silently_and_error_refuses() -> None:
    lenient: tuple[MutableRefPolicy, ...] = ("warn", "allow")
    for mode in lenient:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(_recipe(_build(), policy=Policy(mutable_ref_policy=mode)))
    strict = _recipe(_build(), policy=Policy(mutable_ref_policy="error"))
    with pytest.raises(PolicyError, match="tool@main"):
        compile(strict)
    compile(strict, lock=lock(strict, resolver=_Fixed(SHA_A)))  # a lock pin satisfies it


# ── Drift, explain, check ───────────────────────────────────────────


def test_drift_reports_new_and_moved_sources() -> None:
    recipe = _recipe(Package("curl"), _build())
    added = Diagnostic("lock-added", "source tool is not pinned", subject="sources.tool")
    assert added in lock_status(recipe, lock(_recipe(Package("curl"))))
    locked = lock(recipe, resolver=_Fixed(SHA_A))
    assert lock_status(recipe, locked) == ()
    builds = lower(recipe).source_builds()
    pins = {"tool": locked.lockfile.fetches[0]}
    moved = source_drift(builds, pins, resolver=_Fixed(SHA_B))
    assert moved == ([], ["sources.tool"], [], {"sources.tool": f"{SHA_A[:7]} -> {SHA_B[:7]}"})
    retargeted = lock_status(_recipe(Package("curl"), _build("dev")), locked)
    changed = f"sources.tool changed since the lock: {SHA_A[:7]} -> dev"
    assert Diagnostic("lock-changed", changed, subject="sources.tool") in retargeted


def test_drift_reports_removed_sources() -> None:
    locked = lock(_recipe(Package("curl"), _build()), resolver=_Fixed(SHA_A))
    removed = Diagnostic("lock-removed", "sources.tool is only in the lock", subject="sources.tool")
    assert removed in lock_status(_recipe(Package("curl")), locked)


def _inspect(recipe: Path, *flags: str) -> str:
    out = io.StringIO()
    assert main(["inspect", str(recipe), *flags], stdout=out) == EXIT_OK
    return out.getvalue()


def test_explain_lists_sources_with_pin(tmp_path: Path) -> None:
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE_FILE.format(repo=REPO), encoding="utf-8")
    assert f"tool  git {REPO}  ref=main  pinned=-" in _inspect(path)
    locked = lock(load_recipe(path), resolver=_Fixed(SHA_A))
    write_lock(locked, tmp_path / "build" / "tundravm.lock")  # where the CLI looks
    info = json.loads(_inspect(path, "--json"))["variants"]["default"]
    assert info["sources"] == [
        {
            "build": "script",
            "install": ["/usr/bin/tool"],
            "kind": "git",
            "name": "tool",
            "pinned": SHA_A[:7],
            "ref": "main",
            "url": REPO,
        }
    ]
    summary = _inspect(path)
    assert "Sources (1):" in summary
    assert f"pinned={SHA_A[:7]}" in summary


def test_inspect_renders_hooks_at_the_pins_it_shows(tmp_path: Path) -> None:
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE_FILE.format(repo=REPO), encoding="utf-8")
    write_lock(lock(load_recipe(path), resolver=_Fixed(SHA_A)), tmp_path / "pins.lock")

    def shown(*flags: str) -> tuple[str, object]:
        info = json.loads(_inspect(path, "--json", *flags))["variants"]["default"]
        return info["hooks"]["build"][0], info["sources"][0]["pinned"]

    hook, pinned = shown()
    assert "not pinned" in hook and pinned is None
    hook, pinned = shown("--lockfile", str(tmp_path / "pins.lock"))
    assert "not pinned" not in hook and pinned == SHA_A[:7]
    assert re.search(r"build\}/tool-[0-9a-f]{16}", hook)
    (tmp_path / "build").mkdir()
    (tmp_path / "pins.lock").rename(tmp_path / "build" / "tundravm.lock")
    assert shown() == (hook, pinned)
    text = _inspect(path)
    assert f"pinned={SHA_A[:7]}" in text and re.search(r"build\}/tool-[0-9a-f]{16}", text)


def test_check_rule_source_unpinned() -> None:
    recipe = _recipe(_build())
    found = [d for d in lint(recipe) if d.code == "source-unpinned"]
    assert [(d.level, d.subject) for d in found] == [("warning", "tool")]
    strict = _recipe(_build(), policy=Policy(mutable_ref_policy="error"))
    assert [d.level for d in lint(strict) if d.code == "source-unpinned"] == ["error"]
    # lint reads the pins of ./build/tundravm.lock, as `tundravm lint` does.
    write_lock(lock(recipe, resolver=_Fixed(SHA_A)), Path("build") / "tundravm.lock")
    assert not [d for d in lint(recipe) if d.code == "source-unpinned"]


def test_cli_lock_offline_fails_clearly(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe.py"
    recipe.write_text(RECIPE_FILE.format(repo="https://x/y.git"), encoding="utf-8")
    out = io.StringIO()
    code = main(["lock", str(recipe), "--offline"], stdout=out)
    assert code != EXIT_OK
    assert not (tmp_path / "build" / "tundravm.lock").exists()
