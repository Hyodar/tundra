"""Source builds for the example modules, and the recipe generalizations they use."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from examples.modules import nethermind, raiko, taiko_client

from tundravm._source import DotnetBuild, GitSource, GoBuild, Source, SourceBuild
from tundravm._source import Install as SourceInstall
from tundravm.declarative import (
    Build,
    Disk,
    Fragment,
    Git,
    Install,
    Key,
    Mkosi,
    Package,
    Recipe,
    Secrets,
    Tree,
    compile,
    lock,
    lower,
    tdxs,
)
from tundravm.errors import ValidationError

REPO = "https://example.com/acme/tool.git"
SHA_A = "a" * 40

# The hook TaikoClient wrote by hand before source builds existed (default arguments).
TAIKO_CLIENT_LEGACY_HOOK = (
    'if ! ([ -d "$BUILDDIR/taiko-client-feat_tdx-proving" ] && '
    '[ "$(ls -A "$BUILDDIR/taiko-client-feat_tdx-proving" 2>/dev/null)" ]); then '
    "git clone --depth=1 -b feat/tdx-proving https://github.com/NethermindEth/surge-taiko-mono "
    '"$BUILDROOT/build/taiko-client" && mkosi-chroot bash -c '
    "'cd /build/taiko-client/packages/taiko-client && GO111MODULE=on "
    'CGO_CFLAGS="-O -D__BLST_PORTABLE__" CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" '
    'go build -trimpath -ldflags "-s -w -buildid=" -o bin/taiko-client cmd/main.go\' && '
    'mkdir -p "$BUILDDIR/taiko-client-feat_tdx-proving" && install -D -m 0755 '
    '"$BUILDROOT/build/taiko-client/packages/taiko-client/bin/taiko-client" '
    '"$BUILDDIR/taiko-client-feat_tdx-proving"/taiko-client; fi && '
    'install -D -m 0755 "$BUILDDIR/taiko-client-feat_tdx-proving"/taiko-client '
    '"$DESTDIR/usr/bin/taiko-client"'
)

# The hook Nethermind wrote by hand before source builds existed (default arguments).
_NM_KEY = "$BUILDDIR/nethermind-1.32.3-linux-x64"
NETHERMIND_LEGACY_HOOK = (
    f'if ! ([ -d "{_NM_KEY}" ] && [ "$(ls -A "{_NM_KEY}" 2>/dev/null)" ]); then '
    "git clone --depth=1 -b 1.32.3 https://github.com/NethermindEth/nethermind.git "
    '"$BUILDROOT/build/nethermind" && mkosi-chroot bash -c \'export '
    "DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 DOTNET_NOLOGO=1 "
    "DOTNET_CLI_HOME=/tmp/dotnet NUGET_PACKAGES=/tmp/nuget && cd /build/nethermind && "
    "dotnet restore src/Nethermind/Nethermind.Runner --runtime linux-x64 "
    "--disable-parallel --force && dotnet publish src/Nethermind/Nethermind.Runner "
    "--configuration Release --runtime linux-x64 --self-contained true "
    "--output /build/nethermind/publish -p:Deterministic=true "
    "-p:ContinuousIntegrationBuild=true -p:PublishSingleFile=true -p:BuildTimestamp=0 "
    "-p:Commit=0000000000000000000000000000000000000000 -p:PublishReadyToRun=false "
    "-p:DebugType=none -p:IncludeAllContentForSelfExtract=true "
    "-p:IncludePackageReferencesDuringMarkupCompilation=true -p:EmbedUntrackedSources=true "
    f'-p:PublishRepositoryUrl=true\' && mkdir -p "{_NM_KEY}" && '
    f'install -D -m 0755 "$BUILDROOT/build/nethermind/publish/nethermind" "{_NM_KEY}"/nethermind'
    f' && install -D -m 0644 "$BUILDROOT/build/nethermind/publish/NLog.config" '
    f'"{_NM_KEY}"/NLog.config && mkdir -p "{_NM_KEY}"/plugins && '
    f'cp -r "$BUILDROOT/build/nethermind/publish/plugins"/* "{_NM_KEY}"/plugins/; fi && '
    f'install -D -m 0755 "{_NM_KEY}"/nethermind "$DESTDIR/usr/bin/nethermind" && '
    f'install -D -m 0644 "{_NM_KEY}"/NLog.config "$DESTDIR/etc/nethermind-surge/NLog.config" && '
    'mkdir -p "$DESTDIR/etc/nethermind-surge/plugins" && '
    f'cp -r "{_NM_KEY}"/plugins/* "$DESTDIR/etc/nethermind-surge/plugins"/'
)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``compile`` applies the pins of ``./build/tundravm.lock``; keep the repository's out."""
    monkeypatch.chdir(tmp_path)


