"""``needs=`` waits for runtime-init steps: one unit per step, or the one runtime-init unit."""

from pathlib import Path

import pytest

from tundravm.declarative import (
    RUNTIME_INIT,
    BootStep,
    Debian,
    Declaration,
    Disk,
    Key,
    Mkosi,
    Recipe,
    Service,
    Unit,
)
from tundravm.testing import CompiledTree, compile_tree

APP_UNIT = "[Unit]\nDescription=App\nAfter=network.target\n\n[Service]\nExecStart=/usr/bin/app\n"
HISTORICAL = Mkosi(dialect="nethermind-v1")
KEY = Key("k")
DISK = Disk("d", "/persistent", device="/dev/vdb", key=KEY)
HELLO = BootStep("hello", "echo hello\n", after=(DISK.ready,))


def _tree(tmp_path: Path, *items: Declaration, mkosi: Mkosi | None = None) -> CompiledTree:
    recipe = Recipe(
        "app", items=items, platform=Debian("bookworm", boot=None), mkosi=mkosi or Mkosi()
    )
    return compile_tree(recipe, path=tmp_path / "tree")


def _lines(text: str, directive: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(f"{directive}=")]


def _app(tmp_path: Path, content: str, *extra: Declaration, mkosi: Mkosi | None = None) -> str:
    unit = Unit("app.service", content, enabled=True, needs=(RUNTIME_INIT,))
    return _tree(tmp_path, unit, *extra, mkosi=mkosi).unit("app.service")


def test_needs_runtime_init_prepends_init_when_steps_present(tmp_path: Path) -> None:
    text = _app(tmp_path, APP_UNIT, KEY)

    assert _lines(text, "After") == ["After=runtime-init.service network.target"]
    assert _lines(text, "Requires") == ["Requires=runtime-init.service"]


def test_needs_is_a_noop_without_runtime_init_steps(tmp_path: Path) -> None:
    assert _app(tmp_path, APP_UNIT) == APP_UNIT


def test_needs_no_duplicate_if_already_present(tmp_path: Path) -> None:
    content = APP_UNIT.replace(
        "After=network.target",
        "After=runtime-init.service network.target\nRequires=runtime-init.service",
    )
    assert _app(tmp_path, content, KEY) == content


@pytest.mark.parametrize(
    ("mkosi", "wait"),
    [(None, "runtime-init-hello.service"), (HISTORICAL, "runtime-init.service")],
    ids=["current", "nethermind-v1"],
)
def test_unit_needs_a_step(tmp_path: Path, mkosi: Mkosi | None, wait: str) -> None:
    unit = Unit("app.service", APP_UNIT, enabled=True, needs=(HELLO.ready,))
    text = _tree(tmp_path, KEY, DISK, HELLO, unit, mkosi=mkosi).unit("app.service")

    assert _lines(text, "After") == [f"After={wait} network.target"]
    assert _lines(text, "Requires") == [f"Requires={wait}"]


@pytest.mark.parametrize(
    ("mkosi", "wait"),
    [(None, "runtime-init-disks.service"), (HISTORICAL, "runtime-init.service")],
    ids=["current", "nethermind-v1"],
)
def test_service_needs_a_builtin_step(tmp_path: Path, mkosi: Mkosi | None, wait: str) -> None:
    service = Service("app", "/usr/bin/app", needs=(DISK.ready,))
    text = _tree(tmp_path, KEY, DISK, service, mkosi=mkosi).unit("app.service")

    assert _lines(text, "After") == [f"After={wait}"]
    assert _lines(text, "Requires") == [f"Requires={wait}"]


def test_service_waits_for_all_of_runtime_init_by_default(tmp_path: Path) -> None:
    text = _tree(tmp_path, KEY, DISK, Service("app", "/usr/bin/app")).unit("app.service")

    assert _lines(text, "Requires") == ["Requires=runtime-init.service"]


def test_needs_nothing_adds_no_dependency(tmp_path: Path) -> None:
    service = Service("app", "/usr/bin/app", needs=())
    text = _tree(tmp_path, KEY, DISK, service).unit("app.service")

    assert "runtime-init" not in text


def test_step_units_require_their_direct_dependencies(tmp_path: Path) -> None:
    early = BootStep("early", "echo early\n", before=(KEY.ready,))
    tree = _tree(tmp_path, KEY, DISK, HELLO, early)

    assert _lines(tree.unit("runtime-init-keys.service"), "Requires") == [
        "Requires=runtime-init-early.service"
    ]
    assert _lines(tree.unit("runtime-init-hello.service"), "Requires") == [
        "Requires=runtime-init-disks.service"
    ]
    assert "ExecStart=/usr/lib/runtime-init/hello" in tree.unit("runtime-init-hello.service")
    assert tree.read("mkosi.extra/usr/lib/runtime-init/hello").endswith("echo hello\n")
    steps = ("early", "keys", "disks", "hello")  # run order
    units = " ".join(f"runtime-init-{step}.service" for step in steps)
    assert _lines(tree.unit("runtime-init.service"), "Requires") == [f"Requires={units}"]


def test_nethermind_v1_runs_every_step_in_topological_order(tmp_path: Path) -> None:
    late = BootStep("late", "echo late\n", after=(HELLO.ready,))
    early = BootStep("early", "echo early\n", before=(KEY.ready,))
    tree = _tree(tmp_path, late, KEY, DISK, HELLO, early, mkosi=HISTORICAL)

    script = tree.runtime_init()
    order = ["echo early", "key-gen setup", "disk-setup setup", "echo hello", "echo late"]
    assert sorted(order, key=script.index) == order
    assert not tree.exists("mkosi.extra/usr/lib/runtime-init")
    assert "ExecStart=/usr/bin/runtime-init" in tree.unit("runtime-init.service")
