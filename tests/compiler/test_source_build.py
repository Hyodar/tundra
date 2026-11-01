"""Source builds: declaration, lockfile pinning, policy, drift, explain and check."""

from __future__ import annotations

import io
import json
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import conf_list, source_dir
from tundravm._modules.base import tools_source
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
    Fragment,
    Git,
    Http,
    Install,
    Lock,
    Package,
    Pin,
    Policy,
    Recipe,
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
GO_SCRIPT = (
    'mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/tool ./cmd/tool'
)
# The first line of _build()'s hook: the declaration it is rendered from.
TOOL_MARKER = (
    '# tundravm build: {"build":{"kind":"script","output":"build/tool","packages":["golang"],'
    '"script":"mkdir -p ./build && go build -trimpath -ldflags \\"-s -w -buildid=\\" '
    '-o ./build/tool ./cmd/tool"},"install":[{"dest":"/usr/bin/tool","kind":"file",'
    '"mode":"0755","path":"build/tool"}],"name":"tool","source":{"kind":"git","ref":"main",'
    '"submodules":false,"url":"https://example.com/acme/tool.git"}}'
)

RECIPE_FILE = """
from tundravm.declarative import Build, Fragment, Git, Install, Recipe

recipe = Recipe(
    "tool",
    items=(
        Build(
            "tool",
            Git({repo!r}, "main"),
            script="make",
            install=(Install("tool", "/usr/bin/tool"),),
        ),
    ),
)
"""


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
    variants: tuple[Variant, ...] = (Variant("default", outputs=("qemu",)),),
) -> Recipe:
    return Recipe("tool", items=items, variants=variants, policy=policy)


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


def _hooks(tree: Tree) -> str:
    """The build hooks of the payload's ``mkosi.build.d/50-build.sh``, after its header."""
    script = _read(tree, "payload/mkosi.build.d/50-build.sh")
    return script.split("\n\n", 1)[1].rstrip("\n")


# ── Declaration and payload ─────────────────────────────────────────


def test_build_lowers_to_source_build_packages_and_hook() -> None:
    recipe = _recipe(_build())
    assert lower(recipe).source_builds() == {"tool": _spec()}
    tree = compile(recipe)
    # the checkout is fetched on the host: the build needs the toolchain alone
    assert conf_list(_read(tree, "payload/mkosi.conf"), "BuildPackages") == ["golang"]
    hooks = _hooks(tree)
    assert hooks == _spec().render()
    assert hooks.startswith(f"{TOOL_MARKER}\n# unpinned: main\n")
    assert _spec().marker() == TOOL_MARKER


def test_payload_has_source_builds_only_when_declared() -> None:
    bare = lock(_recipe(Package("curl"))).lockfile.image
    assert "source_builds" not in bare["profiles"]["default"]
    payload = lock(_recipe(_build()), resolver=_Fixed(SHA_A)).lockfile.image
    entry = payload["profiles"]["default"]["source_builds"]["tool"]
    assert entry["source"] == {
        "kind": "git",
        "url": REPO,
        "ref": "main",
        "subdir": None,
        "submodules": False,
    }
    assert entry["build"]["kind"] == "script"


def test_duplicate_source_build_name_is_rejected() -> None:
    recipe = _recipe(_build(), _build("dev"))
    found = lint(recipe)
    assert [(d.code, d.subject) for d in found] == [("identity-collision", "Build(tool)")]
    with pytest.raises(ValidationError, match="identity-collision"):
        lower(recipe)


# ── Rendering ───────────────────────────────────────────────────────