def _hooks(tree: Tree) -> str:
    """The build hooks of the default variant's ``04-build.sh``, after its shebang header."""
    content = next(e.content for e in tree.entries if e.path == "default/scripts/04-build.sh")
    assert content is not None
    return content.decode().split("\n\n", 1)[1].rstrip("\n")


def _pin_all(source: Source) -> str:
    return SHA_A


# ── Example modules ─────────────────────────────────────────────────


def _surge_stack(dialect: Literal["current", "nethermind-v1"] = "nethermind-v1") -> Recipe:
    return Recipe(
        name="stack",
        mkosi=Mkosi(dialect=dialect),
        common=Fragment(
            "stack",
            items=(tdxs(), Package("git", role="build"), raiko(), taiko_client(), nethermind()),
        ),
    )


def test_taiko_client_hook_is_byte_identical_to_legacy_bash() -> None:
    assert TAIKO_CLIENT_LEGACY_HOOK in _hooks(compile(_surge_stack())).splitlines()


def test_nethermind_hook_is_byte_identical_to_legacy_bash() -> None:
    assert NETHERMIND_LEGACY_HOOK in _hooks(compile(_surge_stack())).splitlines()


def test_current_dialect_marks_unpinned_app_builds() -> None:
    hooks = _hooks(compile(_surge_stack("current")))
    assert "\n# unpinned: feat/tdx-proving\n" + TAIKO_CLIENT_LEGACY_HOOK + "\n" in hooks


def test_example_modules_pin_through_the_lockfile() -> None:
    recipe = _surge_stack()
    builds = lower(recipe).source_builds()
    pinned = _hooks(compile(recipe, lock=lock(recipe, resolver=_pin_all)))
    assert "git clone" not in pinned
    for name in ("taiko-client", "nethermind"):
        spec = builds[name]
        assert isinstance(spec.source, GitSource)
        assert spec.render(SHA_A) in pinned.splitlines()
        assert f"fetch -q --depth=1 {spec.source.url} {SHA_A}" in spec.render(SHA_A)
        assert f"{spec.cache_key}-{SHA_A[:12]}".replace("/", "_") in spec.render(SHA_A)


def test_every_module_that_builds_from_source_is_a_source_build() -> None:
    key = Key("key_persistent", output="/tmp/key_persistent")
    disk = Disk("disk_persistent", "/persistent", key=key)
    recipe = Recipe("tools", Fragment("tools", items=(tdxs(), key, disk, Secrets(store=disk))))
    builds = lower(recipe).source_builds()
    assert sorted(builds) == [
        "disk-encryption",
        "key-generation",
        "secret-delivery",
        "tdxs",
    ]
    emitted = ("tdxs", "key-generation", "disk-encryption", "secret-delivery")
    assert _hooks(compile(recipe)) == "\n".join(builds[name].render() for name in emitted)


# ── GoBuild / DotnetBuild generalizations ───────────────────────────


def test_go_build_output_dir_and_mkdir() -> None:
    default = GoBuild(output="tool", package="./cmd/tool")
    assert default.artifact == "build/tool"
    assert default.command("/build/tool") == (
        'cd /build/tool && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" '
        "-o ./build/tool ./cmd/tool"
    )
    custom = GoBuild(output="tool", package="cmd/main.go", output_dir="bin", mkdir=False)
    assert custom.artifact == "bin/tool"
    assert custom.command("/build/tool") == (
        'cd /build/tool && go build -trimpath -ldflags "-s -w -buildid=" -o bin/tool cmd/main.go'
    )
    made = GoBuild(output="tool", output_dir="out/bin")
    assert made.command("/w").startswith("cd /w && mkdir -p out/bin && go build")
    spec = SourceBuild(
        name="tool",
        source=GitSource(REPO, "main"),
        build=custom,
        install=(SourceInstall.artifact("/usr/bin/tool"),),
    )
    assert '"$BUILDROOT/build/tool/bin/tool"' in spec.render()


