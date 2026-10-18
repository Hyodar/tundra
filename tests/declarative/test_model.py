"""Declaration values: design defaults, freezing, and declaration-time validation."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from tundravm.declarative import (
    RUNTIME_INIT,
    BootStep,
    Build,
    Debian,
    Debloat,
    Disk,
    File,
    FileTree,
    Fragment,
    Git,
    Go,
    Group,
    Hook,
    Http,
    Install,
    Kernel,
    Key,
    Mkosi,
    Package,
    Partition,
    Ready,
    Recipe,
    Repository,
    RuntimeTools,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Service,
    Setting,
    Unit,
    User,
    Variant,
)
from tundravm.errors import ValidationError

GIT = Git("https://example.com/r.git", "main")


def test_design_defaults() -> None:
    recipe = Recipe("r")
    assert recipe.platform == Debian("trixie", boot=None)
    assert recipe.platform.name == "debian/trixie" and recipe.platform.packages == ()
    assert recipe.variants == (Variant("default", outputs=("qemu",)),)
    assert recipe.platform.epoch == 0 and recipe.mkosi == Mkosi("directories", "current")
    assert Variant("v").inherits == "base" and Variant("v").add == Fragment("empty")
    assert User("u").system is True and Group("g").system is True
    assert Disk("d", "/data").device is None and Disk("d", "/data").format == "never"
    assert Secrets().store is None and Secrets().name == "secrets"
    assert Install("a", "/usr/bin/a").mode == 0o755


def test_records_are_frozen_and_slotted() -> None:
    package = Package("curl")
    with pytest.raises(dataclasses.FrozenInstanceError):
        package.name = "jq"  # type: ignore[misc]
    assert not hasattr(package, "__dict__")


def test_lists_are_frozen_into_tuples() -> None:
    fragment = Fragment("f", items=[Package("a")])  # type: ignore[arg-type]
    user = User("u", groups=["eth"])  # type: ignore[arg-type]
    assert fragment.items == (Package("a"),) and user.groups == ("eth",)
    assert hash(fragment) == hash(Fragment("f", items=(Package("a"),)))


INVALID: list[tuple[str, Callable[[], object]]] = [
    ("empty fragment name", lambda: Fragment("")),
    ("generator in items", lambda: Fragment("f", items=((Package("a"),),))),  # type: ignore[arg-type]
    ("non-callable check", lambda: Fragment("f", checks=("nope",))),  # type: ignore[arg-type]
    ("variant named base", lambda: Variant("base")),
    ("variant own parent", lambda: Variant("v", inherits="v")),
    ("unknown target", lambda: Variant("v", outputs=("aws",))),  # type: ignore[arg-type]
    ("fragment in changes", lambda: Variant("v", changes=(Fragment("f"),))),  # type: ignore[arg-type]
    ("declaration in changes", lambda: Variant("v", changes=(Package("a"),))),  # type: ignore[arg-type]
    ("no variants", lambda: Recipe("r", variants=())),
    ("duplicate variants", lambda: Recipe("r", variants=(Variant("a"),) * 2)),
    ("negative epoch", lambda: Recipe("r", platform=Debian("trixie", epoch=-1, boot=None))),
    ("bad layout", lambda: Mkosi(layout="flat")),  # type: ignore[arg-type]
    ("empty package", lambda: Package("")),
    ("package role", lambda: Package("a", role="dev")),  # type: ignore[arg-type]
    ("relative file", lambda: File("etc/motd", "x")),
    ("file mode range", lambda: File("/etc/motd", "x", mode=0o10000)),
    ("file stage", lambda: File("/etc/motd", "x", stage="build")),  # type: ignore[arg-type]
    ("directory source str", lambda: FileTree("/opt", "src")),  # type: ignore[arg-type]
    ("group name", lambda: Group("Bad Name")),
    ("user home relative", lambda: User("u", home="home/u")),
    ("unit name", lambda: Unit("bad unit", enabled=True)),
    ("unit without action", lambda: Unit("ssh.service")),
    ("unit file without suffix", lambda: Unit("app", "[Unit]\n", enabled=True)),
    ("hook phase", lambda: Hook("h", "later", "echo")),  # type: ignore[arg-type]
    ("hook empty script", lambda: Hook("h", "build", "")),
    ("hook env name", lambda: Hook("h", "build", "echo", env={"1A": "x"})),
    ("hook env not a mapping", lambda: Hook("h", "build", "echo", env=(("A", "1"),))),  # type: ignore[arg-type]
    ("hook after itself", lambda: Hook("h", "build", "echo", after=("h",))),
    ("reserved boot step name", lambda: BootStep("disks", "echo")),
    ("boot step name", lambda: BootStep("bad step", "echo")),
    ("boot step empty script", lambda: BootStep("s", "")),
    ("boot step after itself", lambda: BootStep("s", "echo", after=(Ready("s"),))),
    ("boot step before itself", lambda: BootStep("s", "echo", before=("s",))),  # type: ignore[arg-type]
    ("boot step after one handle", lambda: BootStep("s", "echo", after=Ready("x"))),  # type: ignore[arg-type]
    ("boot step after a key", lambda: BootStep("s", "echo", after=(Key("k"),))),  # type: ignore[arg-type]
    ("boot step after everything", lambda: BootStep("s", "echo", after=(RUNTIME_INIT,))),
    ("ready step name", lambda: Ready("bad step")),
    ("ready non-str", lambda: Ready(1)),  # type: ignore[arg-type]
    ("unit needs a key", lambda: Unit("a.service", "[Unit]\n", needs=(Key("k"),))),  # type: ignore[arg-type]
    ("service needs a str", lambda: Service("a", "/usr/bin/a", needs="disks")),  # type: ignore[arg-type]
    ("repository suite", lambda: Repository("r", "https://x", "")),
    ("partition mount", lambda: Partition("p", "1G", "data")),
    ("debloat relative path", lambda: Debloat(extra_remove=("usr/share",))),
    ("setting values str", lambda: Setting("Output", "Seed", "abc")),  # type: ignore[arg-type]
    ("key name", lambda: Key("bad key")),
    ("pipe without path", lambda: Key("k", strategy="pipe")),
    ("pipe path without pipe", lambda: Key("k", pipe="/run/k")),
    ("disk key by name", lambda: Disk("d", "/data", key="k")),  # type: ignore[arg-type]
    ("plain disk mapper", lambda: Disk("d", "/data", mapper="m")),
    ("disk format", lambda: Disk("d", "/data", format="sometimes")),  # type: ignore[arg-type]
    ("schema lengths", lambda: Schema(min_length=5, max_length=1)),
    ("secret without targets", lambda: Secret("s", ())),
    ("secret env name", lambda: SecretEnv("1BAD")),
    ("secrets store by name", lambda: Secrets(store="disk")),  # type: ignore[arg-type]
    ("secrets port", lambda: Secrets(port=0)),
    ("runtime tools http", lambda: RuntimeTools(Http("https://x"))),  # type: ignore[arg-type]
    ("git subdir escape", lambda: Git("https://x", "main", subdir="../up")),
    ("http sha", lambda: Http("https://x", sha256="abc")),
    ("install absolute source", lambda: Install("/abs", "/usr/bin/a")),
    ("install relative destination", lambda: Install("a", "usr/bin/a")),
    ("install dir with mode", lambda: Install("share", "/usr/share/a", directory=True)),
    ("build name", lambda: Build("bad name", GIT, "make", (Install("a", "/usr/bin/a"),))),
    ("build installs nothing", lambda: Build("b", GIT, "make", ())),
    (
        "build duplicate basenames",
        lambda: Build("b", GIT, "make", (Install("a", "/usr/bin/x"), Install("b", "/opt/x"))),
    ),
    ("kernel config str", lambda: Kernel("6.1", GIT, config="k.config")),  # type: ignore[arg-type]
    ("install artifact dir", lambda: Install("out", "/opt/out", mode=None, artifact=True)),
    ("artifact of a script", lambda: Build("b", GIT, "make", (Install("b", "/x", artifact=True),))),
    (
        "artifact not the recipe's",
        lambda: Build(
            "b",
            GIT,
            using=Go(output="tool"),
            install=(Install("bin/tool", "/usr/bin/tool", artifact=True),),
        ),
    ),
]


@pytest.mark.parametrize(("label", "make"), INVALID, ids=[label for label, _ in INVALID])
def test_invalid_declarations_raise(label: str, make: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        make()


def test_valid_declarations_construct() -> None:
    key = Key("k", output="/run/k")
    disk = Disk("d", "/data", key=key, mapper="cryptroot")
    Disk("plain", "/scratch", key=Path("/etc/disk.key"))
    Secrets(
        entries=(
            Secret(
                "token",
                (SecretFile("/etc/token", owner="app"), SecretEnv("TOKEN", service="app")),
                schema=Schema(min_length=1),
            ),
        ),
        store=disk,
    )
    Install("share", "/usr/share/a", mode=None, directory=True)
    Build("b", GIT, using=Go(output="t"), install=(Install("build/t", "/t", artifact=True),))
    Unit("ssh.socket", enabled=False, masked=True)
    Hook("h", "boot", "echo", env={"A": "1"}, cwd="/tmp", after=("g",))
    Kernel("6.13.12", Git("https://github.com/gregkh/linux", "v6.13.12"), cmdline="quiet")


def test_packages_is_a_named_bundle_of_packages() -> None:
    from tundravm.declarative import packages, resolve

    tools = packages("strace", "gdb")
    assert tools.items == (Package("strace"), Package("gdb"))
    assert tools.name == "packages(strace, gdb)" and tools == packages("strace", "gdb")
    build = packages("gcc", role="build")
    assert build.items == (Package("gcc", "build"),) and build.name == "packages(gcc, role=build)"
    with pytest.raises(dataclasses.FrozenInstanceError):
        tools.items = ()  # type: ignore[misc]
    with pytest.raises(ValidationError, match="at least one"):
        packages()
    recipe = Recipe(
        "r",
        items=[packages("curl"), Package("jq")],  # type: ignore[arg-type]
        variants=(Variant("default", outputs=["qemu"]), Variant("dev", add=tools)),  # type: ignore[arg-type]
    )
    assert recipe.items == (packages("curl"), Package("jq")) and recipe.variants[0].outputs == (
        "qemu",
    )
    dev = resolve(recipe, variant="dev")
    assert dev.fragments == ("packages(curl)", "packages(strace, gdb)")
    assert {item.name for item in dev.items if isinstance(item, Package)} == {
        "curl",
        "jq",
        "strace",
        "gdb",
    }


def test_mapping_fields_take_only_mappings_and_freeze_them() -> None:
    from tundravm.declarative import Service, Template

    env = {"B": "2", "A": "1"}
    service = Service("app", "/bin/app", env=env, limits={"NOFILE": 1024})
    env["C"] = "3"
    assert dict(service.env) == {"B": "2", "A": "1"} and list(service.env) == ["B", "A"]
    assert service == Service("app", "/bin/app", env={"A": "1", "B": "2"}, limits={"NOFILE": 1024})
    assert hash(service) == hash(
        Service("app", "/bin/app", env={"A": "1", "B": "2"}, limits={"NOFILE": 1024})
    )
    debloat = Debloat(keep_paths_by_variant={"dev": ["/usr/bin/gdb"]})
    assert debloat.keep_paths_by_variant == {"dev": ("/usr/bin/gdb",)}
    for bad in (
        lambda: Service("app", "/bin/app", env=(("A", "1"),)),  # type: ignore[arg-type]
        lambda: Template("/etc/t", "x", variables=[("a", 1)]),  # type: ignore[arg-type]
        lambda: Hook("h", "build", "echo", env=[("A", "1")]),  # type: ignore[arg-type]
        lambda: Debloat(keep_paths_by_variant=(("dev", ("/x",)),)),  # type: ignore[arg-type]
    ):
        with pytest.raises(ValidationError, match="must be a mapping"):
            bad()


def test_readiness_handles_name_their_runtime_init_step() -> None:
    key = Key("k")
    disk = Disk("d", "/data", key=key)
    step = BootStep("warm", "echo warm\n", after=(disk.ready,))
    assert (key.ready, disk.ready, Secrets().ready) == (
        Ready("keys"),
        Ready("disks"),
        Ready("secrets"),
    )
    assert step.ready == Ready("warm") and str(step.ready) == "warm"
    assert RUNTIME_INIT == Ready("*")


def test_step_references_normalize_to_handles() -> None:
    first = BootStep("first", "echo 1\n")
    after: list[object] = [first, "third"]
    second = BootStep("second", "echo 2\n", after=after, before=[Ready("fourth")])  # type: ignore[arg-type]
    assert second.after == (Ready("first"), Ready("third"))
    assert second.before == (Ready("fourth"),)
    assert BootStep("x", "y\n", after=(first.ready, first.ready)).after == (Ready("first"),)


def test_needs_defaults_and_takes_the_runtime_init_sentinel() -> None:
    assert Service("a", "/usr/bin/a").needs == (RUNTIME_INIT,)
    assert Unit("a.service", "[Unit]\n").needs == ()
    assert Unit("a.service", "[Unit]\n", needs=[RUNTIME_INIT]).needs == (RUNTIME_INIT,)
    assert Service("a", "/usr/bin/a", needs=(Key("k").ready,)).needs == (Ready("keys"),)
