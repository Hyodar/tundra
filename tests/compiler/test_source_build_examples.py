"""Source builds for the example modules, and the recipe generalizations they use."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import pytest

from tests.helpers import REPO_ROOT, SURGE_EXAMPLE
from tundravm._source import BUILD_MARKER
from tundravm.declarative import (
    Build,
    Disk,
    Dotnet,
    Git,
    Go,
    Install,
    Key,
    Recipe,
    Secrets,
    Tree,
    compile,
    lock,
)
from tundravm.declarative.utils import Tdxs
from tundravm.errors import ValidationError
from tundravm.recipe import load_recipe

pytestmark = pytest.mark.usefixtures("isolated_cwd")

REPO = "https://example.com/acme/tool.git"
SHA_A = "a" * 40


def _hooks(tree: Tree) -> str:
    """The build hooks of the payload's ``mkosi.build.d/50-build.sh``, after its header."""
    path = "payload/mkosi.build.d/50-build.sh"
    content = next(e.content for e in tree.entries if e.path == path)
    assert content is not None
    return content.decode().split("\n\n", 1)[1].rstrip("\n")


def _pin_all(source: object) -> str:
    return SHA_A


def _source_builds(recipe: Recipe) -> dict[str, dict[str, object]]:
    """The source builds the lockfile records for the default variant, by name."""
    payload = json.loads(lock(recipe, resolver=_pin_all).text())
    builds: dict[str, dict[str, object]] = payload["image"]["profiles"]["default"]["source_builds"]
    return builds


# ── Example modules ─────────────────────────────────────────────────


def _surge_stack() -> Recipe:
    """The surge image: Nethermind, the taiko driver and Raiko from tundravm.components."""
    return load_recipe(SURGE_EXAMPLE / "image.py", extra_paths=[REPO_ROOT])


@pytest.mark.parametrize(
    ("name", "cache_namespace"),
    [
        ("taiko-client", "taiko-client-feat_tdx-proving"),
        ("nethermind", "nethermind-1.32.3-linux-x64"),
    ],
)
def test_surge_stack_builds_copy_the_fetched_checkouts(name: str, cache_namespace: str) -> None:
    recipe = _surge_stack()
    pinned = _hooks(compile(recipe, lock=lock(recipe, resolver=_pin_all)))
    assert "git clone" not in pinned and "fetch -q" not in pinned
    (hook,) = (line for line in pinned.splitlines() if f'"$BUILDROOT/build/{name}"' in line)
    assert re.search(rf'"\$SRCDIR/tundravm-sources/{name}-{SHA_A[:12]}-[0-9a-f]{{8}}"/. ', hook)
    # the declared cache_namespace names the fingerprinted cache key
    assert re.search(rf'build\}}/{cache_namespace}-[0-9a-f]{{16}}"', hook)


def test_every_declaration_that_builds_from_source_is_a_source_build() -> None:
    key = Key("key_persistent", output="/tmp/key_persistent")
    disk = Disk("disk_persistent", "/persistent", key=key)
    recipe = Recipe("tools", items=(Tdxs(), key, disk, Secrets(store=disk)))
    assert sorted(_source_builds(recipe)) == [
        "attested-endpoint",
        "disk-encryption",
        "key-generation",
        "tdxs",
    ]
    lines = _hooks(compile(recipe, lock=lock(recipe, resolver=_pin_all))).splitlines()
    emitted = ("tdxs", "key-generation", "disk-encryption", "attested-endpoint")
    assert len(lines) == 2 * len(emitted)  # each hook follows its `# tundravm build:` marker
    for marker, hook, name in zip(lines[::2], lines[1::2], emitted, strict=True):
        assert json.loads(marker.removeprefix(BUILD_MARKER))["name"] == name
        assert f'mkdir -p "$BUILDROOT/build/{name}" && ' in hook


# ── GoBuild / DotnetBuild generalizations ───────────────────────────


def test_go_build_output_dir_and_mkdir() -> None:
    default = Go(output="tool", package="./cmd/tool")
    assert default.artifact == "build/tool"
    assert default.command("/build/tool") == (
        'cd /build/tool && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" '
        "-o ./build/tool ./cmd/tool"
    )
    custom = Go(output="tool", package="cmd/main.go", output_dir="bin", mkdir=False)
    assert custom.artifact == "bin/tool"
    assert custom.command("/build/tool") == (
        'cd /build/tool && go build -trimpath -ldflags "-s -w -buildid=" -o bin/tool cmd/main.go'
    )
    made = Go(output="tool", output_dir="out/bin")
    assert made.command("/w").startswith("cd /w && mkdir -p out/bin && go build")
    assert made.command("/w").endswith(" -o out/bin/tool .")
    build = Build(
        "tool",
        Git(REPO, "main"),
        using=custom,
        install=(Install(custom.artifact, "/usr/bin/tool"),),
    )
    assert '"$BUILDROOT/build/tool/bin/tool"' in _hook(build)


def test_dotnet_build_restore_args_and_properties() -> None:
    build = Dotnet(
        project="src/App",
        output="app",
        restore_args=("--force",),
        properties={"PublishSingleFile": "true", "Label": "a b"},
    )
    assert build.command("/build/app") == (
        "cd /build/app && dotnet restore src/App --runtime linux-x64 --force && "
        "dotnet publish src/App --configuration Release --runtime linux-x64 "
        "--self-contained true --output /build/app/publish -p:Deterministic=true "
        '-p:ContinuousIntegrationBuild=true -p:PublishSingleFile=true -p:Label="a b"'
    )
    assert build.artifact == "publish/app"