def test_hooks_copy_the_fetched_checkout() -> None:
    recipe = _recipe(_build())
    unpinned = _hooks(compile(recipe))
    assert unpinned == (
        f"{TOOL_MARKER}\n"
        "# unpinned: main\n"
        "echo 'tundravm: source build tool is not pinned: run tundravm lock, then "
        "tundravm build' >&2 && exit 1"
    )
    pinned = _hooks(compile(recipe, lock=lock(recipe, resolver=_Fixed(SHA_A))))
    directory = source_dir("tool", SHA_A, REPO)
    checkout = f'"$SRCDIR/tundravm-sources/{directory}"'
    cache = '"${BUILDDIR:-$BUILDROOT/build}/tool-8d01ae807e60c913"'
    assert pinned == (
        f"{TOOL_MARKER}\n"
        f'if ! ([ -d {cache} ] && [ "$(ls -A {cache} 2>/dev/null)" ]); then '
        f"[ -f {checkout}/.tundravm-complete ] || {{ echo 'tundravm: {directory} is not "
        "fetched: run tundravm build' >&2; exit 1; } && "
        f'mkdir -p "$BUILDROOT/build/tool" && cp -a --no-preserve=ownership {checkout}/. '
        '"$BUILDROOT/build/tool"/ && rm -f "$BUILDROOT/build/tool"/.tundravm-complete && '
        f"mkosi-chroot bash -c 'cd /build/tool && {GO_SCRIPT}' && mkdir -p {cache} && "
        f'install -D -m 0755 "$BUILDROOT/build/tool/build/tool" {cache}/tool; fi && '
        f'install -D -m 0755 {cache}/tool "$DESTDIR/usr/bin/tool"'
    )
    assert "git" not in pinned.split("\n", 1)[1] and "$BUILDDIR/" not in pinned
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
        env={"CGO_CFLAGS": "-O -D__X__", "GO111MODULE": "on"},
    )
    recipe = _recipe(build)
    spec = lower(recipe).source_builds()["tool"]
    hook = spec.render(SHA_A)
    command = (
        '\'export CGO_CFLAGS="-O -D__X__" GO111MODULE=on && cd /build/tool/pkg/tool && '
        "mkdir -p ./build && go build -o ./build/tool '\\''./cmd/tool'\\'''"
    )
    assert command in hook
    assert '"$BUILDROOT/build/tool/pkg/tool/build/tool"' in hook
    assert "submodule update" not in hook  # the host checkout has them
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
    assert spec.packages == ("cargo",)  # the archive is downloaded on the host
    assert _hooks(compile(recipe)).split("\n", 1)[1].startswith(f"# unpinned: {url}\n")
    pinned = spec.render("c" * 64)
    assert "cargo build --release --frozen --features a,b --bin prover" in pinned
    assert f"$SRCDIR/tundravm-sources/{source_dir('prover', 'c' * 64, url, http=True)}" in pinned
    assert "curl" not in pinned.split("\n", 1)[1] and "sha256sum" not in pinned
    inline = _hooks(compile(_recipe(replace(build, source=Http(url, sha256="c" * 64)))))
    assert f'"$SRCDIR/tundravm-sources/{source_dir("prover", "c" * 64, url, http=True)}"' in inline
    assert "curl" not in inline.split("\n", 1)[1] and "sha256sum" not in inline


def test_tdxs_hook_copies_the_fetched_checkout() -> None:
    recipe = _recipe(Tdxs())
    current = _hooks(compile(recipe))
    assert current == lower(recipe).source_builds()["tdxs"].render()
    assert current.startswith('# tundravm build: {"build":{"env":{},"kind":"go"')
    assert current.split("\n", 1)[1].startswith(f"# unpinned: {tools_source().requested}\n")
    assert "run tundravm lock, then tundravm build" in current


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
    assert source_dir("tool", SHA_A, REPO) in script
    assert "unpinned" not in script
    # The recipe payload keeps the symbolic declaration: the lock stays fresh.
    assert lock_status(recipe, locked) == ()


def test_pin_for_another_ref_is_ignored() -> None:
    locked = lock(_recipe(_build()), resolver=_Fixed(SHA_A))
    hook = _hooks(compile(_recipe(_build("dev")), lock=locked))
    assert hook.split("\n", 1)[1].startswith("# unpinned: dev\n")
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
        image={"base": "x"},
        fetches=[LockedFetch(source=REPO, kind="git", digest=SHA_A, name="tool", ref="main")],
    )
    raw = serialize_lockfile(lockfile)
    assert json.loads(raw)["fetches"][0]["ref"] == "main"
    assert parse_lockfile(raw).fetches == lockfile.fetches
    plain = build_lockfile(image={}, fetches=[LockedFetch(source="u", kind="http", digest="d")])
    assert "name" not in json.loads(serialize_lockfile(plain))["fetches"][0]


