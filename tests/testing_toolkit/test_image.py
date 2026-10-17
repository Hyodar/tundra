"""Tests for ``fragment_plan``, ``plan`` and the ``Image`` queries."""

from __future__ import annotations

import pytest

from tundravm import (
    BootStep,
    Build,
    Disk,
    File,
    Fragment,
    Git,
    Group,
    Hook,
    Install,
    Key,
    Package,
    Recipe,
    Secrets,
    Service,
    Setting,
    Unit,
    User,
    Variant,
    compile,
)
from tundravm.errors import ValidationError
from tundravm.testing import (
    TEST_PLATFORM,
    Image,
    InitStep,
    assert_clean,
    assert_diagnostic,
    fragment_plan,
    plan,
)

APP_KEY = Key("app_key")
APP = Fragment(
    "app",
    (
        Package("curl"),
        Package("gcc", role="build"),
        File("/usr/bin/app", "#!/bin/sh\n", mode=0o755),
        Group("app"),
        User("app", groups=("app",)),
        Service("app", "/usr/bin/app --port 80", user="app", env={"MODE": "a b"}),
        Hook("configured", "postinst", "echo configured"),
        Hook("seeded", "postinst", "echo seeded", after=("configured",)),
        BootStep("app-state", "install -d /var/lib/app", after=(APP_KEY.ready,)),
        APP_KEY,
    ),
)


def test_fragment_plan_boots_on_the_test_platform() -> None:
    image = fragment_plan(APP)
    assert isinstance(image, Image)
    assert (image.recipe.name, image.variant) == ("fragment-plan", "default")
    assert image.packages["linux-image-amd64"] == "runtime"
    assert image.packages["curl"] == "runtime"
    assert image.packages["gcc"] == "build"
    assert_clean(image, allow=("source-unpinned",))
    assert TEST_PLATFORM.name in image.resolved.fragments


def test_fragment_plan_wraps_bare_declarations() -> None:
    image = fragment_plan(File("/etc/motd", "hi\n"), Package("jq"))
    assert "under-test" in image.resolved.fragments
    assert image.file("/etc/motd").text == "hi\n"
    assert image.file("etc/motd").fragment == "under-test"


def test_files_carry_mode_stage_and_origin() -> None:
    image = fragment_plan(APP)
    app = image.file("/usr/bin/app")
    assert (app.content, app.mode & 0o777, app.stage, app.owner) == (
        b"#!/bin/sh\n",
        0o755,
        "extra",
        "root",
    )
    assert app.fragment == "app"
    assert app.origin[-1].action == "declared"
    assert image.file("/usr/lib/runtime-init/app-state").origin == ()
    assert "/usr/lib/systemd/system/app.service" in image.files
    with pytest.raises(KeyError, match="/usr/bin/app"):
        image.file("/usr/bin/nope")


def test_service_view_parses_the_unit() -> None:
    unit = fragment_plan(APP).service("app")
    assert (unit.name, unit.enabled, unit.user) == ("app.service", True, "app")
    assert unit.exec_start == "/usr/bin/app --port 80"
    assert unit.requires("runtime-init.service") and unit.after("runtime-init.service")
    assert not unit.wants("network-online.target")
    assert unit.wanted_by == ("minimal.target",)
    assert unit.environment == {"MODE": "a b"}
    assert unit.get("Unit", "Description") == "app"
    assert unit.values("Unit", "Nope") == ()


def test_unit_view_reads_verbatim_units() -> None:
    text = "[Unit]\nAfter=a.target\nAfter=\nAfter=b.target \\\n  c.target\n[Install]\n"
    image = fragment_plan(Unit("raw.target", text, enabled=False))
    raw = image.unit("raw.target")
    assert raw.values("Unit", "After") == ("b.target c.target",)
    assert raw.after("c.target") and not raw.after("a.target")
    assert not raw.enabled
    with pytest.raises(KeyError, match="raw.target"):
        image.unit("missing")


