"""Every program a generated unit runs is in the image.

The committed trees (the fixture's two and the attested node's), each profile on its own:
each ``Exec*=`` program of each unit, each ``/usr/bin`` or ``/usr/sbin`` command of a
script a unit runs, and each program a ``tools/`` command a unit runs executes
(:data:`~tundravm._modules.base.TOOL_PROGRAMS`) is a file of the tree, a build's output or
a program of a listed (or essential) package as check's table knows it. A unit that runs in
a container's root (``RootImage=``) is check's ``service-program-missing``.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from tests.helpers import ATTESTED_NODE, REPO_ROOT, conf_list
from tundravm._modules.base import HOST_TOOLS, TOOL_PROGRAMS, TOOLS_LOCAL, TPM_PROGRAMS
from tundravm.check import _packaged

TOOLS = REPO_ROOT / "tools"
TREES = {
    "fixture-initramfs": REPO_ROOT / "tests/fixtures/native-tree/tree-initramfs",
    "fixture-verity": REPO_ROOT / "tests/fixtures/native-tree/tree-verity",
    "attested-node": ATTESTED_NODE / "mkosi",
}
_MODULE = "github.com/Hyodar/tundra/tools/"
_IMPORT = re.compile(rf'"{re.escape(_MODULE)}([^"]+)"')
_EXEC = re.compile(
    r"(?:\.Run\(\s*ctx\s*,|\.RunWithStdin\(\s*ctx\s*,\s*[^,]+,|exec\.Command\(|"
    r'exec\.CommandContext\(\s*ctx\s*,)\s*"([^"]+)"'
)
_UNIT = re.compile(r"/systemd/system/[^/]+\.service(\.d/[^/]+\.conf)?$")
_BIN_COMMAND = re.compile(r"(?<![\w./-])(/usr/s?bin/[\w.+-]+)")
_BUILD = "# tundravm build: "


def _go_files(package: str) -> list[Path]:
    return sorted(p for p in (TOOLS / package).glob("*.go") if not p.name.endswith("_test.go"))


def _executes(command: str) -> set[str]:
    """What ``tools/cmd/<command>`` and the packages of this module it imports execute."""
    seen: set[str] = set()
    pending = [f"cmd/{command}"]
    programs: set[str] = set()
    while pending:
        package = pending.pop()
        if package in seen:
            continue
        seen.add(package)
        for path in _go_files(package):
            text = path.read_text(encoding="utf-8")
            pending.extend(_IMPORT.findall(text))
            programs.update(_EXEC.findall(text))
    return programs


def test_the_table_names_what_each_tool_executes() -> None:
    commands = {p.name for p in (TOOLS / "cmd").iterdir() if p.is_dir()}
    assert commands == {*TOOL_PROGRAMS, *HOST_TOOLS}
    declared = {tool: set(programs) for tool, programs in TOOL_PROGRAMS.items()}
    declared["key-gen"] |= set(TPM_PROGRAMS)
    assert {tool: _executes(tool) for tool in TOOL_PROGRAMS} == declared


def _files(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        return {}
    return {f"/{p.relative_to(directory)}": p for p in directory.rglob("*") if p.is_file()}


def _builds(directory: Path) -> dict[str, str | None]:
    """Each path the build scripts under *directory* install, with the ``tools/`` command
    it is (``None`` for any other build)."""
    found: dict[str, str | None] = {}
    for script in sorted(directory.glob("mkosi.build.d/*")):
        for line in script.read_text(encoding="utf-8").splitlines():
            if not line.startswith(_BUILD):
                continue
            build = json.loads(line.removeprefix(_BUILD))
            tools = build["source"].get("path") == TOOLS_LOCAL
            command = (build["build"].get("package") or "").removeprefix("./cmd/")
            for step in build["install"]:
                found[step["dest"]] = command if tools else None
    return found


def _profiles(payload: Path) -> dict[str, list[Path]]:
    """Each profile of *payload*, as the directories it is made of, the payload's first: the
    default one with the overlay a ``mkosi.conf.d`` file includes for it."""
    own = [
        payload / line.removeprefix("Include=")
        for conf in sorted(payload.glob("mkosi.conf.d/*.conf"))
        for line in conf.read_text(encoding="utf-8").splitlines()
        if line.startswith("Include=mkosi.profiles/")
    ]
    views = {"default": [payload, *own]}
    for profile in sorted((payload / "mkosi.profiles").glob("*")):
        if profile not in own:
            views[profile.name] = [payload, profile]
    return views


def _program(word: str) -> str | None:
    program = word.lstrip("@-:+!|")
    if not program or "$" in program:
        return None
    return program if "/" in program else f"/usr/bin/{program}"


def _misses(payload: Path, layers: list[Path]) -> list[str]:
    conf = "\n".join(
        path.read_text(encoding="utf-8")
        for layer in layers
        for path in (layer / "mkosi.conf", *sorted(layer.glob("mkosi.conf.d/*.conf")))
        if path.is_file()
    )
    packages = set(conf_list(conf, "Packages"))
    files: dict[str, Path] = {}
    builds: dict[str, str | None] = {}
    for layer in layers:
        files |= _files(layer / "mkosi.extra") | _files(layer / "mkosi.skeleton")
        builds |= _builds(layer)
    misses: list[str] = []

    def need(program: str, why: str) -> None:
        if program in files or program in builds or _packaged(program, packages):
            return
        misses.append(f"{why} runs {program}")

    for path, source in sorted(files.items()):
        text = source.read_text(encoding="utf-8")
        if not _UNIT.search(path) or "RootImage=" in text:
            continue
        for key, _, value in (line.partition("=") for line in text.splitlines()):
            if not key.startswith("Exec") or not value.strip():
                continue
            program = _program(shlex.split(value)[0])
            if program is None:
                continue
            need(program, f"{path} {key}=")
            tool = builds.get(program)
            for name in TOOL_PROGRAMS.get(tool or "", {}):
                need(f"/usr/bin/{name}", f"{path} through {tool}")
            script = files.get(program)
            if script is not None and script.read_bytes().startswith(b"#!"):
                for command in _BIN_COMMAND.findall(script.read_text(encoding="utf-8")):
                    need(command, f"{path} through {program}")
                    for name in TOOL_PROGRAMS.get(builds.get(command) or "", {}):
                        need(f"/usr/bin/{name}", f"{path} through {command}")
    return misses


@pytest.mark.parametrize("tree", sorted(TREES))
def test_every_program_a_unit_runs_is_in_the_image(tree: str) -> None:
    payload = TREES[tree] / "payload"
    misses = {
        name: found
        for name, layers in _profiles(payload).items()
        if (found := _misses(payload, layers))
    }
    assert misses == {}
