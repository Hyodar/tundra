"""Lowering: a declarative Recipe as the compiler's RecipeState, variants as additions to the
payload (profile overlays or cloud wrappers)."""

from __future__ import annotations

from pathlib import Path

import pytest

import tundravm
from tests.helpers import own_files
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


EQUIVALENCE_TREE = "86a9b1589829cecd80b95460877f9556d08f700669017959ad404761d99f2a15"
EQUIVALENCE_RECIPE = "cdc1b4dec171451cfdd1c1ea89ffd0c4d68d20b6f270c6be6e31fa58ab7ec3f0"
"""The tree and recipe digests :func:`declarative_recipe` lowers to, recorded when the tree
became mkosi-native (one payload, its cloud glue for every target, ``azure`` a profile
overlay), and re-recorded when the ESP became a fixed 128M FAT32 and when the unpinned
build scripts' hint named ``tundravm build`` instead of the removed ``fetch``, and when
every profile gained the execution gate, then the instance key, when the initramfs
ESP became sized from the UKI's ceiling, and when the endpoint's configuration gained
its ``attested_tls`` list, and when the cloud glue gained its own groups. The recipe
digest covers the compiler version, recorded as
``0.1.0``."""


def test_lowered_recipe_compiles_to_the_recorded_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tundravm, "__version__", "0.1.0")
    lowered = lower(declarative_recipe())

    assert lowered.select(PROFILES).digest() == EQUIVALENCE_RECIPE
    assert compile(declarative_recipe()).digest == EQUIVALENCE_TREE
    assert [(d.severity, d.code, d.profile, d.subject) for d in check(lowered)] == [
        ("warning", "source-unpinned", "", "sources.app"),
        ("warning", "source-unpinned", "", "sources.attested-endpoint"),
        ("warning", "source-unpinned", "", "sources.disk-encryption"),
        ("warning", "source-unpinned", "", "sources.key-generation"),
        ("error", "disk-device-guessed", "default", "disk_persistent"),
        ("error", "kernel-missing", "default", ""),
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


def test_runtime_init_order_follows_after_and_enters_the_image_digest(tmp_path: Path) -> None:
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
    "variants",
    [
        (Variant("default"), Variant("a", outputs=("azure",)), Variant("b", inherits="a")),
        (Variant("default", add=Fragment("d", items=(Package("d"),))), Variant("sibling")),
        (Variant("default"), Variant("slim", changes=(Package.ref("curl").remove(),))),
        (Variant("default"), Variant("v", changes=(Hook.ref("h").edit(script="other"),))),
        (Variant("default"), Variant("v", add=Fragment("k", items=(Key("k2"),)))),
        (Variant("default"), Variant("rescue", inherits=None)),
    ],
    ids=["chain", "sibling-of-base", "remove", "edit-hook", "child-keys", "standalone"],
)
def test_variants_that_are_not_additions_are_lowered_whole(
    variants: tuple[Variant, ...], tmp_path: Path
) -> None:
    """A variant that extends another, removes or replaces is its whole resolved image, as a
    standalone profile; the emitter splits what every profile shares from its overlay."""
    common = Fragment("c", items=(Package("curl"), Hook("h", "build", "make"), Key("k1")))
    recipe = Recipe("r", items=(common,), variants=variants)
    lowered = lower(recipe)
    name = variants[-1].name
    assert name in lowered.overlays
    assert lowered.state.ensure_profile(name).extends is None
    effective = lowered.state.effective_profile(name)
    resolved = {i.name for i in resolve(recipe, variant=name).items if isinstance(i, Package)}
    assert {"curl", "d"} & effective.packages == {"curl", "d"} & resolved
    compile(recipe).write(tmp_path)


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
    with pytest.raises(ValidationError, match="Unknown profile"):
        mkosi_argv(img, "nope")