def test_declared_users_groups_hooks_and_builds() -> None:
    build = Build(
        "tool",
        Git("https://example.com/t.git", "v1"),
        script="make",
        install=(Install("t", "/usr/bin/t"),),
    )
    image = fragment_plan(APP, build)
    assert image.users["app"].groups == ("app",)
    assert set(image.groups) == {"app"}
    assert [h.name for h in image.hooks("postinst")] == ["configured", "seeded"]
    assert image.hooks("finalize") == ()
    with pytest.raises(ValueError, match="stages"):
        image.hooks("install")
    assert set(image.builds) == {"tool"}


def test_init_steps_run_in_order_with_builtins() -> None:
    steps = fragment_plan(APP).init_steps
    assert steps == (
        InitStep("keys", builtin=True),
        InitStep("app-state", ("keys",), "install -d /var/lib/app"),
    )


def test_init_steps_follow_the_graph_with_before_edges() -> None:
    key = Key("k")
    disk = Disk("d", mount="/persistent", key=key, device="/dev/sdb")
    secrets = Secrets(store=disk)
    report = BootStep("report", "echo report\n", after=(secrets.ready,))
    mark = BootStep(
        "mark", "touch /persistent/mark\n", after=(disk.ready,), before=(secrets.ready,)
    )
    steps = fragment_plan(key, disk, secrets, report, mark).init_steps
    assert steps == (
        InitStep("keys", builtin=True),
        InitStep("disks", ("keys",), builtin=True),
        InitStep("mark", ("disks",), "touch /persistent/mark\n"),
        InitStep("secrets", ("disks", "mark"), builtin=True),
        InitStep("report", ("secrets",), "echo report\n"),
    )


def test_settings_read_the_compiled_mkosi_conf() -> None:
    image = fragment_plan(Setting("Content", "WithDocs", ("no",)))
    assert image.settings("Content")["WithDocs"] == ("no",)
    assert image.settings("Distribution")["Release"] == ("trixie",)
    assert image.settings("Nope") == {}


def test_lint_needs_no_compile_and_feeds_the_assertions() -> None:
    image = fragment_plan(Service("app", "/usr/bin/app", user="ghost"))
    assert_diagnostic(image, "service-user-missing", severity="error", subject="app")
    with pytest.raises(AssertionError, match="service-user-missing"):
        assert_clean(image)
    assert image.service("app").user == "ghost"  # a lint error, yet the tree compiles
    collision = fragment_plan(File("/etc/motd", "a\n"), File("/etc//motd", "b\n"))
    assert [d.code for d in collision.lint()] == ["identity-collision"]
    with pytest.raises(ValidationError, match="identity-collision"):
        _ = collision.files


def test_base_takes_a_fragment_or_a_recipe() -> None:
    bare = fragment_plan(
        File("/etc/x", "x"), base=Fragment("none", (Setting("Content", "Bootable", ("no",)),))
    )
    assert "linux-image-amd64" not in bare.packages
    host = Recipe(
        name="host",
        items=(Package("linux-image-amd64"),),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )
    on_azure = fragment_plan(File("/etc/x", "x"), base=host, variant="azure")
    assert on_azure.variant == "azure"
    assert on_azure.file("/etc/x").text == "x"


def test_include_satisfies_requires() -> None:
    needy = Fragment("needy", (File("/etc/n", "n"),), requires=("tools",))
    assert_diagnostic(fragment_plan(needy), "fragment-requires-missing", subject="needy")
    assert_clean(fragment_plan(needy, include=(Fragment("tools", (Package("jq"),)),)))


def test_plan_takes_a_variant_and_matches_compile() -> None:
    recipe = Recipe(
        name="demo",
        items=(Package("linux-image-amd64"),),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",), add=Fragment("azure", (Package("jq"),))),
        ),
    )
    assert plan(recipe).variant == "default"
    azure = plan(recipe, "azure")
    assert "jq" in azure.packages and "jq" not in plan(recipe).packages
    assert azure.tree.digest == compile(recipe, variants=["azure"]).digest
    with pytest.raises(ValidationError, match="Unknown variant"):
        plan(recipe, "gcp")