def test_dotnet_build_restore_args_and_properties() -> None:
    build = DotnetBuild(
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
    go = SourceBuild(
        name="tool",
        source=GitSource(REPO, "main"),
        build=GoBuild(output="tool"),
        install=(SourceInstall.artifact("/usr/bin/tool"),),
    ).to_payload()
    go_build = go["build"]
    assert isinstance(go_build, dict)
    assert set(go_build) == {"kind", "output", "package", "ldflags", "tags", "env", "packages"}
    assert go["install"] == [
        {"kind": "artifact", "path": None, "dest": "/usr/bin/tool", "mode": "0755"}
    ]
    assert "install_to" not in go and "mode" not in go
    dotnet = DotnetBuild(project="p", output="o", restore_args=("--force",))
    spec = SourceBuild(
        name="app",
        source=GitSource(REPO, "main"),
        build=dotnet,
        install=(SourceInstall.artifact("/usr/bin/o"),),
    )
    build = spec.to_payload()["build"]
    assert isinstance(build, dict)
    assert build["restore_args"] == ["--force"]
    assert "properties" not in build
    script = _spec(Build("app", Git(REPO, "main"), script="make", install=_one())).to_payload()
    assert script["build"] == {"kind": "script", "script": "make", "output": "app", "packages": []}


# ── Multi-artifact install ──────────────────────────────────────────


def _one() -> tuple[Install, ...]:
    return (Install("app", "/usr/bin/app"),)


def _spec(build: Build) -> SourceBuild:
    """The source build *build* lowers to."""
    recipe = Recipe("app", Fragment("app", items=(build,)), mkosi=Mkosi(dialect="nethermind-v1"))
    return lower(recipe).source_builds()[build.name]


def _multi(*install: Install) -> Build:
    return Build("app", Git(REPO, "main"), script="make", install=install)


def test_install_steps_render_files_modes_and_directories() -> None:
    spec = _spec(
        _multi(
            Install("out/app", "/usr/bin/app"),
            Install("conf/app.toml", "/etc/app/app.toml", mode=0o600),
            Install("share", "/usr/share/app-data/", mode=None, directory=True),
            Install("out/helper", "/usr/libexec/helper"),
        )
    )
    url_hash = hashlib.sha256(REPO.encode()).hexdigest()[:12]
    key = f'"$BUILDDIR/app-{url_hash}-main"'
    assert spec.render() == (
        f'if ! ([ -d {key} ] && [ "$(ls -A {key} 2>/dev/null)" ]); then '
        f'git clone --depth=1 -b main {REPO} "$BUILDROOT/build/app" && '
        "mkosi-chroot bash -c 'cd /build/app && make' && "
        f"mkdir -p {key} && "
        f'install -D -m 0755 "$BUILDROOT/build/app/out/app" {key}/app && '
        f'install -D -m 0600 "$BUILDROOT/build/app/conf/app.toml" {key}/app.toml && '
        f'mkdir -p {key}/app-data && cp -r "$BUILDROOT/build/app/share"/* {key}/app-data/ && '
        f'install -D -m 0755 "$BUILDROOT/build/app/out/helper" {key}/helper; fi && '
        f'install -D -m 0755 {key}/app "$DESTDIR/usr/bin/app" && '
        f'install -D -m 0600 {key}/app.toml "$DESTDIR/etc/app/app.toml" && '
        f'mkdir -p "$DESTDIR/usr/share/app-data" && '
        f'cp -r {key}/app-data/* "$DESTDIR/usr/share/app-data"/ && '
        f'install -D -m 0755 {key}/helper "$DESTDIR/usr/libexec/helper"'
    )


def test_install_modes_directories_and_payload() -> None:
    spec = _spec(
        _multi(
            Install("out/app", "/usr/bin/app", mode=0o750),
            Install("conf/app.toml", "/etc/app/app.toml", mode=0o600),
            Install("share/", "/usr/share/app-data", mode=None, directory=True),
        )
    )
    hook = spec.render()
    assert 'install -D -m 0750 "$BUILDROOT/build/app/out/app"' in hook
    assert 'install -D -m 0600 "$BUILDROOT/build/app/conf/app.toml"' in hook
    assert 'cp -r "$BUILDROOT/build/app/share"/* ' in hook
    assert hook.endswith('/app-data/* "$DESTDIR/usr/share/app-data"/')
    assert spec.to_payload()["install"] == [
        {"kind": "file", "path": "out/app", "dest": "/usr/bin/app", "mode": "0750"},
        {"kind": "file", "path": "conf/app.toml", "dest": "/etc/app/app.toml", "mode": "0600"},
        {"kind": "tree", "path": "share/", "dest": "/usr/share/app-data", "mode": None},
    ]


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
