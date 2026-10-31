"""An image and its tests for one Go program run as a service (:func:`render_app_recipe`).

The image builds the program with ``GoBinary``, runs it as its own system user with a
state directory, and optionally keeps that state on a :class:`~tundravm.Volume` an
operator unlocks and reads a :class:`~tundravm.Secret` delivered after attestation. The
tests read the planned image with :mod:`tundravm.testing`. Both are pure functions of
:class:`AppSpec`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from string import Template

from .errors import ValidationError

APP_LANGUAGES = ("go",)
"""The languages ``init --app`` scaffolds."""

DEFAULT_PORT = 8080
DISK_MOUNT = "/persistent"


@dataclass(frozen=True, slots=True)
class AppSpec:
    """What ``init --app go`` was asked for; *url* and *ref* name the Go source."""

    name: str
    url: str
    ref: str
    package: str = "."
    port: int = DEFAULT_PORT
    disk: str | None = None
    secret: str | None = None
    local: bool = False
    """*url* is a local checkout (``--path``), not a remote repository."""

    @property
    def state_dir(self) -> str:
        """Where the service keeps its state: on the volume when there is one."""
        return f"{DISK_MOUNT}/{self.name}" if self.disk else f"/var/lib/{self.name}"


def parse_source(text: str) -> tuple[str, str]:
    """``URL@REF`` as ``(url, ref)``; ``git@host:owner/repo@v1`` keeps its user part."""
    url, at, ref = text.rpartition("@")
    if not at or not ref or not url or not any(mark in url for mark in "/:"):
        raise ValidationError(
            f"--source {text!r} names no ref.",
            hint="Pass --source URL@REF, e.g. https://github.com/acme/app@v1.2.0.",
        )
    return url, ref


def local_source(path: Path) -> tuple[str, str]:
    """A git checkout at *path* as ``(absolute path, branch or commit)``."""

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None

    if not path.is_dir() or git("rev-parse", "--verify", "HEAD") is None:
        raise ValidationError(
            f"--path {path} is not a git checkout with a commit.",
            hint="Builds check out a git ref: commit the code there (git init && git commit), "
            "or push it and pass --source URL@REF.",
        )
    ref = git("symbolic-ref", "--short", "HEAD") or git("rev-parse", "HEAD")
    assert ref is not None
    return str(path.resolve()), ref


def check_spec(spec: AppSpec) -> None:
    """Reject what the generated recipe could not run."""
    if not 1024 <= spec.port <= 65535:
        raise ValidationError(
            f"--port {spec.port} is outside 1024-65535.",
            hint="The service runs as its own user, which cannot bind a port below 1024.",
        )
    if spec.disk is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,10}", spec.disk):
        raise ValidationError(
            f"--disk {spec.disk!r} is not a valid volume name.",
            hint="Use 1 to 11 of a-z, 0-9 and '-': it names the attachment tundravm-NAME.",
        )
    if spec.secret is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", spec.secret):
        raise ValidationError(
            f"--secret {spec.secret!r} is not a valid secret name.",
            hint="Use lower-case letters, digits, `-`, `_` and `.`, starting with a letter or "
            "digit.",
        )


_HEADER = r'''"""$title image ($app as a service, built from its Go source).

Check:  tundravm check $filename
Lock:   tundravm lock $filename
Build:  tundravm build $filename
Run:    tundravm run --mode simulated --dev
"""

'''

_CONSTANTS = r"""
APP = "$app"
PORT = $port
$source_comment
SOURCE = Git(
    "$url",
    ref="$ref",
)
PACKAGE = "$package"
"""

_LOCAL_COMMENT = (
    "# A local checkout: builds take its committed REF, never uncommitted edits. Its path is\n"
    "# part of the compiled tree, so point SOURCE at the pushed repository before you share."
)
_REMOTE_COMMENT = "# The program's repository and ref; `tundravm lock` pins the ref to a commit."

_DISK = r"""
# APP's state lives on an encrypted volume an operator unlocks on every boot, after
# attesting the instance (`tundravm secrets unlock INSTANCE $disk`); deploy attaches it
# as tundravm-$disk. The service waits until it is open.
VOLUME = Volume("$disk", mount="$disk_mount", unlock=OperatorKey())
"""

_SECRET = r"""
# A secret delivered after attestation (`tundravm secrets deliver INSTANCE $secret=@FILE`)
# and handed to APP as a systemd credential: nothing is written into the image, and the
# service waits until it has arrived. APP reads it at SECRET.path.
SECRET = Secret("$secret")
"""

_APP = r"""
binary = GoBinary(APP, SOURCE, package=PACKAGE)

