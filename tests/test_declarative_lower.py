"""Lowering: a declarative Recipe builds the same RecipeState as the fluent calls."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tundravm import Image, MkosiOptions
from tundravm.declarative import (
    Build,
    Debloat,
    Directory,
    Disk,
    File,
    Fragment,
    Git,
    Group,
    Hook,
    Init,
    Install,
    Kernel,
    Key,
    Mkosi,
    Package,
    Recipe,
    RuntimeTools,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Setting,
    Unit,
    User,
    Variant,
    lower,
)
from tundravm.declarative.lower import inject_after_init
from tundravm.diff import diff_trees
from tundravm.errors import ValidationError
from tundravm.lockfile import recipe_digest
from tundravm.models import Kernel as FluentKernel
from tundravm.models import SecretSpec, SecretTarget
from tundravm.modules import DiskEncryption, DiskSpec, KeyGeneration, KeySpec, SecretDelivery
from tundravm.platforms import AzurePlatform
from tundravm.source import GitSource, ScriptBuild, SourceBuild
from tundravm.source import Install as FluentInstall

TOOLS = "https://example.com/tundra-tools.git"
APP_REPO = "https://example.com/app.git"
UNIT = """\
[Unit]
Description=App
After=network.target

[Service]
ExecStart=/usr/bin/app

