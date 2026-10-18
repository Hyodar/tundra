"""The recipe and tests ``tundravm init --app go`` writes: one Go program as a service.

The recipe builds the program with ``GoBinary``, runs it as its own system user
with a state directory, and optionally keeps that state on an encrypted disk and
expects a secret an external agent delivers. The tests read the planned image
with :mod:`tundravm.testing`. Both are pure functions of :class:`AppSpec`.
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
DISK_DEVICE = "/dev/vdb"
"""The device the generated recipe names for ``--disk``: qemu's second virtio disk."""
EXTERNAL_PREFIX = "external:"


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
    delivery: str | None = None
    local: bool = False
    """*url* is a local checkout (``--path``), not a remote repository."""

    @property
    def state_dir(self) -> str:
        """Where the service keeps its state: on the disk when there is one."""
        return f"{DISK_MOUNT}/{self.name}" if self.disk else f"/var/lib/{self.name}"

    @property
    def key_port(self) -> int:
        """The port ``Secrets`` takes root's SSH key on: never the app's."""
        return 8081 if self.port == 8080 else 8080


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
    for flag, value in (("--disk", spec.disk), ("--secret", spec.secret)):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValidationError(
                f"{flag} {value!r} is not a valid name.",
                hint="Use letters, digits, `-`, `_` and `.`, starting with a letter or digit.",
            )
    if spec.secret is None and spec.delivery is not None:
        raise ValidationError("--delivery needs --secret.", hint="Pass --secret NAME too.")
    if spec.secret is not None:
        if spec.delivery is None or not spec.delivery.startswith(EXTERNAL_PREFIX):
            raise ValidationError(
                f"--secret {spec.secret} needs --delivery external:NAME.",
                hint="The built-in runtime delivers only root's SSH key; name what writes the "
                "secret, e.g. --delivery external:vault-agent.",
            )
        if not spec.delivery.removeprefix(EXTERNAL_PREFIX):
            raise ValidationError("--delivery external: names nothing.", hint="Pass external:NAME.")


_HEADER = r'''"""$title image recipe (Go app: $app as a service).

Inspect:  tundravm inspect $filename
Lint:     tundravm lint $filename
Lock:     tundravm lock $filename
Compile:  tundravm compile $filename --tree mkosi
Build:    tundravm bake $filename --build build
"""

'''

_CONSTANTS = r"""
APP = "$app"
PORT = $port
STATE_DIR = "$state_dir"
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
# The encrypted disk holding STATE_DIR. Its key is generated at first boot and kept in the
# TPM; that boot formats the disk (format="on_initialize").
# DISK_DEVICE is the device the platform attaches it as: /dev/vdb is qemu's second virtio
# disk. Set yours (a /dev/disk/by-id/ path on a cloud) before building for that platform.
DISK_DEVICE = "$disk_device"
key = Key("${disk}_key", output="/tmp/${disk}_key")
disk = Disk(
    "$disk",
    mount="$disk_mount",
    device=DISK_DEVICE,
    format="on_initialize",
    key=key,
    mapper="$disk",
    directories=(),
)
"""

_SECRET = r"""
# The secret reaches APP as a file only APP reads. tundra-tools delivers only root's
# SSH key, so "$delivery_name" writes SECRET_FILE at boot. YOU PROVIDE IT: ship or
# package what writes the file (its unit enabled), or this image waits at boot forever.
# APP needs delivery.ready: a boot step waits until SECRET_FILE exists (ready_path=);
# pass unit="NAME.service" instead to start that unit and wait for it. Secrets also
# runs tundra-tools' SSH-key step (secrets.ready), which waits each boot for an
# ed25519 key POSTed to port $key_port (docs/concepts.md: Secret delivery at boot).
SECRET_FILE = f"/run/{APP}/$secret"
# The service reads systemd's copy of it, in $$CREDENTIALS_DIRECTORY.
CREDENTIAL_FILE = f"/run/credentials/{APP}.service/$secret"
delivery = ExternalDelivery("$delivery_name", supports=("file",), ready_path=SECRET_FILE)
secrets = Secrets(
    entries=(Secret("$secret", targets=(SecretFile(SECRET_FILE, owner=APP),)),),$store
    port=$key_port,
    delivery=delivery,
)
"""