image = Image(
    "$title",
    base=Debian("$release"),
    packages=[Package("ca-certificates")],
    services=[
        # Runs as system user APP, restarts on failure, sandboxed by the "static" preset.
        # expose= is what the firewall opens.
        Service(
            APP,
            # The flags are a guess: change them to the ones your program takes.
            [binary, f"--addr=:{PORT}"$argv],
            expose=[TCP(PORT)],
            state=$state,$credentials
        ),
    ],$slots
    # A non-production "dev" profile: debugging tools and a serial console, never shipped.
    dev=DevAccess(password="tdx"),
)
"""


def _imports(spec: AppSpec) -> str:
    names = ["TCP", "Debian", "DevAccess", "Git", "GoBinary", "Image", "Package", "Service"]
    if spec.disk:
        names += ["OperatorKey", "Volume"]
    if spec.secret:
        names += ["Secret"]
    ordered = sorted(names, key=lambda name: (not name.isupper(), name))
    listed = "".join(f"    {name},\n" for name in ordered)
    return f"from tundravm import (\n{listed})\n"


def render_app_recipe(spec: AppSpec, *, title: str, filename: str, release: str) -> str:
    """The image source ``tundravm init --app go`` writes for *spec*."""
    sections = _CONSTANTS + (_DISK if spec.disk else "") + (_SECRET if spec.secret else "")
    slots = ""
    if spec.disk:
        slots += "\n    volumes=[VOLUME],"
    if spec.secret:
        slots += "\n    secrets=[SECRET],"
    source = _HEADER + _imports(spec) + sections + _APP
    return Template(source).substitute(
        title=title,
        filename=filename,
        release=release,
        app=spec.name,
        port=spec.port,
        source_comment=_LOCAL_COMMENT if spec.local else _REMOTE_COMMENT,
        url=spec.url,
        ref=spec.ref,
        package=spec.package,
        disk=spec.disk or "",
        disk_mount=DISK_MOUNT,
        secret=spec.secret or "",
        argv=f', f"--{spec.secret}-file={{SECRET.path}}"' if spec.secret else "",
        state="VOLUME.directory(APP)" if spec.disk else "APP",
        credentials="\n            credentials=[SECRET]," if spec.secret else "",
        slots=slots,
    )


_TESTS = r'''"""Tests for $filename: what the image ships for $app, read from the planned image."""

from pathlib import Path

from tundravm.recipe import load_recipe
from tundravm.testing import assert_clean, plan

PROJECT = Path(__file__).resolve().parent.parent
RECIPE = load_recipe(PROJECT / "$filename")
IMAGE = plan(RECIPE)  # the production profile, compiled in memory
APP = "$app"
PORT = $port
EXECUTABLE = "/usr/bin/$app"


def test_lints_clean() -> None:
    # `tundravm lock $filename` pins the source builds and clears source-unpinned.
    assert_clean(IMAGE, allow=("source-unpinned",))


def test_binary_is_built_and_installed() -> None:
    build = IMAGE.builds[APP]
    assert [install.destination for install in build.install] == [EXECUTABLE]


def test_service_runs_the_binary_on_the_port() -> None:
    command = IMAGE.service(APP).exec_start or ""
    assert command.startswith(EXECUTABLE)
    assert f":{PORT}" in command


def test_service_runs_as_its_own_user() -> None:
    # Service declares the system user for you when user= is left out.
    assert IMAGE.service(APP).user == APP


def test_service_is_enabled() -> None:
    service = IMAGE.service(APP)
    assert service.enabled
    assert service.wanted_by == ("minimal.target",)


def test_tee_baseline_is_in_the_image() -> None:
    # Implied: authenticated (NTS) time, a RAM-only journal, kernel sysctl hardening.
    assert "chrony" in IMAGE.packages
    assert " nts\n" in IMAGE.file("/etc/chrony/chrony.conf").text
    journal = IMAGE.file("/etc/systemd/journald.conf.d/50-tundravm-tee.conf").text
    assert "Storage = volatile" in journal
    assert "kexec_load_disabled = 1" in IMAGE.file("/etc/sysctl.d/50-tundravm-tee.conf").text
    # DHCP on every NIC, and a firewall that drops inbound traffic except APP's port.
    assert "DHCP=yes" in IMAGE.file("/etc/systemd/network/50-tundravm.network").text
    assert f"tcp dport {PORT} accept" in IMAGE.file("/etc/nftables.conf").text
$extra'''

_DISK_TEST = r"""

def test_state_lives_on_the_volume() -> None:
    service = IMAGE.service(APP)
    assert service.environment["STATE_DIRECTORY"] == "$disk_mount/$app"
    # The unit waits until the volume is open and its directory exists.
    after = " ".join(service.values("Unit", "After")).split()
    assert "runtime-init-volume-$disk.service" in after
"""

_SECRET_TEST = r"""

def test_service_reads_the_delivered_secret() -> None:
    service = IMAGE.service(APP)
    (binding,) = service.values("Service", "LoadCredential")
    assert binding == "$secret:/run/tundravm/credentials/$secret"
    # The unit waits until the secret has been delivered.
    after = " ".join(service.values("Unit", "After")).split()
    assert "credential-$secret.service" in after
"""


def render_app_tests(spec: AppSpec, *, filename: str) -> str:
    """``tests/test_<name>.py`` for the image :func:`render_app_recipe` writes."""
    extra = (_DISK_TEST if spec.disk else "") + (_SECRET_TEST if spec.secret else "")
    return Template(_TESTS.replace("$extra", extra)).substitute(
        filename=filename,
        app=spec.name,
        port=spec.port,
        disk=spec.disk or "",
        disk_mount=DISK_MOUNT,
        secret=spec.secret or "",
    )


__all__ = [
    "APP_LANGUAGES",
    "AppSpec",
    "check_spec",
    "local_source",
    "parse_source",
    "render_app_recipe",
    "render_app_tests",
]