def test_new_recipe_fields_stay_out_of_the_payload_at_their_defaults() -> None:
    go_recipe = Go(output="tool")
    go = _payload(
        Build(
            "tool",
            Git(REPO, "main"),
            using=go_recipe,
            install=(Install("build/tool", "/usr/bin/tool"),),
        )
    )
    go_build = go["build"]
    assert isinstance(go_build, dict)
    assert set(go_build) == {"kind", "output", "package", "ldflags", "tags", "env", "packages"}
    assert go["install"] == [
        {"kind": "file", "path": "build/tool", "dest": "/usr/bin/tool", "mode": "0755"}
    ]
    assert "install_to" not in go and "mode" not in go
    dotnet = Dotnet(project="p", output="o", restore_args=("--force",))
    build = _payload(
        Build(
            "app",
            Git(REPO, "main"),
            using=dotnet,
            install=(Install(dotnet.artifact, "/usr/bin/o"),),
        )
    )["build"]
    assert isinstance(build, dict)
    assert build["restore_args"] == ["--force"]
    assert "properties" not in build
    script = _payload(Build("app", Git(REPO, "main"), script="make", install=_one()))
    assert script["build"] == {"kind": "script", "script": "make", "output": "app", "packages": []}


# ── Multi-artifact install ──────────────────────────────────────────


def _one() -> tuple[Install, ...]:
    return (Install("app", "/usr/bin/app"),)


def _single(build: Build) -> Recipe:
    return Recipe("app", items=(build,))


def _hook(build: Build) -> str:
    """The build hook *build* compiles to, pinned (an unpinned hook only fails)."""
    recipe = _single(build)
    return _hooks(compile(recipe, lock=lock(recipe, resolver=_pin_all))).split("\n", 1)[1]


def _payload(build: Build) -> dict[str, object]:
    """The lockfile's record of *build*."""
    return _source_builds(_single(build))[build.name]


def _multi(*install: Install) -> Build:
    return Build("app", Git(REPO, "main"), script="make", install=install)


def test_install_steps_render_files_modes_and_directories() -> None:
    build = _multi(
        Install("out/app", "/usr/bin/app"),
        Install("conf/app.toml", "/etc/app/app.toml", mode=0o600),
        Install("share", "/usr/share/app-data/", mode=None, directory=True),
        Install("out/helper", "/usr/libexec/helper"),
    )
    hook = _hook(build)
    found = re.search(r'"\$\{BUILDDIR:-\$BUILDROOT/build\}/app-[0-9a-f]{16}"', hook)
    assert found is not None
    key = found.group()
    checkout = '"$SRCDIR/tundravm-sources/app-aaaaaaaaaaaa-26961656"'
    assert hook == (
        f'if ! ([ -d {key} ] && [ "$(ls -A {key} 2>/dev/null)" ]); then '
        f"[ -f {checkout}/.tundravm-complete ] || {{ echo 'tundravm: "
        "app-aaaaaaaaaaaa-26961656 is not fetched: run tundravm build' >&2; exit 1; } && "
        f'mkdir -p "$BUILDROOT/build/app" && cp -a --no-preserve=ownership {checkout}/. '
        '"$BUILDROOT/build/app"/ && rm -f "$BUILDROOT/build/app"/.tundravm-complete && '
        "mkosi-chroot bash -c 'cd /build/app && make' && "
        f"mkdir -p {key} && "
        f'install -D -m 0755 "$BUILDROOT/build/app/out/app" {key}/app && '
        f'install -D -m 0600 "$BUILDROOT/build/app/conf/app.toml" {key}/app.toml && '
        f'mkdir -p {key}/app-data && cp -a "$BUILDROOT/build/app/share/." {key}/app-data/ && '
        f'install -D -m 0755 "$BUILDROOT/build/app/out/helper" {key}/helper; fi && '
        f'install -D -m 0755 {key}/app "$DESTDIR/usr/bin/app" && '
        f'install -D -m 0600 {key}/app.toml "$DESTDIR/etc/app/app.toml" && '
        f'mkdir -p "$DESTDIR/usr/share/app-data" && '
        f'cp -a {key}/app-data/. "$DESTDIR/usr/share/app-data/" && '
        f'install -D -m 0755 {key}/helper "$DESTDIR/usr/libexec/helper"'
    )


@pytest.mark.parametrize(
    ("install", "message"),
    [
        (lambda: (), "installs nothing"),
        (lambda: (Install("/abs/app", "/usr/bin/app"),), "must be relative"),
        (lambda: (Install("../app", "/usr/bin/app"),), "must be relative"),
        (
            lambda: (Install("share/../..", "/usr/share/x", mode=None, directory=True),),
            "must be relative",
        ),
        (lambda: (Install("out/app", "usr/bin/app"),), "must be an absolute path"),
        (
            lambda: (Install("share", "usr/share/x", mode=None, directory=True),),
            "must be an absolute path",
        ),
        (lambda: (Install("share", "/usr/share/x", mode=0o644, directory=True),), "takes no mode"),
        (lambda: (Install("share/", "/usr/share/x"),), "names a directory"),
        (lambda: ("out/app",), "is not Install"),
        (
            lambda: (
                Install("out/app", "/usr/bin/app"),
                Install("share", "/usr/share/app", mode=None, directory=True),
            ),
            "share the file name app",
        ),
    ],
)
def test_install_validation(install: Callable[[], tuple[Install, ...]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _multi(*install())
