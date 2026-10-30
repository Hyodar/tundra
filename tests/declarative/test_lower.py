"""Lowering: a declarative Recipe as the compiler's RecipeState, variants as additions to the
payload (profile overlays or cloud wrappers)."""

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
    disk = Disk(
        "disk_persistent", mount="/persistent", key=key, mapper="cryptroot", format="on_fail"
    )
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


EQUIVALENCE_TREE = "19ac76421381bbf79aa31f0745d16275ea78833abce73f9b879106b0bddecaf8"
EQUIVALENCE_RECIPE = "51394ca2d4424ed7676cf241e482572587f1322435922ff13ae2ac21f062c9a6"
"""The tree and recipe digests :func:`declarative_recipe` lowers to, recorded when the tree
became mkosi-native (one payload, its cloud glue for every target, ``azure`` a profile
overlay)."""


def test_lowered_recipe_compiles_to_the_recorded_tree(tmp_path: Path) -> None:
    lowered = lower(declarative_recipe())

    assert lowered.select(PROFILES).digest() == EQUIVALENCE_RECIPE
    assert compile(declarative_recipe()).digest == EQUIVALENCE_TREE
    assert [(d.severity, d.code, d.variant, d.subject) for d in check(lowered)] == [
        ("error", "disk-device-guessed", "default", "disk_persistent"),
        ("error", "kernel-missing", "default", ""),
        ("warning", "source-unpinned", "default", "app"),
        ("warning", "source-unpinned", "default", "disk-encryption"),
        ("warning", "source-unpinned", "default", "key-generation"),
        ("warning", "source-unpinned", "default", "secret-delivery"),
    ]

    emit(lowered.select(PROFILES), tmp_path / "lowered")
    extra = tmp_path / "lowered/payload/mkosi.extra"
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
    # azure adds a package and edits a file: a profile overlay, built with its wrapper
    overlay = tmp_path / "lowered/payload/mkosi.profiles/azure"
    assert (overlay / "mkosi.extra/etc/motd").read_text() == "azure\n"
    assert "    waagent\n" in (overlay / "mkosi.conf").read_text()
    assert (tmp_path / "lowered/mkosi.images/azure/mkosi.conf").is_file()
    assert (lowered.overlays, lowered.clouds("azure")) == (("azure",), ("azure",))


def test_runtime_init_order_follows_after_and_enters_the_recipe_digest(tmp_path: Path) -> None:
    def ordered(first: str, second: str) -> Recipe:
        return Recipe(
            "inits",
            items=(
                BootStep(second, f"echo {second}\n", after=(Ready(first),)),
                BootStep(first, f"echo {first}\n"),
            ),
        )

    a_first, b_first = ordered("a", "b"), ordered("b", "a")
    afters = []
    for name, recipe in (("a", a_first), ("b", b_first)):
        emit(lower(recipe), tmp_path / name)
        units = tmp_path / name / "payload/mkosi.extra/usr/lib/systemd/system"
        aggregate = (units / "runtime-init.service").read_text()
        afters.append(next(line for line in aggregate.splitlines() if line.startswith("After=")))
    assert afters[0] == "After=runtime-init-a.service runtime-init-b.service"
    assert afters[1] == "After=runtime-init-b.service runtime-init-a.service"
    assert lower(a_first).digest() != lower(b_first).digest()
    sections = [lock(r).lockfile.sections["init_scripts"] for r in (a_first, b_first)]
    assert sections[0] != sections[1]


def test_runtime_init_runs_each_step_as_its_own_unit(tmp_path: Path) -> None:
    first = BootStep("first", "echo first\n")
    recipe = Recipe(
        "inits",
        items=(BootStep("second", "echo second\n", after=(first.ready,)), first),
    )
    emit(lower(recipe), tmp_path)
    extra = tmp_path / "payload/mkosi.extra"
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
            Setting("Build", "Environment", ("FOO=bar",)),
            Setting("Build", "WithNetwork", ("no",)),
            Setting("Build", "SandboxTrees", ("a:/b", "c:/d")),
        ),
        platform=Debian(
            "trixie", mirror="https://mirror/", tools_mirror="https://tools/", epoch=7, boot=None
        ),
        mkosi=Mkosi(root="verity", build_network=True),
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
        environment={"FOO": "bar"},
        with_network=False,
        sandbox_trees=("a:/b", "c:/d"),
        root="verity",
    )
    assert img.epoch == 7 and img.image_id == "wide"
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


@pytest.mark.parametrize(
    ("setting", "code"),
    [
        (Setting("Config", "ConfigureScripts", ("configure.sh",)), "mkosi-configure-script"),
        (Setting("Build", "Environment", ("HOME",)), "mkosi-environment-passthrough"),
        (Setting("Config", "PassEnvironment", ("HOME",)), "mkosi-environment-passthrough"),
    ],
)
def test_lowering_refuses_settings_summary_cannot_describe(setting: Setting, code: str) -> None:
    with pytest.raises(ValidationError, match=code):
        lower(Recipe("r", items=(setting,)))


