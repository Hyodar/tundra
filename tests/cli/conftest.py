"""Shared fixtures of the CLI tests: a project directory, the CLI in-process, a build."""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

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
    """The production build of the starter."""
    result = cli("build")
    assert result.code == 0, result.err
    return Build.open((project / "build" / "demo" / "latest-production").resolve())


@pytest.fixture
def dev_built(built: Build, cli: Cli) -> Build:
    """The development build of the starter (the production one exists too)."""
    result = cli("build", "--dev")
    assert result.code == 0, result.err
    return Build.open((Path("build") / "demo" / "latest-development").resolve())
