"""Source builds for the example modules, and the recipe generalizations they use."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from examples.modules import Nethermind, Raiko, TaikoClient

from tundravm import Image
from tundravm.errors import ValidationError
from tundravm.modules import (
    DiskEncryption,
    DiskSpec,
    KeyGeneration,
    KeySpec,
    SecretDelivery,
    Tdxs,
)
from tundravm.source import (
    DotnetBuild,
    GitSource,
    GoBuild,
    Install,
    ScriptBuild,
    SourceBuild,
)

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


def _build_phase_hooks(img: Image) -> list[str]:
    return [h.command.argv[0] for h in img.state.profiles["default"].hooks if h.phase == "build"]


# ── Example modules ─────────────────────────────────────────────────


def test_taiko_client_hook_is_byte_identical_to_legacy_bash(tmp_path: Path) -> None:
    img = Image(build_dir=tmp_path / "build")
    TaikoClient().apply(img)
    assert _build_phase_hooks(img) == [TAIKO_CLIENT_LEGACY_HOOK]
    assert TaikoClient().source_spec().render() == TAIKO_CLIENT_LEGACY_HOOK
    assert list(img.source_builds()) == ["taiko-client"]


def test_nethermind_hook_is_byte_identical_to_legacy_bash(tmp_path: Path) -> None:
    img = Image(build_dir=tmp_path / "build")
    Nethermind().apply(img)
    assert _build_phase_hooks(img) == [NETHERMIND_LEGACY_HOOK]
    assert Nethermind().source_spec().render() == NETHERMIND_LEGACY_HOOK
    assert list(img.source_builds()) == ["nethermind"]


def test_example_modules_keep_their_build_packages(tmp_path: Path) -> None:
    img = Image(build_dir=tmp_path / "build")
    img.apply(TaikoClient(), Nethermind())
    assert img.state.profiles["default"].build_packages == {
        "build-essential",
        "dotnet-runtime-10.0",
        "dotnet-sdk-10.0",
        "git",
        "golang",
    }


def test_example_modules_follow_their_fields() -> None:
    source = GitSource(TaikoClient().source.url, "main", subdir="cmd/client")
    taiko = TaikoClient(source=source).source_spec()
    assert taiko.source == source
    hook = taiko.render()
    assert "cd /build/taiko-client/cmd/client && " in hook
    assert '"$BUILDROOT/build/taiko-client/cmd/client/bin/taiko-client"' in hook
    nm_source = GitSource(Nethermind().source.url, "1.33.0")
    nm_module = Nethermind(source=nm_source, runtime="linux-arm64", user="nm")
    assert nm_module.version == "1.33.0"
    nm = nm_module.source_spec().render()
    assert '"$BUILDDIR/nethermind-1.33.0-linux-arm64"' in nm
    assert "--runtime linux-arm64" in nm
    assert '"$DESTDIR/etc/nm/NLog.config"' in nm
    assert 'mkdir -p "$DESTDIR/etc/nm/plugins"' in nm


def test_example_modules_pin_through_the_lockfile() -> None:
    for spec in (TaikoClient().source_spec(), Nethermind().source_spec()):
        pinned = spec.render(SHA_A)
        assert "git clone" not in pinned
        assert f"fetch -q --depth=1 {spec.source.url} {SHA_A}" in pinned
        assert f"{spec.cache_key}-{SHA_A[:12]}".replace("/", "_") in pinned


def test_every_module_that_builds_from_source_is_a_source_build(tmp_path: Path) -> None:
    key = KeySpec("key_persistent", strategy="tpm", output="/tmp/key_persistent")
    keys = KeyGeneration(keys=(key,))
    disk = DiskSpec("disk_persistent", device=None, key=key, mount_at="/persistent")
    disks = DiskEncryption(disks=(disk,))
    delivery = SecretDelivery(method="http_post", store_at=disk)
    img = Image(build_dir=tmp_path / "build")
    img.apply(Tdxs(), keys, disks, delivery, Raiko(), TaikoClient(), Nethermind())
    assert sorted(img.source_builds()) == [
        "disk-encryption",
        "key-generation",
        "nethermind",
        "raiko",
        "secret-delivery",
        "taiko-client",
        "tdxs",
    ]
    rendered = [spec.render() for spec in img.source_builds().values()]
    assert sorted(rendered) == sorted(_build_phase_hooks(img))


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
        install=(Install.artifact("/usr/bin/tool"),),
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
        install=(Install.artifact("/usr/bin/tool"),),
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
        install=(Install.artifact("/usr/bin/o"),),
    )
    build = spec.to_payload()["build"]
    assert isinstance(build, dict)
    assert build["restore_args"] == ["--force"]
    assert "properties" not in build


# ── Multi-artifact install ──────────────────────────────────────────


def _multi(**kwargs: object) -> SourceBuild:
    return SourceBuild(
        name="app",
        source=GitSource(REPO, "main"),
        build=ScriptBuild(script="make", output="out/app"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_install_steps_render_files_modes_and_directories() -> None:
    spec = _multi(
        install=(
            Install.artifact("/usr/bin/app"),
            Install.file("conf/app.toml", "/etc/app/app.toml", mode="0600"),
            Install.tree("share", "/usr/share/app-data/"),
            Install.file("out/helper", "/usr/libexec/helper"),
        ),
        mark_unpinned=False,
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


def test_install_without_artifact_and_payload() -> None:
    spec = _multi(
        install=(
            Install.file("out/app", "/usr/bin/app", mode="0750"),
            Install.file("conf/app.toml", "/etc/app/app.toml", mode="0600"),
            Install.tree("share/", "/usr/share/app-data"),
        ),
        mark_unpinned=False,
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
        (lambda: (Install.file("/abs/app", "/usr/bin/app"),), "must be relative"),
        (lambda: (Install.file("../app", "/usr/bin/app"),), "must be relative"),
        (lambda: (Install.tree("share/../..", "/usr/share/x"),), "must be relative"),
        (lambda: (Install.file("out/app", "usr/bin/app"),), "must be absolute"),
        (lambda: (Install.artifact("usr/bin/app"),), "must be absolute"),
        (
            lambda: (Install(kind="tree", dest="/usr/share/x", path="share", mode="0644"),),
            "takes no mode",
        ),
        (lambda: (Install.file("share/", "/usr/share/x"),), "names a directory"),
        (lambda: (Install("/etc/app.toml", "0600"),), "Unknown install kind"),  # type: ignore[arg-type]
        (
            lambda: (Install.artifact("/usr/bin/app"), Install.tree("share", "/usr/share/app")),
            "share the file name app",
        ),
    ],
)
def test_install_validation(install: Callable[[], tuple[Install, ...]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _multi(install=install())