[Install]
WantedBy=minimal.target
"""
UNIT_AFTER_INIT = UNIT.replace(
    "After=network.target\n",
    "After=runtime-init.service network.target\nRequires=runtime-init.service\n",
)
PROFILES = ("default", "azure")


def declarative_recipe() -> Recipe:
    key = Key("key_persistent", output="/tmp/key_persistent")
    disk = Disk("disk_persistent", mount="/persistent", key=key, mapper="cryptroot")
    common = Fragment(
        "app",
        items=(
            Package("curl"),
            Package("jq"),
            Package("golang", role="build"),
            File("/etc/motd", "hello\n"),
            File("/etc/resolv.conf", "nameserver 1.1.1.1\n", stage="skeleton"),
            File("/usr/bin/blob", b"\x00\xff\x01", mode=0o755),
            Group("eth"),
            User("app", home="/home/app", groups=("eth",)),
            Unit("app.service", UNIT, enabled=True, after_init=True),
            Unit("ssh.service", enabled=False, masked=True),
            Hook("second", "postinst", "echo second", after=("first",)),
            Hook("first", "postinst", "echo first", env=(("STAGE", "1"),)),
            Init("announce", "echo up\n", priority=25, after=("mount-check",)),
            Init("mount-check", "test -d /persistent\n", priority=25, after=("disks",)),
            RuntimeTools(Git(TOOLS, "v1")),
            key,
            disk,
            Secrets(
                entries=(
                    Secret(
                        "token",
                        (SecretFile("/etc/app/token", owner="app"), SecretEnv("APP_TOKEN")),
                    ),
                ),
                store=disk,
            ),
            Build(
                "app",
                Git(APP_REPO, "main"),
                script="make",
                install=(
                    Install("out/app", "/usr/bin/app"),
                    Install("share", "/usr/share/app-data", mode=None, directory=True),
                ),
                packages=("make",),
                env=(("CFLAGS", "-O2"),),
            ),
            Debloat(extra_remove=("/usr/share/foo",)),
        ),
    )
    return Recipe(
        name="equivalence",
        base="debian/bookworm",
        common=common,
        variants=(
            Variant("default", target="qemu"),
            Variant(
                "azure",
                parent="default",
                target="azure",
                add=Fragment("azure-extras", items=(Package("waagent"),)),
                replace=(File("/etc/motd", "azure\n"),),
            ),
        ),
    )


def fluent_image() -> Image:
    """The fluent calls lowering issues for :func:`declarative_recipe`, written by hand."""
    tools = GitSource(TOOLS, "v1")
    key = KeySpec("key_persistent", output="/tmp/key_persistent")
    disk = DiskSpec("disk_persistent", device=None, key=key, mapper_name="cryptroot")
    img = Image(base="debian/bookworm", mkosi=MkosiOptions())
    img.install("curl", "jq").build_packages("golang")
    img.file("/etc/motd", content="hello\n")
    img.skeleton("/etc/resolv.conf", content="nameserver 1.1.1.1\n")
    img.file("/usr/bin/blob", content=b"\x00\xff\x01", mode="0755")
    img.group("eth", system=True)
    img.user("app", system=True, home="/home/app", groups=("eth",))
    img.file("/usr/lib/systemd/system/app.service", content=UNIT_AFTER_INIT)
    img.enable("app.service")
    img.disable("ssh.service").mask("ssh.service")
    img.shell("echo first", phase="postinst", env={"STAGE": "1"})
    img.shell("echo second", phase="postinst")
    img.apply(
        KeyGeneration(keys=(key,), source=tools),
        DiskEncryption(disks=(disk,), source=tools),
        SecretDelivery(
            secrets=(
                SecretSpec(
                    "token",
                    targets=(
                        SecretTarget.file("/etc/app/token", owner="app"),
                        SecretTarget.env("APP_TOKEN", scope="global"),
                    ),
                ),
            ),
            store_at=disk,
            source=tools,
        ),
    )
    img.build_from(
        SourceBuild(
            name="app",
            source=GitSource(APP_REPO, "main"),
            build=ScriptBuild(
                script="make", output="out/app", packages=("make",), env={"CFLAGS": "-O2"}
            ),
            install=(
                FluentInstall.file("out/app", "/usr/bin/app"),
                FluentInstall.tree("share", "/usr/share/app-data"),
            ),
        )
    )
    img.debloat(extra_remove_paths=("/usr/share/foo",))
    img.runtime_init("test -d /persistent\n", priority=25)
    img.runtime_init("echo up\n", priority=25)
    with img.profile("azure"):
        img.file("/etc/motd", content="azure\n")
        img.install("waagent")
        img.targets("azure")
        img.apply(AzurePlatform())
    return img


def modes(root: Path) -> dict[str, int]:
    return {str(path.relative_to(root)): os.lstat(path).st_mode for path in sorted(root.rglob("*"))}


def test_lowered_recipe_compiles_to_the_fluent_tree(tmp_path: Path) -> None:
    lowered = lower(declarative_recipe())
    fluent = fluent_image()

    assert lowered._recipe_payload(profile_names=PROFILES) == fluent._recipe_payload(
        profile_names=PROFILES
    )
    assert recipe_digest(lowered._recipe_payload(profile_names=PROFILES)) == recipe_digest(
        fluent._recipe_payload(profile_names=PROFILES)
    )

    lowered.compile(tmp_path / "lowered", profiles=PROFILES)
    fluent.compile(tmp_path / "fluent", profiles=PROFILES)
    diff = diff_trees(tmp_path / "fluent", tmp_path / "lowered")
    assert diff.is_clean, diff.unified()
    assert modes(tmp_path / "fluent") == modes(tmp_path / "lowered")
    assert lowered.check() == fluent.check()

    runtime_init = (tmp_path / "lowered/default/mkosi.extra/usr/bin/runtime-init").read_text()
    order = ["key-gen", "disk-setup", "test -d", "echo up", "secret-delivery"]
    assert [runtime_init.index(marker) for marker in order] == sorted(
        runtime_init.index(marker) for marker in order
    )


def test_lowering_maps_recipe_wide_fields(tmp_path: Path) -> None:
    config = tmp_path / "kernel.config"
    config.write_text("CONFIG_X=y\n")
    recipe = Recipe(
        "wide",
        Fragment(
            "c",
            items=(
                Kernel("6.1.2", Git("https://example.com/linux", "v6.1.2"), config=config),
                Setting("Output", "Seed", ("seed-1",)),
                Setting("Output", "OutputDirectory", ("build",)),
                Setting("Build", "PackageCacheDirectory", ("mkosi.cache",)),
                Setting("Build", "Environment", ("KERNEL_IMAGE", "FOO=bar")),
                Setting("Build", "WithNetwork", ("no",)),
                Setting("Build", "SandboxTrees", ("a:/b", "c:/d")),
            ),
        ),
        mirror="https://mirror/",
        tools_mirror="https://tools/",
        epoch=7,
        mkosi=Mkosi(layout="native", dialect="nethermind-v1"),
    )
    img = lower(recipe)
    assert (img.base, img.mirror, img.tools_tree_mirror) == (
        "debian/trixie",
        "https://mirror/",
        "https://tools/",
    )
    assert img.kernel == FluentKernel(
        version="6.1.2", config_file=config, tdx=True, source_repo="https://example.com/linux"
    )
    assert img.mkosi == MkosiOptions(
        seed="seed-1",
        output_directory="build",
        package_cache_directory="mkosi.cache",
        environment={"FOO": "bar", "SOURCE_DATE_EPOCH": "7"},
        environment_passthrough=("KERNEL_IMAGE",),
        with_network=False,
        sandbox_trees=("a:/b", "c:/d"),
        emit_mode="native_profiles",
    )
    assert img.reproducible
    assert not lower(Recipe("r", Fragment("c"), epoch=None)).reproducible


@pytest.mark.parametrize(
    "item",
    [
        Setting("Distribution", "Mirror", ("https://x",)),
        Setting("Output", "Seed", ("a", "b")),
        Setting("Build", "WithNetwork", ("maybe",)),
        Kernel("6.1", Git("https://example.com/linux", "main")),
    ],
)
def test_lowering_rejects_unmapped_recipe_wide_items(item: Setting | Kernel) -> None:
    with pytest.raises(ValidationError):
        lower(Recipe("r", Fragment("c", items=(item,))))


def test_standalone_variant_gets_only_its_own_declarations() -> None:
    recipe = Recipe(
        "r",
        Fragment("c", items=(Package("curl"), Debloat(enabled=False))),
        variants=(
            Variant("default"),
            Variant("rescue", parent=None, add=Fragment("r", items=(Package("busybox"),))),
        ),
    )
    img = lower(recipe)
    rescue = img.state.effective_profile("rescue")
    assert img.state.profiles["rescue"].extends is None
    assert rescue.packages == {"busybox"}
    assert rescue.debloat.enabled  # not the default profile's disabled debloat


@pytest.mark.parametrize(
    ("variants", "message"),
    [
        (
            (Variant("default"), Variant("a", target="azure"), Variant("b", parent="a")),
            "only 'base'",
        ),
        (
            (
                Variant("default", add=Fragment("d", items=(Package("d"),))),
                Variant("sibling"),
            ),
            "lacks Package",
        ),
        ((Variant("default"), Variant("slim", remove=(Package("curl"),))), "lacks Package"),
        (
            (Variant("default"), Variant("v", replace=(Hook("h", "build", "other"),))),
            "cannot replace",
        ),
        (
            (Variant("default"), Variant("v", add=Fragment("k", items=(Key("k2"),)))),
            "keys, disks or secrets",
        ),
        (
            (
                Variant("default"),
                Variant("v", add=Fragment("s", items=(Setting("Output", "Seed", ("x",)),))),
            ),
            "recipe-wide",
        ),
    ],
)
def test_unsupported_variant_shapes_raise(variants: tuple[Variant, ...], message: str) -> None:
    common = Fragment("c", items=(Package("curl"), Hook("h", "build", "make"), Key("k1")))
    with pytest.raises(ValidationError, match=message):
        lower(Recipe("r", common, variants=variants))


def test_lowering_selected_variants_keeps_the_default() -> None:
    recipe = Recipe(
        "r",
        Fragment("c", items=(Package("curl"),)),
        variants=(Variant("default"), Variant("a"), Variant("b")),
    )
    assert lower(recipe, variants=("b",)).profile_names == ("b", "default")


def test_directories_units_from_paths_and_reemitted_units(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "tool").write_text("#!/bin/sh\n")
    (tree / "bin" / "tool").chmod(0o755)
    (tree / "notes.txt").write_text("skip me\n")
    unit_file = tmp_path / "worker.service"
    unit_file.write_text("[Unit]\nDescription=Worker\n\n[Service]\nExecStart=/bin/true\n")
    recipe = Recipe(
        "r",
        Fragment(
            "c",
            items=(
                Directory("/opt/tree", tree, exclude=("*.txt",)),
                Directory("/etc/skel", tree, stage="skeleton", mode=0o600),
                Unit("worker.service", unit_file, enabled=True, after_init=True),
            ),
        ),
        variants=(
            Variant("default"),
            Variant("boot", add=Fragment("i", items=(Init("hello", "echo hi\n"),))),
        ),
    )
    img = lower(recipe)
    default = img.state.effective_profile("default")
    assert [(f.path, f.mode) for f in default.files] == [
        ("/opt/tree/bin/tool", "0755"),
        ("/usr/lib/systemd/system/worker.service", "0644"),
    ]
    assert [(f.path, f.mode) for f in default.skeleton_files] == [
        ("/etc/skel/bin/tool", "0600"),
        ("/etc/skel/notes.txt", "0600"),
    ]
    worker = "/usr/lib/systemd/system/worker.service"
    default_unit = next(f for f in default.files if f.path == worker)
    assert "runtime-init" not in str(default_unit.content)
    # The boot variant adds an init step, so its copy of the unit waits for it.
    boot_unit = next(f for f in img.state.effective_profile("boot").files if f.path == worker)
    assert str(boot_unit.content).startswith(
        "[Unit]\nDescription=Worker\nAfter=runtime-init.service\nRequires=runtime-init.service\n"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "[Unit]\nDescription=x\n\n[Service]\nExecStart=/x\n",
            "[Unit]\nDescription=x\nAfter=runtime-init.service\nRequires=runtime-init.service\n"
            "\n[Service]\nExecStart=/x\n",
        ),
        (
            "[Unit]\nRequires=a.service\nAfter=a.service\n",
            "[Unit]\nRequires=runtime-init.service a.service\n"
            "After=runtime-init.service a.service\n",
        ),
        (
            "[Unit]\nRequires=a.service\n",
            "[Unit]\nAfter=runtime-init.service\nRequires=runtime-init.service a.service\n",
        ),
        (
            "[Unit]\nAfter=runtime-init.service\n",
            "[Unit]\nAfter=runtime-init.service\nRequires=runtime-init.service\n",
        ),
        (
            "[Service]\nExecStart=/x\n",
            "[Unit]\nAfter=runtime-init.service\nRequires=runtime-init.service\n\n"
            "[Service]\nExecStart=/x\n",
        ),
    ],
)
def test_inject_after_init(text: str, expected: str) -> None:
    assert inject_after_init(text) == expected


def test_runtime_tools_default_and_key_handoff() -> None:
    key = Key("k")  # no output: the disk tool reads it from the environment
    recipe = Recipe("r", Fragment("c", items=(key, Disk("d", "/data", key=key, device="/dev/vdb"))))
    img = lower(recipe)
    files = img.state.profiles["default"].files
    config = next(f.content for f in files if f.path == "/etc/tdx/disk-setup.yaml")
    assert 'encryption_key: "k"' in str(config) and 'pattern: "/dev/vdb"' in str(config)
    assert "key-generation" in img.source_builds()
    assert img.source_builds()["key-generation"].source.url.endswith("tundra-tools.git")
