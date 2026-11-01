"""A public value class takes a list wherever its docs show one, for mypy and at run time."""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

import pytest
from mypy import api

from tests.helpers import REPO_ROOT

WARM_CACHE = REPO_ROOT / ".mypy_cache"
"""The repository's own mypy cache, when a ``mypy .`` left one: copied, never written."""
HEADER = """\
from tundravm import User
from tundravm.sources import Build, CargoBinary, Git, GoBinary, Install
from tundravm.systemd import DropIn, Hardening

SOURCE = Git("https://example.com/app.git", "v1")
INSTALL = Install("public", "/srv/www", mode=None, directory=True)
"""
CASES = [
    (
        'Build("site", SOURCE, command=["hugo"], packages=["hugo"], install=[INSTALL])',
        ("command", "packages", "install"),
    ),
    ('User("app", groups=["tdx"])', ("groups",)),
    ('DropIn(after=["network-online.target"], write_paths=["/a"])', ("after", "write_paths")),
    (
        'Hardening(devices=["/dev/kvm"], capabilities=["CAP_SYS_TIME"], syscalls=["@clock"])',
        ("devices", "capabilities", "syscalls"),
    ),
    ('GoBinary("app", SOURCE, tags=["netgo"])', ("tags",)),
    ('CargoBinary("app", SOURCE, features=["tls"])', ("features",)),
]


@pytest.mark.parametrize(("call", "fields"), CASES, ids=[c.partition("(")[0] for c, _ in CASES])
def test_a_list_argument_is_kept_as_a_tuple(call: str, fields: tuple[str, ...]) -> None:
    namespace: dict[str, object] = {}
    exec(HEADER + f"value = {call}\n", namespace)  # noqa: S102
    assert all(isinstance(getattr(namespace["value"], name), tuple) for name in fields)


def _copy_present(source: str, target: str) -> None:
    with contextlib.suppress(FileNotFoundError):  # a running mypy's temporary file
        shutil.copy2(source, target)


def test_mypy_accepts_every_list_argument(tmp_path: Path) -> None:
    snippet = tmp_path / "lists.py"
    lines = [f"value_{i} = {call}" for i, (call, _) in enumerate(CASES)]
    snippet.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    cache = tmp_path / "mypy-cache"
    if WARM_CACHE.is_dir():  # the gate's `mypy .` just checked the package: check the snippet
        shutil.copytree(WARM_CACHE, cache, copy_function=_copy_present)
    out, err, status = api.run(["--strict", "--cache-dir", str(cache), str(snippet)])
    assert status == 0, out + err