@pytest.mark.parametrize(
    "variants",
    [
        (Variant("default"), Variant("a", outputs=("azure",)), Variant("b", inherits="a")),
        (Variant("default", add=Fragment("d", items=(Package("d"),))), Variant("sibling")),
        (Variant("default"), Variant("slim", changes=(Package.ref("curl").remove(),))),
        (Variant("default"), Variant("v", changes=(Hook.ref("h").edit(script="other"),))),
        (Variant("default"), Variant("v", add=Fragment("k", items=(Key("k2"),)))),
        (Variant("default"), Variant("rescue", inherits=None)),
        (
            Variant("default"),
            Variant("v", add=Fragment("s", items=(Setting("Output", "Seed", ("x",)),))),
        ),
    ],
    ids=["chain", "sibling-of-base", "remove", "edit-hook", "child-keys", "standalone", "settings"],
)
def test_variants_a_profile_overlay_cannot_express_are_refused(
    variants: tuple[Variant, ...],
) -> None:
    common = Fragment("c", items=(Package("curl"), Hook("h", "build", "make"), Key("k1")))
    recipe = Recipe("r", items=(common,), variants=variants)
    with pytest.raises(ValidationError, match=f"Cannot emit variant '{variants[-1].name}'"):
        lower(recipe)


def test_a_variant_that_only_names_outputs_is_a_wrapper_not_an_overlay() -> None:
    recipe = Recipe(
        "r",
        items=(Package("curl"),),
        variants=(
            Variant("default"),
            Variant("a", outputs=("azure",)),
            Variant("dev", add=Fragment("d", items=(Package("strace"),))),
        ),
    )
    img = lower(recipe)
    assert img.targets == {"default": ("qemu",), "a": ("azure",), "dev": ("qemu",)}
    assert img.overlays == ("dev",)
    assert (img.mkosi_profiles("a"), img.clouds("a")) == ((), ("azure",))
    assert (img.mkosi_profiles("dev"), img.clouds("dev")) == (("dev",), ())
    # the payload carries the glue of every target a variant names, whichever are selected
    payload = lower(recipe, variants=("default",)).state.effective_profile("default")
    assert payload.output_targets == ("qemu", "azure") and "dmidecode" in payload.packages


def test_mkosi_argv_selects_profiles_and_wrappers(tmp_path: Path) -> None:
    from tundravm.declarative import mkosi_argv

    recipe = Recipe(
        "r",
        items=(Package("curl"),),
        variants=(
            Variant("default"),
            Variant("cloud", outputs=("azure", "gcp")),
            Variant("dev", add=Fragment("d", items=(Package("strace"),))),
        ),
    )
    img = lower(recipe)
    assert mkosi_argv(img, "default") == []
    assert mkosi_argv(img, "cloud") == ["--dependency=azure", "--dependency=gcp"]
    assert mkosi_argv(img, "dev") == ["--profile=dev"]
    with pytest.raises(ValidationError, match="Unknown variant"):
        mkosi_argv(img, "nope")


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
    emit(lower(_one(disk, ready, *services)), tmp_path)
    tree = tmp_path / "payload/mkosi.extra/usr/lib/systemd/system"
    assert "runtime-init" not in (tree / "early.service").read_text()
    assert "After=runtime-init.service\n" in (tree / "late.service").read_text()
    data = (tree / "data.service").read_text()
    assert "After=runtime-init-disks.service runtime-init-ready.service\n" in data
    assert "Requires=runtime-init-disks.service runtime-init-ready.service\n" in data


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
    payload = tmp_path / "payload"
    default, boot = payload / units, payload / "mkosi.profiles/boot" / units
    assert "runtime-init" not in (default / "app.service").read_text()
    for unit in ("app.service", "worker.service"):
        assert "After=runtime-init.service" in (boot / unit).read_text()
        assert "Requires=runtime-init.service" in (boot / unit).read_text()
    # early needs nothing: the overlay leaves the payload's unit as it is
    assert not (boot / "early.service").exists()
    assert "runtime-init" not in (default / "early.service").read_text()
    postinst = (payload / "mkosi.profiles/boot/mkosi.postinst.d/50-postinst.sh").read_text()
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
        # The SSH key step listens with no interface up: network-missing, not this test's.
        assert [d.code for d in resolution_lint(recipe)] == ["network-missing"]
        out = tmp_path / delivery.step
        emit(lower(recipe), out)
        root = out / "payload" / "mkosi.extra"
        assert (root / f"usr/lib/runtime-init/{delivery.step}").read_text().endswith(script)
        units = root / "usr/lib/systemd/system"
        waits = f"runtime-init-{delivery.step}.service"
        assert f"After={waits}\n" in (units / "app.service").read_text()
        assert "After=runtime-init-secrets.service\n" in (units / "ssh-user.service").read_text()
    assert agent.step == "delivery-vault-agent"
    unwired = _one(Secrets(entries=(Secret("token", (entry,)),), delivery=ExternalDelivery("x")))
    (finding,) = [d for d in resolution_lint(unwired) if d.code != "network-missing"]
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
            mkosi=Mkosi(version_script=True, root="verity"),
            policy=policy,
        )
    )
    assert img.mkosi.generate_version_script and img.mkosi.root == "verity"
    assert not img.mkosi.with_network  # build_network defaults to False
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
    assert img.targets["cloud"] == ("azure", "gcp") and img.clouds("cloud") == ("azure", "gcp")
    assert img.overlays == ()
    with pytest.raises(ValidationError, match="must be a sequence of targets"):
        Variant("x", outputs="qemu")  # type: ignore[arg-type]
