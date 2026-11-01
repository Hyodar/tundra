"""Shared fixtures of the CLI tests: a project directory, the CLI in-process, a build."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import fake_ovmf
from tundravm import instances
from tundravm.builds import Build
from tundravm.cli import main
from tundravm.templates import render_recipe_template

STARTER = """\
from tundravm import File, Image, Service
from tundravm.components import DevAccess
from tundravm.ports import TCP

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
    dev=DevAccess(password="tdx"),
)
"""
"""Gate 4's starter, with a dev profile."""

PYPROJECT = '[project]\nname = "demo"\nversion = "0"\n\n[tool.tundravm]\nimage = "demo.py"\n'


@pytest.fixture
def tmp_path(short_tmp_path: Path) -> Path:
    """A CLI test's project may run an instance whose QEMU monitor socket lives under it:
    keep the path within AF_UNIX's 108 bytes under ``-n auto`` too."""
    return short_tmp_path


@pytest.fixture(autouse=True)
def _short_instance_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake QEMU starts, prints and stops at once, and nothing listens on a deployment's
    address: wait milliseconds, not the seconds a real guest needs."""
    monkeypatch.setattr("tundravm.instances.LAUNCH_GRACE", 0.05)
    monkeypatch.setattr("tundravm.instances.POLL", 0.01)
    monkeypatch.setattr("tundravm.instances.STOP_POLL", 0.01)
    monkeypatch.setattr("tundravm.instances.CONNECT_TIMEOUT", 0.05)


@dataclass(frozen=True, slots=True)
class Result:
    code: int
    out: str
    err: str


Cli = Callable[..., Result]


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Cli:
    """Run ``tundravm ARGV`` in-process: exit code, stdout, stderr."""

    def run(*argv: str | Path) -> Result:
        capsys.readouterr()
        out = io.StringIO()
        code = main([str(arg) for arg in argv], stdout=out)
        return Result(code, out.getvalue(), capsys.readouterr().err)

    return run


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory holding the starter as ``demo.py``; the tests run from it."""
    (tmp_path / "demo.py").write_text(STARTER, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def prover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project holding the prover template (a volume an operator unlocks, a secret)."""
    source = render_recipe_template(
        title="prover", filename="prover.py", release="trixie", template="prover"
    )
    (tmp_path / "prover.py").write_text(source, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "prover"\nversion = "0"\n\n[tool.tundravm]\nimage = "prover.py"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    import tundravm._source as source_module

    def resolve(source: object) -> str:  # the EfiStub package: an http source, by sha256
        return "1" * 40 if hasattr(source, "ref") else "a" * 64

    monkeypatch.setattr(source_module, "default_resolver", resolve)
    return tmp_path


@pytest.fixture
def built(project: Path, cli: Cli) -> Build:
    """The production build of the starter, locked first."""
    assert cli("lock").code == 0
    result = cli("build")
    assert result.code == 0, result.err
    return Build.open((project / "build" / "demo" / "latest-production").resolve())


@pytest.fixture
def dev_built(built: Build, cli: Cli) -> Build:
    """The development build of the starter (the production one exists too)."""
    result = cli("build", "--dev")
    assert result.code == 0, result.err
    return Build.open((Path("build") / "demo" / "latest-development").resolve())


FAKE_QEMU = """\
#!{python}
import os, signal, socket, sys, time
argv = sys.argv[1:]
def value(flag):
    return argv[argv.index(flag) + 1]
console = value("-serial").removeprefix("file:")
monitor = value("-monitor").removeprefix("unix:").split(",")[0]
with open(value("-pidfile"), "w") as handle:
    handle.write(str(os.getpid()))
with open(console, "w") as handle:
    handle.write("[    1.0] Kernel command line: console=ttyS0\\nReached target minimal.target\\n")
    handle.write(os.environ.get("FAKE_QEMU_CONSOLE", ""))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
server = socket.socket(socket.AF_UNIX)
server.bind(monitor)
server.listen(1)
while True:
    conn, _ = server.accept()
    if b"quit" in conn.recv(64):
        sys.exit(0)
"""
"""A ``qemu-system-x86_64`` that writes a console (then what ``$FAKE_QEMU_CONSOLE`` holds,
the probe's lines in a test that waits for them), records its pid and quits on its
monitor's ``quit``."""


@pytest.fixture
def qemu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The fake ``qemu-system-x86_64`` first on PATH, and fake OVMF images."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "qemu-system-x86_64"
    script.write_text(FAKE_QEMU.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake_ovmf(tmp_path, monkeypatch)
    started: list[int] = []
    real = instances.launch

    def launch(*args: object, **kwargs: object) -> instances.Instance:
        found = real(*args, **kwargs)  # type: ignore[arg-type]
        started.append(int(found.data["pid"]))
        # the fake writes its console, then binds the monitor: a test that appends to the
        # console must not race the fake's truncating open on a loaded host
        deadline = time.monotonic() + 30
        while not (found.path / instances.MONITOR).exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        return found

    monkeypatch.setattr(instances, "launch", launch)
    yield script
    for pid in started:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def receipt(project: Path, name: str) -> dict[str, object]:
    """``instance.json`` of the instance *name* under *project*."""
    path = project / ".tundravm" / "instances" / name / "instance.json"
    payload: dict[str, object] = json.loads(path.read_text())
    return payload