def test_lowering_lowers_every_variant_whatever_the_selection() -> None:
    """The shared payload is what every variant builds, so a selection lowers them all (and
    still checks the names it is given)."""
    recipe = Recipe(
        "r",
        items=(Package("curl"),),
        variants=(Variant("default"), Variant("a"), Variant("b")),
    )
    assert lower(recipe, variants=("b",)).profile_names == ("a", "b", "default")
    with pytest.raises(ValidationError, match="ghost"):
        lower(recipe, variants=("ghost",))


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
    assert [(f.path, f.mode) for f in own_files(default.files)] == [
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
    ("text", "services", "expected"),
    [
        (
            "[Unit]\nDescription=x\n\n[Service]\nExecStart=/x\n",
            (),
            "[Unit]\nDescription=x\nAfter=runtime-init.service\nRequires=runtime-init.service\n"
            "\n[Service]\nExecStart=/x\n",
        ),
        (
            "[Unit]\nRequires=a.service\nAfter=a.service\n",
            (),
            "[Unit]\nRequires=runtime-init.service a.service\n"
            "After=runtime-init.service a.service\n",
        ),
        (
            "[Unit]\nRequires=a.service\n",
            (),
            "[Unit]\nAfter=runtime-init.service\nRequires=runtime-init.service a.service\n",
        ),
        (
            "[Unit]\nAfter=runtime-init.service\n",
            (),
            "[Unit]\nAfter=runtime-init.service\nRequires=runtime-init.service\n",
        ),
        (
            "[Service]\nExecStart=/x\n",
            (),
            "[Unit]\nAfter=runtime-init.service\nRequires=runtime-init.service\n\n"
            "[Service]\nExecStart=/x\n",
        ),
        (
            "[Unit]\nAfter=a.service\n",
            (("runtime-init-x.service", "runtime-init-y.service"),),
            "[Unit]\nAfter=runtime-init-x.service runtime-init-y.service a.service\n"
            "Requires=runtime-init-x.service runtime-init-y.service\n",
        ),
    ],
    ids=[
        "no-relations",
        "both-relations",
        "requires-only",
        "already-after",
        "no-unit-section",
        "several-units-in-order",
    ],
)
def test_inject_after_init(text: str, services: tuple[tuple[str, ...], ...], expected: str) -> None:
    assert inject_after_init(text, *services) == expected


# ── Declarations added for the declarative-only API ─────────────────────


def _one(*items: object, epoch: int | None = 0, **recipe: object) -> Recipe:
    platform = Debian("trixie", epoch=epoch, boot=None)
    return Recipe("r", items=items, platform=platform, **recipe)  # type: ignore[arg-type]


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
    # the payload enables app; the overlay only ships the unit that waits for runtime-init
    postinst = (payload / "mkosi.profiles/boot/mkosi.postinst.d/50-postinst.sh").read_text()
    assert "systemctl enable app.service\n" not in postinst
    shared = (payload / "mkosi.postinst.d/50-postinst.sh").read_text()
    assert shared.count("systemctl enable app.service\n") == 1


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
    # no steps, no wait
    assert not own_files(lower(alone).state.effective_profile("default").files)


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
        gate = "tdx-evidence.service"
        assert f"After={waits} {gate}\n" in (units / "app.service").read_text()
        user = (units / "ssh-user.service").read_text()
        assert f"After=runtime-init-secrets.service {gate}\n" in user
    assert agent.step == "delivery-vault-agent"
    unwired = _one(Secrets(entries=(Secret("token", (entry,)),), delivery=ExternalDelivery("x")))
    (finding,) = [d for d in resolution_lint(unwired) if d.code != "network-missing"]
    assert (finding.code, finding.severity) == ("external-delivery-unwired", "warning")
    with pytest.raises(ValidationError, match="not both"):
        ExternalDelivery("x", unit="a.service", ready_path="/run/x")


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


def test_debloat_extra_units_and_per_variant_paths() -> None:
    debloat = Debloat(keep_units_extra=("app.service",), keep_paths_by_variant={"x": ("/opt",)})
    config = lower(_one(debloat)).state.effective_profile("default").debloat
    assert config.extra_keep_units == ("app.service",)
    assert config.paths_skip_for_profiles == (("x", ("/opt",)),)


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
    assert resolve(recipe, variant="cloud").targets == ("azure", "gcp")
    img = lower(recipe)
    assert img.targets["cloud"] == ("azure", "gcp") and img.clouds("cloud") == ("azure", "gcp")
    assert img.overlays == ()
    with pytest.raises(ValidationError, match="must be a sequence of targets"):
        Variant("x", outputs="qemu")  # type: ignore[arg-type]