_APP = r"""
binary = GoBinary(APP, SOURCE, package=PACKAGE)

app = Fragment(
    f"{APP}-service",
    items=(
        # Runs as system user APP (declared for you), restarts on failure, and is
        # sandboxed by Hardening.strict(): it writes only to its state directory.
        Service(
            APP,
            # The flags are a guess: change them to the ones your program takes.
            f"{binary.executable} --addr=:{PORT}",
            description=APP,$state
            env={$env},$needs
        ),
    ),
)

backend = $backend_expr

recipe = Recipe(
    name="$title",
    # Debian with its kernel and systemd boot set: recipe.platform.packages lists them.
    platform=Debian("$release"),
    items=(
$items    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        # Dev tools and root login: release=False keeps it out of releases.
        Variant("dev", inherits="default", add=DevTools(), release=False),
    ),
)
"""


def _imports(spec: AppSpec, backend_import: str) -> str:
    names = ["Debian", "Fragment", "Git", "Package", "Recipe", "Service", "Variant"]
    utils = ["DevTools", "GoBinary"]
    if spec.disk:
        names += ["Disk", "Key", "RuntimeTools"]
    if spec.secret:
        names += ["ExternalDelivery", "RuntimeTools", "Secret", "SecretFile", "Secrets"]
    if spec.disk or spec.secret:
        utils.insert(0, "TUNDRA_TOOLS")
    listed = "".join(f"    {name},\n" for name in sorted(set(names)))
    return (
        f"from tundravm import (\n{listed})\n{backend_import}\n"
        f"from tundravm.declarative.utils import {', '.join(utils)}\n"
    )


def secret_env(secret: str) -> str:
    """The variable the service reads *secret*'s file path from: ``API_TOKEN_FILE``."""
    return "".join(c if c.isalnum() else "_" for c in secret).upper() + "_FILE"


_NEEDS = {
    (False, False): "",
    (True, False): "\n            # Starts once the disk is mounted at boot.\n"
    "            needs=(disk.ready,),",
    (
        True,
        True,
    ): "\n            # Starts once the disk is mounted and the secret delivered, without\n"
    "            # waiting for the SSH key.\n"
    "            needs=(disk.ready, delivery.ready),",
    (
        False,
        True,
    ): "\n            # Starts once the secret is delivered, not waiting for the SSH key.\n"
    "            needs=(delivery.ready,),",
}
"""The service's ``needs=``, by (disk, secret): what it waits for at boot."""

_CREDENTIAL = (
    "\n            # LoadCredential=: systemd hands SECRET_FILE over as CREDENTIAL_FILE.\n"
    "            credentials={$secret_name: SECRET_FILE},"
)
_STATE = {
    False: "\n            # StateDirectory=: systemd creates STATE_DIR, owned by APP, sets\n"
    "            # $STATE_DIRECTORY and makes it the working directory.\n"
    "            state=APP,",
    True: "\n            # STATE_DIR is on the disk, not under /var/lib: the sandbox may write it\n"
    "            # (write_paths); because it is on the disk, the unit also waits for the\n"
    "            # mount and creates it, owned by APP, before the service starts.\n"
    "            write_paths=(STATE_DIR,),",
}
"""The service's state directory fields, by disk: ``StateDirectory=`` or a disk path."""


