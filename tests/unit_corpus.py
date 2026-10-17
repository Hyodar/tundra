"""Every systemd unit text in the repository, for the unit text and unit lint tests.

Sources: the committed surge golden tree, the unit constants of modules, examples and
templates, and the emitted tree of every example, surge variant and ``init`` template.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from tests.compiler.test_lowering_parity import recipe_paths
from tests.helpers import REPO_ROOT, SURGE_EXAMPLE

_UNIT_PATH = re.compile(r"(^|/)(usr/lib|etc)/systemd/system/")
_UNIT_SUFFIX = re.compile(r"\.(service|socket|target|timer|mount|path|conf)$")


def is_unit_file(path: str) -> bool:
    """Whether *path* (relative to a tree root) is a unit or drop-in under a systemd directory."""
    return bool(_UNIT_PATH.search(path) and _UNIT_SUFFIX.search(path))


def golden_units() -> Iterator[tuple[str, str]]:
    """``(path, text)`` for every unit file in the committed surge golden tree."""
    root = SURGE_EXAMPLE / "mkosi"
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and is_unit_file(relative):
            yield f"golden:{relative}", path.read_text(encoding="utf-8")


def constant_units() -> Iterator[tuple[str, str]]:
    """``(name, text)`` for the unit text constants written in Python."""
    from examples.fragments import nethermind, raiko, taiko_client

    from tundravm._modules import devtools, init
    from tundravm._modules.tdxs import Tdxs
    from tundravm.compiler.emit_mkosi import MINIMAL_TARGET_UNIT
    from tundravm.platforms import azure

    yield "MINIMAL_TARGET_UNIT", MINIMAL_TARGET_UNIT
    yield "NETHERMIND_UNIT", nethermind.NETHERMIND_UNIT
    yield "RAIKO_UNIT", raiko.RAIKO_UNIT
    yield "TAIKO_CLIENT_UNIT", taiko_client.TAIKO_CLIENT_UNIT
    yield "SERIAL_CONSOLE_SERVICE", devtools.SERIAL_CONSOLE_SERVICE
    yield "AZURE_PROVISIONING_SERVICE", azure.AZURE_PROVISIONING_SERVICE
    yield "AZURE_PROVISIONING_SERVICE_ONLINE", azure.AZURE_PROVISIONING_SERVICE_ONLINE
    yield "tdxs.service", Tdxs().render_service_unit()
    yield "tdxs.socket", Tdxs().render_socket_unit()
    yield "runtime-init.service", init.render_service_unit()
    yield "runtime-init.service (online)", init.render_service_unit(network_setup=False)
    yield (
        "network-setup.service",
        (REPO_ROOT / "examples" / "nethermind-base" / "network-setup.service").read_text(
            encoding="utf-8"
        ),
    )


@cache
def emitted_units(scratch: Path) -> tuple[tuple[str, str], ...]:
    """``(recipe:path, text)`` for every unit file of every recipe's emitted tree."""
    from tundravm.declarative import compile, load

    found: list[tuple[str, str]] = []
    for name, path in sorted(recipe_paths(scratch).items()):
        tree = compile(load(path, extra_paths=[REPO_ROOT]))
        for entry in tree.entries:
            if entry.content is not None and entry.symlink is None and is_unit_file(entry.path):
                found.append((f"{name}:{entry.path}", entry.content.decode("utf-8")))
    return tuple(found)
