"""Lowering: a declarative Recipe as the compiler's RecipeState, variants as profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm._options import MkosiOptions
from tundravm.check import check
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
    Http,
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
    compile,
    lower,
    resolve,
)
from tundravm.declarative._compile import emit
from tundravm.declarative.lower import inject_after_init
from tundravm.errors import ValidationError
from tundravm.models import Kernel as FluentKernel

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


EQUIVALENCE_TREE = "9f9e6b98aac49ff870e81b97801bcd01b742ec6575800773550e628050c8ff77"
EQUIVALENCE_RECIPE = "8b91d4ae612c237aa879c98cf87be2348c5e5c8ff167f08a97433111314c4d04"
"""The tree and recipe digests :func:`declarative_recipe` lowered to through the retired
fluent ``Image`` calls, recorded when lowering started writing ``RecipeState`` directly."""


def test_lowered_recipe_compiles_to_the_recorded_tree(tmp_path: Path) -> None:
    lowered = lower(declarative_recipe())

    assert lowered.select(PROFILES).digest() == EQUIVALENCE_RECIPE
    assert compile(declarative_recipe()).digest == EQUIVALENCE_TREE
    assert [(d.level, d.code, d.profile, d.subject) for d in check(lowered)] == [
        ("error", "kernel-missing", "default", None),
        ("warning", "disk-auto-format", "default", "disk_persistent"),
        ("warning", "init-priority-collision", "default", "priority 25"),
        ("warning", "source-unpinned", "default", "app"),
        ("warning", "source-unpinned", "default", "disk-encryption"),
        ("warning", "source-unpinned", "default", "key-generation"),
        ("warning", "source-unpinned", "default", "secret-delivery"),
    ]

    emit(lowered.select(PROFILES), tmp_path / "lowered")
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
        dialect="nethermind-v1",
    )
    assert img.reproducible
    assert not lower(Recipe("r", Fragment("c"), epoch=None)).reproducible


@pytest.mark.parametrize(
    "item",
    [
        Setting("Distribution", "Mirror", ("https://x",)),
        Setting("Output", "Seed", ("a", "b")),
        Setting("Build", "WithNetwork", ("maybe",)),
        Kernel("6.1", Http("https://example.com/linux-6.1.tar.xz")),
    ],
)
def test_lowering_rejects_inexpressible_settings_and_kernels(item: Setting | Kernel) -> None:
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
    "variants",
    [
        (Variant("default"), Variant("a", target="azure"), Variant("b", parent="a")),
        (Variant("default", add=Fragment("d", items=(Package("d"),))), Variant("sibling")),
        (Variant("default"), Variant("slim", remove=(Package("curl"),))),
        (Variant("default"), Variant("v", replace=(Hook("h", "build", "other"),))),
        (Variant("default"), Variant("v", add=Fragment("k", items=(Key("k2"),)))),
    ],
    ids=["chain", "sibling-of-base", "remove", "replace-hook", "child-keys"],
)
def test_shapes_the_profile_merge_cannot_express_lower_standalone(
    variants: tuple[Variant, ...],
) -> None:
    common = Fragment("c", items=(Package("curl"), Hook("h", "build", "make"), Key("k1")))
    recipe = Recipe("r", common, variants=variants)
    name = variants[-1].name
    img = lower(recipe)
    assert img.state.profiles[name].extends is None
    resolved = resolve(recipe, variant=name)
    packages = {i.name for i in resolved.items if isinstance(i, Package) and i.role == "runtime"}
    assert packages <= img.state.effective_profile(name).packages
    if "curl" not in packages:
        assert "curl" not in img.state.effective_profile(name).packages


def test_chained_variant_inherits_its_parents_target() -> None:
    recipe = Recipe(
        "r",
        Fragment("c", items=(Package("curl"),)),
        variants=(Variant("default"), Variant("a", target="azure"), Variant("b", parent="a")),
    )
    img = lower(recipe)
    assert img.state.effective_profile("b").output_targets == ("azure",)


def test_variant_with_its_own_settings_lowers_standalone() -> None:
    common = Fragment("c", items=(Package("curl"),))
    variants = (
        Variant("default"),
        Variant("v", add=Fragment("s", items=(Setting("Output", "Seed", ("x",)),))),
    )
    img = lower(Recipe("r", common, variants=variants))
    assert img.state.profiles["v"].extends is None
    assert img.mkosi_for("v").seed == "x" and img.mkosi.seed is None


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


# ── Declarations added for the declarative-only API ─────────────────────


def _one(*items: object, **recipe: object) -> Recipe:
    return Recipe("r", Fragment("c", items=items), **recipe)  # type: ignore[arg-type]


def test_service_lowers_to_the_generated_unit() -> None:
    from tundravm.declarative import Init, Service

    service = Service(
        "app",
        "/usr/bin/app --serve",
        user="app",
        env=(("MODE", "prod"),),
        limits=(("NOFILE", 1048576),),
        restart="always",
        security="strict",
    )
    img = lower(_one(service, Init("ready", "true")))
    (spec,) = img.state.effective_profile("default").services[:1]
    assert spec.name == "app"
    assert spec.command == ("/usr/bin/app", "--serve")
    assert spec.env == {"MODE": "prod"} and spec.limits == {"NOFILE": "1048576"}
    assert spec.security_profile == "strict" and spec.restart == "always"


def test_service_after_init_false_skips_the_runtime_init_dependency(tmp_path: Path) -> None:
    from tundravm.declarative import Init, Service

    recipe = _one(
        Service("early", "/usr/bin/early", after_init=False),
        Service("late", "/usr/bin/late"),
        Init("ready", "true"),
    )
    emit(lower(recipe), tmp_path / "tree")
    units = tmp_path / "tree" / "default" / "mkosi.extra" / "usr" / "lib" / "systemd" / "system"
    assert "runtime-init.service" not in (units / "early.service").read_text()
    assert "After=runtime-init.service" in (units / "late.service").read_text()


def test_service_rejects_bad_fields() -> None:
    from tundravm.declarative import Service

    with pytest.raises(ValidationError, match="restart"):
        Service("x", "/bin/x", restart="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="limit"):
        Service("x", "/bin/x", limits=(("NOFILE", 1), ("NOFILE", 2)))
    with pytest.raises(ValidationError, match=".service"):
        Service("x.socket", "/bin/x")


def test_template_renders_in_extra_and_skeleton() -> None:
    from tundravm.declarative import Template

    img = lower(
        _one(
            Template("/etc/app.conf", "port={port}\n", variables=(("port", 8080),)),
            Template("/etc/boot.conf", "id={id}\n", variables=(("id", "x"),), stage="skeleton"),
        )
    )
    profile = img.state.effective_profile("default")
    (entry,) = profile.templates
    assert entry.path == "/etc/app.conf" and entry.rendered == "port=8080\n"
    skeleton = next(f for f in profile.skeleton_files if f.path == "/etc/boot.conf")
    assert skeleton.content == "id=x\n"


def test_template_missing_variable_raises() -> None:
    from tundravm.declarative import Template

    with pytest.raises(ValidationError, match="placeholder"):
        lower(_one(Template("/etc/a", "{missing}", stage="skeleton")))


def test_build_sources_setting_mounts_per_variant() -> None:
    recipe = Recipe(
        "r",
        Fragment("c", items=(Setting("Build", "BuildSources", ("../src:app", "../lib")),)),
    )
    profile = lower(recipe).state.effective_profile("default")
    assert profile.build_sources == [("../src", "app"), ("../lib", "")]


def test_debloat_extra_units_and_per_variant_paths() -> None:
    debloat = Debloat(keep_units_extra=("app.service",), keep_paths_by_variant=(("x", ("/opt",)),))
    config = lower(_one(debloat)).state.effective_profile("default").debloat
    assert config.extra_keep_units == ("app.service",)
    assert config.paths_skip_for_profiles == (("x", ("/opt",)),)


def test_mkosi_fields_and_policy_reach_the_compiler() -> None:
    from tundravm.declarative import Mkosi, Policy

    policy = Policy(require_frozen_lock=True)
    img = lower(
        _one(
            Package("curl"),
            mkosi=Mkosi(init_script="#!/bin/sh\n", version_script=True, cloud_postoutput=False),
            policy=policy,
        )
    )
    assert img.mkosi.init_script == "#!/bin/sh\n"
    assert img.mkosi.generate_version_script and not img.mkosi.generate_cloud_postoutput
    assert img.policy == policy


def test_strip_os_release_overrides_the_epoch_default() -> None:
    from tundravm.declarative import Mkosi

    def strips(recipe: Recipe) -> bool:
        hooks = lower(recipe).state.effective_profile("default").phases.get("finalize", [])
        return any("IMAGE_VERSION" in c.argv[0] for c in hooks)

    assert strips(_one(Package("a")))
    assert not strips(_one(Package("a"), mkosi=Mkosi(strip_os_release=False)))
    assert strips(_one(Package("a"), epoch=None, mkosi=Mkosi(strip_os_release=True)))
    assert not strips(_one(Package("a"), epoch=None))


def test_variant_with_several_targets() -> None:
    recipe = Recipe(
        "r",
        Fragment("c", items=(Package("curl"),)),
        variants=(Variant("default"), Variant("cloud", targets=("azure", "gcp"))),
    )
    assert resolve(recipe, variant="cloud").targets == ("azure", "gcp")
    img = lower(recipe)
    assert img.state.effective_profile("cloud").output_targets == ("azure", "gcp")
    with pytest.raises(ValidationError, match="both target= and targets="):
        Variant("x", target="qemu", targets=("azure",))


def _chained(layout: str) -> Recipe:
    from tundravm.declarative import Mkosi

    return Recipe(
        "r",
        Fragment("c", items=(Package("curl"), File("/etc/motd", "base\n"))),
        variants=(
            Variant("default"),
            Variant("cloud", target="azure", add=Fragment("a", items=(Package("waagent"),))),
            Variant(
                "slim",
                parent="cloud",
                remove=(Package("curl"),),
                replace=(File("/etc/motd", "slim\n"),),
            ),
        ),
        mkosi=Mkosi(layout=layout),  # type: ignore[arg-type]
    )


def test_native_layout_rejects_standalone_variants() -> None:
    with pytest.raises(ValidationError, match="layout='directories'"):
        lower(_chained("native"))


def test_chained_variant_compiles_its_resolved_declarations() -> None:
    from tundravm.declarative import compile

    recipe = _chained("directories")
    tree = compile(recipe)
    assert "slim" in tree.variants
    slim = lower(recipe).state.effective_profile("slim")
    assert "curl" not in slim.packages and "waagent" in slim.packages
    assert slim.output_targets == ("azure",)
