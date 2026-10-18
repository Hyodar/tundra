"""Lowering: a declarative Recipe as the compiler's RecipeState, variants as profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm._modules.init import runtime_init_order
from tundravm._options import MkosiOptions
from tundravm.check import check
from tundravm.declarative import (
    RUNTIME_INIT,
    BootStep,
    Build,
    Debian,
    Debloat,
    Disk,
    ExternalDelivery,
    File,
    FileTree,
    Fragment,
    Git,
    Group,
    Hook,
    Http,
    Install,
    Kernel,
    Key,
    Mkosi,
    Package,
    Ready,
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
    lock,
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
            Unit("app.service", UNIT, enabled=True, needs=(RUNTIME_INIT,)),
            Unit("ssh.service", enabled=False, masked=True),
            Hook("second", "postinst", "echo second", after=("first",)),
            Hook("first", "postinst", "echo first", env={"STAGE": "1"}),
            BootStep("announce", "echo up\n", after=(Ready("mount-check"),)),
            BootStep("mount-check", "test -d /persistent\n", after=(disk.ready,)),
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
                delivery=ExternalDelivery("app-secrets"),
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
                env={"CFLAGS": "-O2"},
            ),
            Debloat(extra_remove=("/usr/share/foo",)),
        ),
    )
    return Recipe(
        name="equivalence",
        platform=Debian("bookworm", boot=None),
        items=(common,),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "azure",
                inherits="default",
                outputs=("azure",),
                add=Fragment("azure-extras", items=(Package("waagent"),)),
                changes=(File.ref("/etc/motd").edit(content="azure\n"),),
            ),
        ),
    )


EQUIVALENCE_TREE = "50efe4c0bcd7d6829727380464b55522dc4d2e50d9407bc9158a3577ad125482"
EQUIVALENCE_RECIPE = "e650653c311025468325f44534964be348eef12f66437c0af9dbaef97c63b680"
"""The tree and recipe digests :func:`declarative_recipe` lowered to through the retired
fluent ``Image`` calls, recorded when lowering started writing ``RecipeState`` directly
(re-recorded when current-dialect build hooks gained their ``# tundravm build:`` line, and
when runtime-init became a graph of boot steps, one unit per step, locked by step name, and
when debloat's default keep list gained ``timers.target``, and when the azure provisioning
unit became a typed ``Task`` and runtime-init step units render through the typed table, and
when reproducible current-dialect finalize scripts gained the deterministic accounts step, and
when the azure provisioning unit gained ``ConditionVirtualization=microsoft`` and cloud glue
units moved first in a profile's unit order)."""


def test_lowered_recipe_compiles_to_the_recorded_tree(tmp_path: Path) -> None:
    lowered = lower(declarative_recipe())

    assert lowered.select(PROFILES).digest() == EQUIVALENCE_RECIPE
    assert compile(declarative_recipe()).digest == EQUIVALENCE_TREE
    assert [(d.severity, d.code, d.variant, d.subject) for d in check(lowered)] == [
        ("error", "kernel-missing", "default", ""),
        ("warning", "disk-auto-format", "default", "disk_persistent"),
        ("warning", "source-unpinned", "default", "app"),
        ("warning", "source-unpinned", "default", "disk-encryption"),
        ("warning", "source-unpinned", "default", "key-generation"),
        ("warning", "source-unpinned", "default", "secret-delivery"),
    ]

    emit(lowered.select(PROFILES), tmp_path / "lowered")
    extra = tmp_path / "lowered/default/mkosi.extra"
    assert not (extra / "usr/bin/runtime-init").exists()
    units = extra / "usr/lib/systemd/system"
    check_unit = (units / "runtime-init-mount-check.service").read_text()
    assert "After=network-online.target runtime-init-disks.service\n" in check_unit
    assert "Requires=runtime-init-disks.service\n" in check_unit
    assert "ExecStart=/usr/lib/runtime-init/mount-check\n" in check_unit
    announce = (units / "runtime-init-announce.service").read_text()
    assert "Requires=runtime-init-mount-check.service\n" in announce
    assert (extra / "usr/lib/runtime-init/announce").read_text().endswith("echo up\n")
    assert (units / "app.service").read_text() == UNIT_AFTER_INIT
    order = [entry.name for entry in runtime_init_order(lowered.init_scripts("default"))]
    assert order == ["keys", "disks", "secrets", "mount-check", "announce"]


def test_runtime_init_order_follows_after_and_enters_the_recipe_digest(tmp_path: Path) -> None:
    def ordered(first: str, second: str) -> Recipe:
        return Recipe(
            "inits",
            items=(
                BootStep(second, f"echo {second}\n", after=(Ready(first),)),
                BootStep(first, f"echo {first}\n"),
            ),
            mkosi=Mkosi(dialect="nethermind-v1"),
        )

    a_first, b_first = ordered("a", "b"), ordered("b", "a")
    scripts = []
    for name, recipe in (("a", a_first), ("b", b_first)):
        emit(lower(recipe), tmp_path / name)
        scripts.append((tmp_path / name / "default/mkosi.extra/usr/bin/runtime-init").read_text())
    assert scripts[0].index("echo a") < scripts[0].index("echo b")
    assert scripts[1].index("echo b") < scripts[1].index("echo a")
    assert lower(a_first).digest() != lower(b_first).digest()
    sections = [lock(r).lockfile.sections["init_scripts"] for r in (a_first, b_first)]
    assert sections[0] != sections[1]


def test_current_dialect_runs_each_step_as_its_own_unit(tmp_path: Path) -> None:
    first = BootStep("first", "echo first\n")
    recipe = Recipe(
        "inits",
        items=(BootStep("second", "echo second\n", after=(first.ready,)), first),
    )
    emit(lower(recipe), tmp_path)
    extra = tmp_path / "default/mkosi.extra"
    units = extra / "usr/lib/systemd/system"
    assert (extra / "usr/lib/runtime-init/first").read_text() == (
        "#!/bin/bash\nset -euo pipefail\n\necho first\n"
    )
    assert (
        "Requires=runtime-init-first.service\n"
        in (units / "runtime-init-second.service").read_text()
    )
    aggregate = (units / "runtime-init.service").read_text()
    assert "After=runtime-init-first.service runtime-init-second.service\n" in aggregate
    assert "ExecStart=/bin/true\n" in aggregate and "WantedBy=minimal.target" in aggregate


def test_lowering_maps_recipe_wide_fields(tmp_path: Path) -> None:
    config = tmp_path / "kernel.config"
    config.write_text("CONFIG_X=y\n")
    recipe = Recipe(
        "wide",
        items=(
            Kernel("6.1.2", Git("https://example.com/linux", "v6.1.2"), config=config),
            Setting("Output", "Seed", ("seed-1",)),
            Setting("Output", "OutputDirectory", ("build",)),
            Setting("Build", "PackageCacheDirectory", ("mkosi.cache",)),
            Setting("Build", "Environment", ("KERNEL_IMAGE", "FOO=bar")),
            Setting("Build", "WithNetwork", ("no",)),
            Setting("Build", "SandboxTrees", ("a:/b", "c:/d")),
        ),
        platform=Debian(
            "trixie", mirror="https://mirror/", tools_mirror="https://tools/", epoch=7, boot=None
        ),
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
    assert not lower(Recipe("r", platform=Debian("trixie", epoch=None, boot=None))).reproducible


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
        lower(Recipe("r", items=(item,)))


def test_standalone_variant_gets_only_its_own_declarations() -> None:
    recipe = Recipe(
        "r",
        items=(Package("curl"), Debloat(enabled=False)),
        variants=(
            Variant("default"),
            Variant("rescue", inherits=None, add=Fragment("r", items=(Package("busybox"),))),
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
        (Variant("default"), Variant("a", outputs=("azure",)), Variant("b", inherits="a")),
        (Variant("default", add=Fragment("d", items=(Package("d"),))), Variant("sibling")),
        (Variant("default"), Variant("slim", changes=(Package.ref("curl").remove(),))),
        (Variant("default"), Variant("v", changes=(Hook.ref("h").edit(script="other"),))),
        (Variant("default"), Variant("v", add=Fragment("k", items=(Key("k2"),)))),
    ],
    ids=["chain", "sibling-of-base", "remove", "edit-hook", "child-keys"],
)
def test_shapes_the_profile_merge_cannot_express_lower_standalone(
    variants: tuple[Variant, ...],
) -> None:
    common = Fragment("c", items=(Package("curl"), Hook("h", "build", "make"), Key("k1")))
    recipe = Recipe("r", items=(common,), variants=variants)
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
        items=(Package("curl"),),
        variants=(Variant("default"), Variant("a", outputs=("azure",)), Variant("b", inherits="a")),
    )
    img = lower(recipe)
    assert img.state.effective_profile("b").output_targets == ("azure",)


def test_variant_with_its_own_settings_lowers_standalone() -> None:
    common = Fragment("c", items=(Package("curl"),))
    variants = (
        Variant("default"),
        Variant("v", add=Fragment("s", items=(Setting("Output", "Seed", ("x",)),))),
    )
    img = lower(Recipe("r", items=(common,), variants=variants))
    assert img.state.profiles["v"].extends is None
    assert img.mkosi_for("v").seed == "x" and img.mkosi.seed is None


def test_lowering_selected_variants_keeps_the_default() -> None:
    recipe = Recipe(
        "r",
        items=(Package("curl"),),
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
        items=(
            FileTree("/opt/tree", tree, exclude=("*.txt",)),
            FileTree("/etc/skel", tree, stage="skeleton", mode=0o600),
            Unit("worker.service", unit_file, enabled=True, needs=(RUNTIME_INIT,)),
        ),
        variants=(
            Variant("default"),
            Variant("boot", add=Fragment("i", items=(BootStep("hello", "echo hi\n"),))),
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


def test_inject_after_init_prepends_several_units_in_order() -> None:
    text = "[Unit]\nAfter=a.service\n"
    assert inject_after_init(text, ("runtime-init-x.service", "runtime-init-y.service")) == (
        "[Unit]\nAfter=runtime-init-x.service runtime-init-y.service a.service\n"
        "Requires=runtime-init-x.service runtime-init-y.service\n"
    )


def test_runtime_tools_default_and_key_handoff() -> None:
    key = Key("k")  # no output: the disk tool reads it from the environment
    recipe = Recipe("r", items=(key, Disk("d", "/data", key=key, device="/dev/vdb")))
    img = lower(recipe)
    files = img.state.profiles["default"].files
    config = next(f.content for f in files if f.path == "/etc/tdx/disk-setup.yaml")
    assert 'encryption_key: "k"' in str(config) and 'pattern: "/dev/vdb"' in str(config)
    assert "key-generation" in img.source_builds()
    assert img.source_builds()["key-generation"].source.url.endswith("tundra-tools.git")


# ── Declarations added for the declarative-only API ─────────────────────


def _one(*items: object, epoch: int | None = 0, **recipe: object) -> Recipe:
    platform = Debian("trixie", epoch=epoch, boot=None)
    return Recipe("r", items=items, platform=platform, **recipe)  # type: ignore[arg-type]


def test_service_lowers_to_the_generated_unit() -> None:
    from tundravm.declarative import Service

    service = Service(
        "app",
        "/usr/bin/app --serve",
        user="app",
        env={"MODE": "prod"},
        limits={"NOFILE": 1048576},
        restart="always",
        security="strict",
    )
    img = lower(_one(service, BootStep("ready", "true")))
    (spec,) = img.state.effective_profile("default").services[:1]
    assert spec.name == "app"
    assert spec.command == ("/usr/bin/app", "--serve")
    assert spec.env == {"MODE": "prod"} and spec.limits == {"NOFILE": "1048576"}
    assert spec.security_profile == "strict" and spec.restart == "always"


def test_service_needs_picks_the_runtime_init_units_it_waits_for(tmp_path: Path) -> None:
    from tundravm.declarative import Service

    disk = Disk("d", "/data", device="/dev/vdb")
    ready = BootStep("ready", "true", after=(disk.ready,))
    services = (
        Service("early", "/usr/bin/early", needs=()),
        Service("late", "/usr/bin/late"),
        Service("data", "/usr/bin/data", needs=(disk.ready, ready.ready)),
    )
    current, historical = (
        _one(disk, ready, *services),
        _one(disk, ready, *services, mkosi=Mkosi(dialect="nethermind-v1")),
    )
    for name, recipe in (("current", current), ("historical", historical)):
        emit(lower(recipe), tmp_path / name)
    units = Path("default/mkosi.extra/usr/lib/systemd/system")
    tree = tmp_path / "current" / units
    assert "runtime-init" not in (tree / "early.service").read_text()
    assert "After=runtime-init.service\n" in (tree / "late.service").read_text()
    data = (tree / "data.service").read_text()
    assert "After=runtime-init-disks.service runtime-init-ready.service\n" in data
    assert "Requires=runtime-init-disks.service runtime-init-ready.service\n" in data
    old = tmp_path / "historical" / units
    assert "Requires=runtime-init.service\n" in (old / "data.service").read_text()
    assert "runtime-init" not in (old / "early.service").read_text()


def test_inherited_service_and_unit_wait_for_a_child_variants_first_step(
    tmp_path: Path,
) -> None:
    from tundravm.declarative import Service

    recipe = _one(
        Service("app", "/usr/bin/app"),
        Service("early", "/usr/bin/early", needs=()),
        Unit("worker.service", UNIT, enabled=True, needs=(RUNTIME_INIT,)),
        variants=(
            Variant("default"),
            Variant("boot", add=Fragment("i", items=(BootStep("hello", "echo hi\n"),))),
        ),
    )
    emit(lower(recipe).select(("default", "boot")), tmp_path)
    units = Path("mkosi.extra/usr/lib/systemd/system")
    default, boot = tmp_path / "default" / units, tmp_path / "boot" / units
    assert "runtime-init" not in (default / "app.service").read_text()
    for unit in ("app.service", "worker.service"):
        assert "After=runtime-init.service" in (boot / unit).read_text()
        assert "Requires=runtime-init.service" in (boot / unit).read_text()
    assert "runtime-init" not in (boot / "early.service").read_text()  # needs nothing
    postinst = (tmp_path / "boot" / "scripts" / "06-postinst.sh").read_text()
    assert postinst.count("systemctl enable app.service\n") == 1


def test_packaged_unit_needs_become_an_ordering_dropin() -> None:
    disk = Disk("d", "/data", device="/dev/vdb")
    recipe = _one(
        disk,
        Unit("database.service", enabled=True, needs=[disk.ready]),
        Unit("cron", enabled=True, needs=(RUNTIME_INIT,)),
    )
    files = {f.path: f.content for f in lower(recipe).state.effective_profile("default").files}
    dropin = "/usr/lib/systemd/system/database.service.d/10-tundravm-needs.conf"
    waits = "runtime-init-disks.service"
    assert files[dropin] == f"[Unit]\nAfter={waits}\nRequires={waits}\n"
    cron = files["/usr/lib/systemd/system/cron.service.d/10-tundravm-needs.conf"]
    assert "After=runtime-init.service\n" in str(cron)
    alone = _one(Unit("database.service", enabled=True, needs=(RUNTIME_INIT,)))
    assert not lower(alone).state.effective_profile("default").files  # no steps, no wait


def test_external_delivery_ready_waits_for_its_unit_or_path(tmp_path: Path) -> None:
    from tundravm.declarative import Service
    from tundravm.declarative.resolve import lint as resolution_lint

    entry = SecretFile("/run/app/token")
    agent = ExternalDelivery("vault agent", supports=("file",), unit="vault-agent.service")
    marker = ExternalDelivery("writer", supports=("file",), ready_path="/run/app/.ready")
    for delivery, script in (
        (agent, "systemctl start vault-agent.service\n"),
        (marker, "until [ -e /run/app/.ready ]; do sleep 1; done\n"),
    ):
        recipe = _one(
            Secrets(entries=(Secret("token", (entry,)),), delivery=delivery),
            Service("app", "/usr/bin/app", needs=(delivery.ready,)),
            Service("ssh-user", "/usr/bin/u", needs=(Secrets().ready,)),
        )
        assert not resolution_lint(recipe)
        out = tmp_path / delivery.step
        emit(lower(recipe), out)
        root = out / "default" / "mkosi.extra"
        assert (root / f"usr/lib/runtime-init/{delivery.step}").read_text().endswith(script)
        units = root / "usr/lib/systemd/system"
        waits = f"runtime-init-{delivery.step}.service"
        assert f"After={waits}\n" in (units / "app.service").read_text()
        assert "After=runtime-init-secrets.service\n" in (units / "ssh-user.service").read_text()
    assert agent.step == "delivery-vault-agent"
    unwired = _one(Secrets(entries=(Secret("token", (entry,)),), delivery=ExternalDelivery("x")))
    (finding,) = resolution_lint(unwired)
    assert (finding.code, finding.severity) == ("external-delivery-unwired", "warning")
    with pytest.raises(ValidationError, match="not both"):
        ExternalDelivery("x", unit="a.service", ready_path="/run/x")


def test_service_rejects_bad_fields() -> None:
    from tundravm.declarative import Service

    with pytest.raises(ValidationError, match="restart"):
        Service("x", "/bin/x", restart="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="limit"):
        Service("x", "/bin/x", limits={"NOFILE": True})
    with pytest.raises(ValidationError, match="limits= must be a mapping"):
        Service("x", "/bin/x", limits=(("NOFILE", 1),))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match=".service"):
        Service("x.socket", "/bin/x")


def test_template_renders_in_extra_and_skeleton() -> None:
    from tundravm.declarative import Template

    img = lower(
        _one(
            Template("/etc/app.conf", "port={port}\n", variables={"port": 8080}),
            Template("/etc/boot.conf", "id={id}\n", variables={"id": "x"}, stage="skeleton"),
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
        items=(Setting("Build", "BuildSources", ("../src:app", "../lib")),),
    )
    profile = lower(recipe).state.effective_profile("default")
    assert profile.build_sources == [("../src", "app"), ("../lib", "")]


def test_debloat_extra_units_and_per_variant_paths() -> None:
    debloat = Debloat(keep_units_extra=("app.service",), keep_paths_by_variant={"x": ("/opt",)})
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
        items=(Package("curl"),),
        variants=(Variant("default"), Variant("cloud", outputs=("azure", "gcp"))),
    )
    assert resolve(recipe, variant="cloud").outputs == ("azure", "gcp")
    img = lower(recipe)
    assert img.state.effective_profile("cloud").output_targets == ("azure", "gcp")
    with pytest.raises(ValidationError, match="must be a sequence of targets"):
        Variant("x", outputs="qemu")  # type: ignore[arg-type]


def _chained(layout: str) -> Recipe:
    from tundravm.declarative import Mkosi

    return Recipe(
        "r",
        items=(Package("curl"), File("/etc/motd", "base\n")),
        variants=(
            Variant("default"),
            Variant("cloud", outputs=("azure",), add=Fragment("a", items=(Package("waagent"),))),
            Variant(
                "slim",
                inherits="cloud",
                changes=(
                    Package.ref("curl").remove(),
                    File.ref("/etc/motd").edit(content="slim\n"),
                ),
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
