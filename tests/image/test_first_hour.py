# mypy: disable-error-code="arg-type, call-arg, list-item, misc"
"""The mistakes a new user makes in their first hour: each error names the public object
and keyword, and its ``fix:`` line says what to write instead."""

from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from pathlib import Path

import pytest

import tundravm
from tundravm import File, Image, Package, Profile, Service
from tundravm.cli import main
from tundravm.components.network import Egress, Network
from tundravm.errors import TdxError
from tundravm.ports import TCP
from tundravm.secrets import Credential, Secret
from tundravm.sources import Git, GoBinary, Local
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import Every

from .conftest import STARTER_SOURCE

APP = ["/usr/bin/app"]
DATA = Volume("data", unlock=OperatorKey())


EDITS_A_MISSING_SERVICE = """\
from tundravm import Image, Profile, Service

image = Image(
    "x",
    services=[Service("app", ["/usr/bin/app"])],
    profiles={"dev": Profile(changes=[Service("nope", ["/usr/bin/app"]).ref.edit(user="x")])},
)
"""


def _cli(here: Path, *argv: str, source: str = STARTER_SOURCE) -> str:
    (here / "demo.py").write_text(source, encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        main([*argv, str(here / "demo.py")], stdout=out)
    return out.getvalue()


ROWS: list[tuple[str, Callable[[Path], object], str, str]] = [
    ("keyword-type", lambda _: Service("app", APP, expose=8080), 'Service("app")', "[TCP(8080)]"),
    (
        "argv-a-string",
        lambda _: Service("app", "/usr/bin/app --port 8080"),
        "argv= must be a list",
        'argv=["/usr/bin/app", "--port", "8080"]',
    ),
    (
        "keyword-misspelt",
        lambda _: Service("app", APP, enviroment={"A": "b"}),
        'Service("app") takes no keyword enviroment=',
        "environment=",
    ),
    (
        "component-keyword-unknown",
        lambda _: Network(egres=Egress()),
        "Network takes no keyword egres=",
        "Network(egress=...)",
    ),
    (
        "component-positional",
        lambda _: Network(True),
        "Network takes keywords only",
        "Network(dhcp=...",
    ),
    (
        "port-twice",
        lambda _: Image(
            "x", services=[Service("a", APP, expose=[TCP(80)]), Service("b", APP, expose=[TCP(80)])]
        ),
        'Service("a") and Service("b") both expose',
        "expose=[TCP(port=80)] on one of them",
    ),
    (
        "port-twice-in-one-service",
        lambda _: Service("a", APP, expose=[TCP(80), TCP(80)]),
        "expose= lists TCP(port=80) twice",
        "expose=[TCP(port=80)]",
    ),
    (
        "secret-unbound",
        lambda _: Service("app", [*APP, f"--jwt={Secret('jwt').path}"]),
        "argv= reads credential 'jwt'",
        "credentials=[secret]",
    ),
    (
        "local-missing",
        lambda here: tundravm.lower(
            Image("x", services=[Service("app", [GoBinary(Local("nope"))])])
        ),
        "is not a directory",
        "relative to the image file",
    ),
    ("production-without-lock", lambda here: _cli(here, "build"), "No lockfile", "tundravm lock"),
    (
        "git-without-ref",
        lambda _: Git("https://example.com/app.git"),
        "Git needs ref=",
        'ref="v1.2.3"',
    ),
    ("git-empty-ref", lambda _: Git("https://example.com/app.git", ""), "ref", "tundravm lock"),
    (
        "service-without-program",
        lambda _: Service("app"),
        'Service("app") needs argv=',
        'argv=["/usr/bin/app"',
    ),
    (
        "service-empty-argv",
        lambda _: Service("app", []),
        "argv= is empty",
        'GoBinary(Local("app"))',
    ),
    (
        "program-not-a-path",
        lambda _: Service("app", ["app"]),
        "not a program path",
        '"/usr/bin/app"',
    ),
    (
        "volume-name-clash",
        lambda _: Image(
            "x",
            services=[
                Service("a", APP, state=DATA.directory("a")),
                Service(
                    "b",
                    APP,
                    state=Volume("data", unlock=OperatorKey(), mount="/srv").directory("b"),
                ),
            ],
        ),
        'Volume("data")',
        "Declare it once",
    ),
    (
        "volume-without-unlock",
        lambda _: Volume("data"),
        'Volume("data") needs unlock=',
        "OperatorKey()",
    ),
    (
        "profile-edits-a-missing-service",
        lambda here: _cli(here, "check", source=EDITS_A_MISSING_SERVICE),
        "edits Service('nope'), which it does not inherit",
        "demo.py:6",
    ),
    (
        "profile-extends-a-missing-one",
        lambda _: Image("x", profiles={"dev": Profile(extends="staging")}),
        "extends 'staging'",
        "Declared profiles",
    ),
    (
        "instance-key-as-credential",
        lambda _: Credential("key", "/run/tundravm/instance-key.pem"),
        "is the instance key",
        "InstanceKey()",
    ),
    (
        "file-source-missing",
        lambda here: tundravm.lower(Image("x", files=[File.copy(here / "nope.txt", "/etc/x")])),
        'File("/etc/x"): cannot read',
        "working directory",
    ),
    (
        "file-path-relative",
        lambda _: File.text("etc/app.conf", "x"),
        "path=",
        'path="/etc/app.conf"',
    ),
    ("file-mode-a-string", lambda _: File("/etc/x", "a", mode="644"), "mode=", "mode=0o644"),
    ("port-a-string", lambda _: TCP("8080"), "not a port number", "TCP(8080)"),
    (
        "hardening-misspelt",
        lambda _: Service("app", APP, hardening="relax"),
        "hardening=",
        '"relaxed"',
    ),
    ("state-a-path", lambda _: Service("app", APP, state="/var/lib/app"), "state=", 'state="app"'),
    (
        "environment-name",
        lambda _: Service("app", APP, environment={"1A": "x"}),
        "not a variable name",
        "environment={",
    ),
    ("packages-a-string", lambda _: Image("x", packages="curl"), "packages=", 'packages=["curl"]'),
    (
        "service-in-packages",
        lambda _: Image("x", packages=[Service("app", APP)]),
        'packages= holds Service("app")',
        "services=[...]",
    ),
    (
        "two-services-one-name",
        lambda _: Image("x", services=[Service("app", APP), Service("app", ["/usr/bin/b"])]),
        "two services named 'app'",
        "its own name",
    ),
    ("target-unknown", lambda _: Image("x", targets=["aws"]), "targets=", "qemu, azure, gcp"),
    (
        "timer-with-a-port",
        lambda _: Service("app", APP, timer=Every("1h"), expose=[TCP(80)]),
        "runs on a timer, so expose=",
        "Drop expose=",
    ),
    ("requires-a-name", lambda _: Service("app", APP, requires=["db"]), "requires=", ".ready"),
    (
        "image-keyword-misspelt",
        lambda _: Image("x", service=[]),
        "takes no keyword service=",
        "services=",
    ),
    ("package-name", lambda _: Package("Bad Name"), 'Package("Bad Name")', 'Package("curl")'),
]


@pytest.mark.parametrize(
    ("action", "names", "fix"), [row[1:] for row in ROWS], ids=[row[0] for row in ROWS]
)
def test_a_first_hour_mistake_names_the_public_object_and_the_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[Path], object],
    names: str,
    fix: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    try:
        said = action(tmp_path)
    except TdxError as exc:
        said = str(exc)
        assert "\nfix: " in said, said
        what, _, rest = said.partition("\nfix: ")
        assert names in what and fix in rest, said
    else:
        assert isinstance(said, str) and names in said and fix in said, said