def render_app_recipe(
    spec: AppSpec, *, title: str, filename: str, release: str, backend: tuple[str, str]
) -> str:
    """The recipe source ``tundravm init --app go`` writes for *spec*."""
    backend_import, backend_expr = backend
    env = ['"STATE_DIRECTORY": STATE_DIR'] if spec.disk else []
    items = ['Package("ca-certificates")']
    sections = _CONSTANTS
    if spec.disk:
        sections += _DISK
        items += ['Package("cryptsetup")', "key", "disk"]
    if spec.secret:
        sections += _SECRET
        env.append(f'"{secret_env(spec.secret)}": CREDENTIAL_FILE')
        items.append("secrets")
    if spec.disk or spec.secret:
        items.append("# The key, disk and secrets steps run the tundra-tools binaries.")
        items.append("RuntimeTools(TUNDRA_TOOLS)")
    items += ["binary", "app"]
    delivery = (spec.delivery or "").removeprefix(EXTERNAL_PREFIX)
    source = _HEADER + _imports(spec, backend_import) + sections + _APP
    return Template(source).substitute(
        title=title,
        filename=filename,
        release=release,
        backend_expr=backend_expr,
        app=spec.name,
        port=spec.port,
        state_dir=spec.state_dir,
        source_comment=_LOCAL_COMMENT if spec.local else _REMOTE_COMMENT,
        url=spec.url,
        ref=spec.ref,
        package=spec.package,
        disk=spec.disk or "",
        disk_device=DISK_DEVICE,
        disk_mount=DISK_MOUNT,
        secret=spec.secret or "",
        delivery_name=delivery,
        key_port=spec.key_port,
        store="\n    store=disk," if spec.disk else "",
        needs=_NEEDS[bool(spec.disk), bool(spec.secret)],
        state=_STATE[bool(spec.disk)]
        + (_CREDENTIAL.replace("$secret_name", f'"{spec.secret}"') if spec.secret else ""),
        env=", ".join(env),
        items="".join(
            f"        {item}\n" if item.startswith("#") else f"        {item},\n" for item in items
        ),
    )


_TESTS = r'''"""Tests for $filename: what the image ships for $app, read from the planned image."""

from pathlib import Path

import tundravm
from tundravm.testing import assert_clean, fragment_plan, plan

PROJECT = Path(__file__).resolve().parent.parent
RECIPE = tundravm.load_recipe(PROJECT / "$filename")
IMAGE = plan(RECIPE)  # the default variant, compiled in memory
ITEMS = {getattr(item, "name", None): item for item in RECIPE.items}
APP = "$app"
PORT = $port
EXECUTABLE = "/usr/bin/$app"


def test_lints_clean() -> None:
    # `tundravm lock $filename` pins the source builds and clears source-unpinned.
    assert_clean(RECIPE, allow=("source-unpinned",))


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
$extra

def test_service_fragment_plans_on_its_own() -> None:
    # The service with only what it needs under it, on a bare test platform.
    needs = ($include)
    image = fragment_plan(ITEMS[f"{APP}-service"], include=needs)
    assert_clean(image, allow=("source-unpinned",))
'''

_DISK_TEST = r"""

def test_state_lives_on_the_encrypted_disk() -> None:
    service = IMAGE.service(APP)
    assert service.environment["STATE_DIRECTORY"].startswith("$disk_mount/")
    # The unit is ordered after the boot step that mounts the disk.
    assert "disks" in " ".join(service.values("Unit", "After"))
"""

_SECRET_TEST = r"""

def test_service_reads_the_secret_file() -> None:
    environment = IMAGE.service(APP).environment
    assert environment["$secret_env"] == "/run/credentials/$app.service/$secret"
"""


def render_app_tests(spec: AppSpec, *, filename: str) -> str:
    """``tests/test_<name>.py`` for the recipe :func:`render_app_recipe` writes."""
    extra = (_DISK_TEST if spec.disk else "") + (_SECRET_TEST if spec.secret else "")
    return Template(_TESTS.replace("$extra", extra)).substitute(
        filename=filename,
        app=spec.name,
        port=spec.port,
        disk_mount=DISK_MOUNT,
        include=f'ITEMS[APP], ITEMS["{spec.disk}_key"], ITEMS["{spec.disk}"]'
        + (', ITEMS["secrets"]' if spec.secret else "")
        if spec.disk
        else 'ITEMS[APP], ITEMS["secrets"]'
        if spec.secret
        else "ITEMS[APP],",
        secret=spec.secret or "",
        secret_env=secret_env(spec.secret or "x"),
    )


__all__ = [
    "APP_LANGUAGES",
    "AppSpec",
    "check_spec",
    "local_source",
    "parse_source",
    "render_app_recipe",
    "render_app_tests",
    "secret_env",
]
