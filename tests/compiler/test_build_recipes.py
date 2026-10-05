"""``Build(recipe=Go/Cargo/Dotnet(...))`` renders through the toolchain build renderers."""

from __future__ import annotations

import pytest

from tundravm._source import CargoBuild, DotnetBuild, GitSource, GoBuild, ScriptBuild, SourceBuild
from tundravm._source import Install as SourceInstall
from tundravm.declarative import (
    Build,
    Cargo,
    Dotnet,
    Fragment,
    Git,
    Go,
    Install,
    Recipe,
    Tree,
    compile,
    lock,
    lower,
)
from tundravm.errors import ValidationError

pytestmark = pytest.mark.usefixtures("isolated_cwd")

REPO = "https://example.com/acme/tool.git"
SHA_A = "a" * 40


def _recipe(build: Build) -> Recipe:
    return Recipe("tool", Fragment("tool", items=(build,)))


def _pinned(recipe: Recipe) -> Tree:
    """*recipe*'s tree with its sources pinned to SHA_A: the hooks build the fetched checkout."""
    return compile(recipe, lock=lock(recipe, resolver=lambda source: SHA_A))


def _hooks(tree: Tree) -> str:
    entry = next(e for e in tree.entries if e.path == "default/scripts/04-build.sh")
    assert entry.content is not None
    return entry.content.decode().split("\n\n", 1)[1].rstrip("\n")


def _conf(tree: Tree) -> str:
    entry = next(e for e in tree.entries if e.path == "default/mkosi.conf")
    assert entry.content is not None
    return entry.content.decode()


def test_declarative_names_are_the_compiler_recipes() -> None:
    assert (Go, Cargo, Dotnet) == (GoBuild, CargoBuild, DotnetBuild)


def test_go_recipe_lowers_to_go_build_and_renders_its_command() -> None:
    go = Go(package="./cmd/tool", output="tool")
    build = Build(
        "tool", Git(REPO, "main"), recipe=go, install=(Install("build/tool", "/usr/bin/tool"),)
    )
    spec = lower(_recipe(build)).source_builds()["tool"]
    assert spec == SourceBuild(
        name="tool",
        source=GitSource(REPO, "main"),
        build=go,
        install=(SourceInstall("file", "/usr/bin/tool", "build/tool", "0755"),),
    )
    tree = _pinned(_recipe(build))
    hooks = _hooks(tree)
    assert hooks == spec.render(SHA_A, mounted=True)
    assert (
        "mkosi-chroot bash -c 'cd /build/tool && mkdir -p ./build && go build -trimpath "
        '-ldflags "-s -w -buildid=" -o ./build/tool ./cmd/tool\'' in hooks
    )
    assert "BuildPackages=\n    git\n    golang\n" in _conf(tree)


def test_go_recipe_renders_like_the_equivalent_script() -> None:
    install = (Install("build/tool", "/usr/bin/tool"),)
    script = (
        'mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" '
        "-o ./build/tool ./cmd/tool"
    )
    by_recipe = Build(
        "tool", Git(REPO, "main"), recipe=Go(package="./cmd/tool", output="tool"), install=install
    )
    by_script = Build("tool", Git(REPO, "main"), script=script, install=install)
    assert _hooks(_pinned(_recipe(by_recipe))) == _hooks(_pinned(_recipe(by_script)))


def test_cargo_recipe_renders_cargo_build_and_installs_its_artifact() -> None:
    cargo = Cargo(output="tool", bin="tool", features=("tdx",), env={"RUSTFLAGS": "-C x"})
    assert cargo.artifact == "target/release/tool"
    build = Build(
        "tool",
        Git(REPO, "main"),
        recipe=cargo,
        install=(Install(cargo.artifact, "/usr/bin/tool"),),
    )
    tree = _pinned(_recipe(build))
    hooks = _hooks(tree)
    assert (
        'mkosi-chroot bash -c \'export RUSTFLAGS="-C x" && cd /build/tool && cargo fetch && '
        "cargo build --release --frozen --features tdx --bin tool'"
    ) in hooks
    assert '"$BUILDROOT/build/tool/target/release/tool"' in hooks
    assert "BuildPackages=\n    cargo\n    git\n" in _conf(tree)


def test_dotnet_recipe_renders_dotnet_publish() -> None:
    dotnet = Dotnet(project="src/App/App.csproj", output="App")
    build = Build(
        "app",
        Git(REPO, "v1"),
        recipe=dotnet,
        install=(Install("publish", "/usr/lib/app/", mode=None, directory=True),),
    )
    hooks = _hooks(_pinned(_recipe(build)))
    assert "dotnet restore src/App/App.csproj --runtime linux-x64 && dotnet publish" in hooks
    assert "--output /build/app/publish -p:Deterministic=true" in hooks
    assert 'cp -r "$BUILDROOT/build/app/publish"/*' in hooks


def test_lock_payload_records_the_recipe() -> None:
    build = Build(
        "tool",
        Git(REPO, "main"),
        recipe=Cargo(output="tool"),
        install=(Install("target/release/tool", "/usr/bin/tool"),),
    )
    payload = lower(_recipe(build)).source_builds()["tool"].to_payload()
    assert payload["build"] == {
        "kind": "cargo",
        "output": "tool",
        "bin": None,
        "package": None,
        "features": [],
        "profile": "release",
        "env": {},
        "packages": ["cargo"],
    }
    locked = lock(_recipe(build), resolver=lambda _source: SHA_A)
    assert [(p.identity, p.digest) for p in locked.pins] == [("tool", SHA_A)]


def test_script_builds_still_lower_to_script_build() -> None:
    build = Build("tool", Git(REPO, "main"), "make", (Install("tool", "/usr/bin/tool"),))
    spec = lower(_recipe(build)).source_builds()["tool"]
    assert spec.build == ScriptBuild(script="make", output="tool")


def test_recipe_builds_are_hashable() -> None:
    install = (Install("build/tool", "/usr/bin/tool"),)
    first = Build("tool", Git(REPO, "main"), recipe=Go(output="tool"), install=install)
    second = Build("tool", Git(REPO, "main"), recipe=Go(output="tool"), install=install)
    assert first == second
    assert hash(Fragment("f", items=(first,))) == hash(Fragment("f", items=(second,)))


INSTALL = (Install("build/tool", "/usr/bin/tool"),)


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("neither script nor recipe", {}),
        ("script and recipe", {"script": "make", "recipe": Go(output="tool")}),
        ("script build as recipe", {"recipe": ScriptBuild(script="make", output="tool")}),
        ("packages beside a recipe", {"recipe": Go(output="tool"), "packages": ("gcc",)}),
        ("env beside a recipe", {"recipe": Go(output="tool"), "env": (("CGO", "0"),)}),
    ],
)
def test_build_needs_exactly_one_of_script_and_recipe(
    label: str, kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        Build("tool", Git(REPO, "main"), install=INSTALL, **kwargs)  # type: ignore[arg-type]
