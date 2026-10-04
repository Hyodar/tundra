"""Declaration values: design defaults, freezing, and declaration-time validation."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

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
    Partition,
    Recipe,
    Repository,
    RuntimeTools,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Setting,
    Unit,
    User,
    Variant,
)
from tundravm.errors import ValidationError

GIT = Git("https://example.com/r.git", "main")


def test_design_defaults() -> None:
    recipe = Recipe("r", Fragment("common"))
    assert recipe.base == "debian/trixie"
    assert recipe.variants == (Variant("default", target="qemu"),)
    assert recipe.epoch == 0 and recipe.mkosi == Mkosi("directories", "current")
    assert Variant("v").parent == "base" and Variant("v").add == Fragment("empty")
    assert User("u").system is True and Group("g").system is True
    assert Disk("d", "/data").device is None and Disk("d", "/data").format == "on_fail"
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
    ("variant own parent", lambda: Variant("v", parent="v")),
    ("unknown target", lambda: Variant("v", target="aws")),  # type: ignore[arg-type]
    ("fragment in replace", lambda: Variant("v", replace=(Fragment("f"),))),  # type: ignore[arg-type]
    ("no variants", lambda: Recipe("r", Fragment("c"), variants=())),
    ("duplicate variants", lambda: Recipe("r", Fragment("c"), variants=(Variant("a"),) * 2)),
    ("negative epoch", lambda: Recipe("r", Fragment("c"), epoch=-1)),
    ("bad layout", lambda: Mkosi(layout="flat")),  # type: ignore[arg-type]
    ("empty package", lambda: Package("")),
    ("package role", lambda: Package("a", role="dev")),  # type: ignore[arg-type]
    ("relative file", lambda: File("etc/motd", "x")),
    ("file mode range", lambda: File("/etc/motd", "x", mode=0o10000)),
    ("file stage", lambda: File("/etc/motd", "x", stage="build")),  # type: ignore[arg-type]
    ("directory source str", lambda: Directory("/opt", "src")),  # type: ignore[arg-type]
    ("group name", lambda: Group("Bad Name")),
    ("user home relative", lambda: User("u", home="home/u")),
    ("unit name", lambda: Unit("bad unit", enabled=True)),
    ("unit without action", lambda: Unit("ssh.service")),
    ("unit file without suffix", lambda: Unit("app", "[Unit]\n", enabled=True)),
    ("hook phase", lambda: Hook("h", "later", "echo")),  # type: ignore[arg-type]
    ("hook empty script", lambda: Hook("h", "build", "")),
    ("hook env name", lambda: Hook("h", "build", "echo", env=(("1A", "x"),))),
    ("hook env duplicate", lambda: Hook("h", "build", "echo", env=(("A", "1"), ("A", "2")))),
    ("hook after itself", lambda: Hook("h", "build", "echo", after=("h",))),
    ("reserved init name", lambda: Init("disks", "echo")),
    ("init after itself", lambda: Init("i", "echo", after=("i",))),
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
    Unit("ssh.socket", enabled=False, masked=True)
    Hook("h", "boot", "echo", env=(("A", "1"),), cwd="/tmp", after=("g",))
    Kernel("6.13.12", Git("https://github.com/gregkh/linux", "v6.13.12"), cmdline="quiet")
