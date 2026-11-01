"""``needs=`` waits for runtime-init steps: one unit per step, or the whole chain."""

from pathlib import Path

from tundravm.declarative import (
    RUNTIME_INIT,
    BootStep,
    Debian,
    Declaration,
    Disk,
    Key,
    Mkosi,
    Recipe,
    Unit,
)
from tundravm.testing._model import CompiledTree, compile_tree

APP_UNIT = "[Unit]\nDescription=App\nAfter=network.target\n\n[Service]\nExecStart=/usr/bin/app\n"
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


def test_needs_is_a_noop_without_runtime_init_steps(tmp_path: Path) -> None:
    assert _app(tmp_path, APP_UNIT) == APP_UNIT


def test_unit_needs_a_step(tmp_path: Path) -> None:
    wait = "runtime-init-hello.service"
    unit = Unit("app.service", APP_UNIT, enabled=True, needs=(HELLO.ready,))
    text = _tree(tmp_path, KEY, DISK, HELLO, unit).unit("app.service")

    assert _lines(text, "After") == [f"After={wait} network.target"]
    assert _lines(text, "Requires") == [f"Requires={wait}"]


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