def test_frozen_bake_refuses_unpinned_sources(tmp_path: Path) -> None:
    recipe = _recipe(_build(), Package("linux-image-amd64"))
    pinned = lock(recipe, resolver=_Fixed(SHA_A))
    unpinned = Lock.of(build_lockfile(image=pinned.lockfile.image))
    with pytest.raises(LockfileError, match="unpinned: tool") as excinfo:
        bake(recipe, lock=unpinned, backend=Backend("local"), build=tmp_path / "out", fetch=False)
    assert excinfo.value.hint is not None
    assert "`tundravm lock`" in excinfo.value.hint


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


@pytest.mark.parametrize(
    ("current", "locked_items", "diagnostic", "drift"),
    [
        (
            (Package("curl"), _build()),
            (Package("curl"),),
            Diagnostic("lock-added", "error", "source tool is not pinned", subject="sources.tool"),
            None,
        ),
        # A fresh lock reports nothing; source_drift names its pin moving upstream.
        (
            (Package("curl"), _build()),
            (Package("curl"), _build()),
            None,
            ([], ["sources.tool"], [], {"sources.tool": f"{SHA_A[:7]} -> {SHA_B[:7]}"}),
        ),
        (
            (Package("curl"), _build("dev")),
            (Package("curl"), _build()),
            Diagnostic(
                "lock-changed",
                "error",
                f"sources.tool changed since the lock: {SHA_A[:7]} -> dev",
                subject="sources.tool",
            ),
            None,
        ),
        (
            (Package("curl"),),
            (Package("curl"), _build()),
            Diagnostic(
                "lock-removed", "error", "sources.tool is only in the lock", subject="sources.tool"
            ),
            None,
        ),
    ],
    ids=["unlocked-source-added", "fresh-lock-pin-moved", "retargeted-ref-changed", "removed"],
)
def test_drift_reports_added_moved_changed_and_removed_sources(
    current: tuple[Declaration, ...],
    locked_items: tuple[Declaration, ...],
    diagnostic: Diagnostic | None,
    drift: tuple[list[str], list[str], list[str], dict[str, str]] | None,
) -> None:
    recipe = _recipe(*current)
    locked = lock(_recipe(*locked_items), resolver=_Fixed(SHA_A))
    status = lock_status(recipe, locked)
    if diagnostic is None:
        assert status == ()
    else:
        assert diagnostic in [replace(found, hint=None) for found in status]
    if drift is not None:
        builds = lower(recipe).source_builds()
        pins = {"tool": locked.lockfile.fetches[0]}
        assert source_drift(builds, pins, resolver=_Fixed(SHA_B)) == drift


def _inspect(recipe: Path, *flags: str) -> str:
    out = io.StringIO()
    assert main(["show", str(recipe), *flags], stdout=out) == EXIT_OK
    return out.getvalue()


@pytest.mark.parametrize(
    ("locked", "pinned", "column"),
    [(False, None, "-"), (True, SHA_A[:7], SHA_A[:7])],
    ids=["unlocked", "locked"],
)
def test_show_lists_sources_at_their_pins(
    tmp_path: Path, locked: bool, pinned: str | None, column: str
) -> None:
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE_FILE.format(repo=REPO), encoding="utf-8")
    if locked:
        pins = lock(load_recipe(path), resolver=_Fixed(SHA_A))
        write_lock(pins, tmp_path / "build" / "tundravm.lock")  # the one lockfile the CLI reads
    info = json.loads(_inspect(path, "--json"))["resolved"]["default"]
    assert info["sources"] == [
        {
            "build": "script",
            "install": ["/usr/bin/tool"],
            "kind": "git",
            "name": "tool",
            "pinned": pinned,
            "ref": "main",
            "url": REPO,
        }
    ]
    assert info["build_steps"][0] == "builds tool from source"
    text = _inspect(path)
    assert f"Sources (1):\n    tool  git {REPO}  ref=main  pinned={column}\n" in text


def test_check_rule_source_unpinned() -> None:
    recipe = _recipe(_build())
    found = [d for d in lint(recipe) if d.code == "source-unpinned"]
    assert [(d.severity, d.subject) for d in found] == [("warning", "sources.tool")]
    strict = _recipe(_build(), policy=Policy(mutable_ref_policy="error"))
    assert [d.severity for d in lint(strict) if d.code == "source-unpinned"] == ["error"]
    # lint reads the pins of ./build/tundravm.lock, as `tundravm lint` does.
    write_lock(lock(recipe, resolver=_Fixed(SHA_A)), Path("build") / "tundravm.lock")
    assert not [d for d in lint(recipe) if d.code == "source-unpinned"]
