"""File templates for ``tundravm new``: the starter images and the project scaffold.

Each image template is assembled from the sections below with
:class:`string.Template` (``$name`` placeholders; the generated Python holds no
``$``), so the rendered source is a pure function of its arguments.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from string import Template
from typing import Literal
from urllib.parse import unquote, urlparse

from .errors import ValidationError
from .project import render_table

EXPLICIT_BACKENDS: dict[str, tuple[str, str]] = {
    "lima": ("LimaMkosiBackend", 'LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")'),
    "local": ("LocalLinuxBackend", "LocalLinuxBackend()"),
}
"""``render_recipe_template(backend=KIND)``: the class and the instance a file that must build
with one backend, whatever the host, binds to ``backend``."""

TemplateName = Literal["minimal", "service", "cloud", "prover"]

TEMPLATES: dict[str, str] = {
    "minimal": "the starter: one package, one file, one service, one qemu target",
    "service": "an App component (a web page busybox httpd serves from a sandboxed service, "
    "its readiness and a lint rule) plus a dev profile",
    "cloud": "service with one UKI for qemu, azure and gcp, and an EFI stub pinned to a "
    "Debian snapshot",
    "prover": "cloud with the app behind an attested TLS ingress, its state on a volume an "
    "operator unlocks, and a token delivered after attestation",
}
"""Template name -> one-line description; ``new --template`` offers ``service`` and
``prover``, and the tests render all four."""

DEFAULT_TEMPLATE: TemplateName = "service"

_HEADER = r'''"""$title image ($template template).

Check:  uv run tundravm check $filename
Lock:   uv run tundravm lock $filename
Build:  uv run tundravm build $filename --dev
Run:    uv run tundravm run --mode simulated --dev
"""

'''

_MINIMAL = r"""from tundravm import File, Image, Service
from tundravm.components import DevAccess
from tundravm.ports import TCP

image = Image(
    "$title",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>$title</h1>\n")],
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

_SERVICE_IMPORTS = (
    "from tundravm import Debian, File, Image, Package, Service\n"
    "from tundravm.components import Component, DevAccess, Diagnostic, Endpoint, Resolved\n"
    "from tundravm.ports import TCP\n"
    "from tundravm.systemd import TcpProbe\n"
)

_APP_IMPORTS = {
    "service": _SERVICE_IMPORTS,
    "cloud": _SERVICE_IMPORTS,
    "prover": (
        "from tundravm import Debian, File, Image, Package, Service\n"
        "from tundravm.components import Component, DevAccess, Diagnostic, Endpoint, Resolved\n"
        "from tundravm.ports import AttestedTLS\n"
        "from tundravm.secrets import Secret\n"
        "from tundravm.storage import OperatorKey, Volume, VolumeDirectory\n"
        "from tundravm.systemd import TcpProbe\n"
    ),
}

_APP = r'''
APP_VERSION = "0.1.0"
APP_PORT = 8080
WWW = "/srv/www"
$constants

class App(Component):
    """The app: a web page busybox httpd serves, its readiness, and a lint rule.

    Swap in your program: ship it with a GoBinary, a CargoBinary or a Package and point
    the service at it. Component name: ``app``.
    """

    version: str
    port: int

    @property
    def http(self) -> Endpoint:
        """Where the app answers, with its readiness: what a caller wires to."""
        return Endpoint("127.0.0.1", self.port, ready=self.service("app").ready)

    def compose(self) -> Iterator[Package | File | Service]:
        # Debian's busybox has the httpd applet: a real server with nothing to download.
        yield Package("busybox")
        yield File.text(f"{WWW}/index.html", self.page())
        # Runs as system user "app" (declared for you), sandboxed by the "static" preset,
        # restarted on failure. expose= is what the image's firewall opens; readiness= is
        # what another service's requires=[app.http] would wait for.
        yield Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", str(self.port), "-h", WWW],
            expose=[TCP(self.port)],
            readiness=TcpProbe(self.port),
        )

    def page(self) -> str:
        """The index page: ``curl http://VM:PORT/`` answers with it."""
        return f"<!doctype html>\n<title>$title</title>\n<h1>$title {self.version}</h1>\n"

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", "error", message),)
'''

_SERVICE_RECIPE = r"""

image = Image(
    "$title",
    base=Debian("$release"),
    # Implied with no further words: Debian from the locked snapshot, a measured
    # UKI, NTS time, a volatile journal, sysctl hardening, DHCP, a firewall that opens only
    # what a service exposes, one hardened user per service and the evidence service.
    packages=[Package("ca-certificates")],
    components=[App(version=APP_VERSION, port=APP_PORT)],
    # A non-production "dev" profile: debugging tools, and a serial console with root
    # password "tdx". Build it with --dev; production never has it.
    dev=DevAccess(password="tdx"),
)
"""

_CLOUD_CONSTANTS = r"""
# Packages and the EFI stub come from this snapshot.debian.org snapshot, so rebuilds see the
# same archive. The stub version is the systemd-boot-efi trixie ships in it.
SNAPSHOT = "20251113T083151Z"
EFI_STUB_VERSION = "257.8-1~deb13u1"
"""

_CLOUD_RECIPE = r"""

image = Image(
    "$title",
    base=Debian("$release", snapshot=SNAPSHOT, efi_stub=EFI_STUB_VERSION),
    packages=[Package("ca-certificates")],
    components=[App(version=APP_VERSION, port=APP_PORT)],
    # One UKI, one set of measurements, three disks: the cloud glue is gated at boot.
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
"""

_PROVER_APP = r'''
APP_VERSION = "0.1.0"
APP_PORT = 8080
WWW = "/srv/www"
$constants

class App(Component):
    """The app: a page busybox httpd serves on the loopback, its state on an encrypted
    volume and a token delivered after attestation.

    Swap in your program: ship it with a GoBinary, a CargoBinary or a Package and point
    the service at it; it reads the token at ``self.token.path``. Component name: ``app``.
    """

    version: str
    port: int
    state: VolumeDirectory
    token: Secret

    @property
    def http(self) -> Endpoint:
        """Where the app answers on the loopback, with its readiness: what the ingress fronts."""
        return Endpoint("127.0.0.1", self.port, ready=self.service("app").ready)

    def compose(self) -> Iterator[Package | File | Service]:
        yield Package("busybox")
        yield File.text(f"{WWW}/index.html", self.page())
        # It listens on the loopback and exposes no port: the attested ingress is the way
        # in. state= waits until the volume is unlocked, credentials= until the token is
        # delivered; systemd hands the token over as a credential, never in the image.
        yield Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", f"127.0.0.1:{self.port}", "-h", WWW],
            readiness=TcpProbe(self.port),
            state=self.state,
            credentials=[self.token],
        )

    def page(self) -> str:
        """The index page the ingress serves."""
        return f"<!doctype html>\n<title>$title</title>\n<h1>$title {self.version}</h1>\n"

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", "error", message),)
'''

_PROVER_RECIPE = r"""

# An encrypted volume an operator unlocks on every boot, after attesting the instance
# (`tundravm secrets unlock INSTANCE data`); deploy attaches it as tundravm-data.
data = Volume("data", mount="/persistent", unlock=OperatorKey())
# A secret delivered after attestation (`tundravm secrets deliver INSTANCE api-token=@f`)
# and read as a systemd credential: nothing is written into the image.
token = Secret("api-token", rotation="restart")
app = App(version=APP_VERSION, port=APP_PORT, state=data.directory("app"), token=token)

image = Image(
    "$title",
    base=Debian("$release", snapshot=SNAPSHOT, efi_stub=EFI_STUB_VERSION),
    packages=[Package("ca-certificates")],
    components=[app],
    volumes=[data],
    secrets=[token],
    # TLS on 443, relayed to the app on the loopback: the only way requests reach it.
    ingress=[AttestedTLS(443, upstream=app.http)],
    # One UKI, one set of measurements, three disks: the cloud glue is gated at boot.
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
"""

_SOURCES: dict[str, str] = {
    "minimal": _HEADER + _MINIMAL,
    "service": _HEADER
    + "from collections.abc import Iterator\n\n"
    + _APP_IMPORTS["service"]
    + _APP
    + _SERVICE_RECIPE,
    "cloud": _HEADER
    + "from collections.abc import Iterator\n\n"
    + _APP_IMPORTS["cloud"]
    + _APP
    + _CLOUD_RECIPE,
    "prover": _HEADER
    + "from collections.abc import Iterator\n\n"
    + _APP_IMPORTS["prover"]
    + _PROVER_APP
    + _PROVER_RECIPE,
}

RECIPE_TEMPLATE = _SOURCES[DEFAULT_TEMPLATE]
"""The default (``service``) template's ``$``-placeholder source."""

TEST_TEMPLATE = r'''"""Tests for $filename: check, compile and the committed mkosi/ tree."""

import os
from pathlib import Path

import tundravm
from tundravm.testing import UPDATE_GOLDEN_ENV, assert_clean, assert_tree, plan, read_lock

PROJECT = Path(__file__).resolve().parent.parent
IMAGE = tundravm.load(PROJECT / "$filename")
PROFILES = ($profiles)
GOLDEN = PROJECT / "mkosi"
LOCKFILE = PROJECT / "build" / "tundravm.lock"


def test_lints_clean() -> None:
    for profile in PROFILES:
$lint_call


def test_compiles_every_profile() -> None:
    for profile in PROFILES:
        app = plan(IMAGE, profile).service("app")
        assert app.user == "app"


def test_matches_committed_tree() -> None:
    # mkosi/ is the committed output of `tundravm build $filename --tree-only mkosi`. After an
    # intended image change, rewrite it with: TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests
    if not GOLDEN.is_dir() and os.environ.get(UPDATE_GOLDEN_ENV) != "1":
        raise AssertionError(f"{GOLDEN} is missing: run `uv run tundravm build --tree-only mkosi`")
    lock = read_lock(LOCKFILE) if LOCKFILE.exists() else None
    assert_tree(tundravm.compile(IMAGE, lock=lock), GOLDEN)
'''

EDITABLE_INSTALL = "uv add --editable PATH/TO/tundravm"
"""How ``new`` tells users to use a local tundravm checkout (it is not on PyPI yet)."""

TUNDRAVM_REPOSITORY = "https://github.com/Hyodar/tundra"
"""The repository a scaffold installs tundravm from (it is not on PyPI)."""

TUNDRAVM_FALLBACK_REV = "master"
"""The branch a scaffold follows when the running tundravm's commit is unknown."""

_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class TundravmSource:
    """The git revision of tundravm a scaffolded project depends on."""

    url: str
    rev: str
    pinned: bool
    """``rev`` is the commit of the running tundravm; ``False``: a branch that moves."""

    @property
    def requirement(self) -> str:
        """The PEP 508 dependency: ``tundravm @ git+URL@REV``."""
        return f"tundravm @ git+{self.url}@{self.rev}"

    @property
    def install(self) -> str:
        """The ``uv add`` command that writes :attr:`requirement` into a project."""
        return f'uv add "{self.requirement}"'

    @property
    def note(self) -> str | None:
        """Why the project follows a branch, or ``None`` when it is pinned."""
        if self.pinned:
            return None
        return (
            f"could not tell which tundravm commit is running, so the project follows "
            f'{self.rev}; pin one with `uv add "tundravm @ git+{self.url}@COMMIT"`'
        )


def tundravm_source() -> TundravmSource:
    """The revision of the running tundravm a scaffold can pin, from its install metadata.

    A ``git+`` install (``uv add``/``uvx``/``pip`` from the repository) records its
    commit in ``direct_url.json``; an editable install of a git checkout pins the
    checkout's ``HEAD`` when a remote-tracking branch contains it (so a fresh machine
    can fetch it). Anything else follows :data:`TUNDRAVM_FALLBACK_REV`.
    """
    try:
        raw = metadata.distribution("tundravm").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        raw = None
    return source_from_direct_url(raw)


def source_from_direct_url(raw: str | None) -> TundravmSource:
    """The :class:`TundravmSource` a PEP 610 ``direct_url.json`` text names."""
    fallback = TundravmSource(TUNDRAVM_REPOSITORY, TUNDRAVM_FALLBACK_REV, pinned=False)
    try:
        record = json.loads(raw) if raw else {}
    except ValueError:
        return fallback
    if not isinstance(record, dict) or not isinstance(record.get("url"), str):
        return fallback
    url: str = record["url"]
    vcs = record.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id")
        remote = _https_url(url)
        if isinstance(commit, str) and _COMMIT.fullmatch(commit) and remote is not None:
            return TundravmSource(remote, commit, pinned=True)
        return fallback
    if isinstance(record.get("dir_info"), dict) and url.startswith("file://"):
        published = _published_head(Path(unquote(urlparse(url).path)))
        if published is not None:
            return TundravmSource(*published, pinned=True)
    return fallback


def _https_url(url: str) -> str | None:
    """*url* as an ``https://`` clone URL without ``.git``; ``None`` for other schemes."""
    if scp := re.fullmatch(r"[\w.-]+@([\w.-]+):(.+)", url):  # git@github.com:owner/repo
        url = f"https://{scp.group(1)}/{scp.group(2)}"
    elif url.startswith("ssh://"):
        parsed = urlparse(url)
        url = f"https://{parsed.hostname}{parsed.path}"
    if not url.startswith("https://"):
        return None
    return url.removesuffix("/").removesuffix(".git")


def _git(checkout: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _published_head(checkout: Path) -> tuple[str, str] | None:
    """``(url, commit)`` of *checkout*'s ``HEAD`` when a remote-tracking branch has it."""
    commit = _git(checkout, "rev-parse", "HEAD")
    if commit is None or not _COMMIT.fullmatch(commit):
        return None
    branches = _git(
        checkout, "for-each-ref", "--contains", commit, "--format=%(refname)", "refs/remotes/"
    )
    for ref in (branches or "").splitlines():
        remote = ref.removeprefix("refs/remotes/").split("/", 1)[0]
        url = _git(checkout, "remote", "get-url", remote)
        https = None if url is None else _https_url(url)
        if https is not None:
            return https, commit
    return None


PYPROJECT_TEMPLATE = r"""[project]
name = "$name"
version = "0.1.0"
requires-python = ">=3.12"
$comment
dependencies = ["$requirement"]

[dependency-groups]
dev = ["pytest"]

[tool.pytest.ini_options]
pythonpath = ["."]

$table"""

README_TEMPLATE = r"""# $title: a tundravm image (`$filename`, $template template)

$commands
"""

GITIGNORE_MARKER = "/build/*"

GITIGNORE_BLOCK = f"""\
# tundravm build directories; the lockfile stays committed for `tundravm lock --check`
/.tundravm/
{GITIGNORE_MARKER}
!/build/tundravm.lock
"""

WORKFLOW_PATH = ".github/workflows/tundravm.yml"

WORKFLOW_TEMPLATE = """\
# Generated by `tundravm new --ci`. The check job fails when the image has errors, the
# committed lockfile is stale or the committed mkosi/ tree differs from what the image
# compiles to; findings show inline on the pull request.
#
# pyproject.toml pins tundravm to a git commit (it is not on PyPI). Commit uv.lock
# (`uv lock`) and both jobs install exactly what it locks.
#
# The build job is opt-in: it runs after check only when the repository variable
# TUNDRAVM_BACKEND (Settings > Secrets and variables > Actions > Variables) names the
# backend: local (the runner has no Lima). It builds the production profile twice in independent
# workspaces (`build --reproduce`), verifies the build directory against its manifest
# and uploads it.
name: tundravm

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-24.04
    steps:
      - name: Checkout
        uses: actions/checkout@v7
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@v10.3.3

      - name: Sync dependencies
        run: if [ -f uv.lock ]; then uv sync --frozen; else uv sync; fi

      - name: Summarize the image
        run: |
          {{ echo '```text'; uv run tundravm show {recipe}; echo '```'; }} >> "$GITHUB_STEP_SUMMARY"

      - name: Check the image, the lockfile and the committed tree
        run: |
          uv run tundravm check {recipe} --tree {tree}
          uv run tundravm lock {recipe} --check

  build:
    needs: check
    if: vars.TUNDRAVM_BACKEND != ''
    runs-on: ubuntu-24.04
    env:
      TUNDRAVM_BACKEND: ${{{{ vars.TUNDRAVM_BACKEND }}}}
    steps:
      - name: Checkout
        uses: actions/checkout@v7
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@v10.3.3

      - name: Prepare the build host
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq debian-archive-keyring
          sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

      - name: Sync dependencies
        run: if [ -f uv.lock ]; then uv sync --frozen; else uv sync; fi

      - name: Build twice and compare
        run: uv run tundravm build {recipe} --reproduce

      - name: Verify the build directory
        run: uv run tundravm verify latest

      - name: Upload the build directory
        if: ${{{{ !cancelled() }}}}
        uses: actions/upload-artifact@v7
        with:
          name: tundravm-build
          path: build/*/production/
"""

TEMPLATE_VARIANTS: dict[str, tuple[str, ...]] = {
    "minimal": ("production", "dev"),
    "service": ("production", "dev"),
    "cloud": ("production", "dev"),
    "prover": ("production", "dev"),
}
"""The profiles each template declares."""

EXPECTED_FINDINGS: dict[str, tuple[str, ...]] = {
    "minimal": ("source-unpinned",),
    "service": ("source-unpinned",),
    "cloud": ("source-unpinned",),
    "prover": ("source-unpinned",),
}
"""Lint codes a freshly generated image reports by design: ``tundravm lock`` clears
``source-unpinned``."""

_ALLOW_COMMENTS: dict[str, str] = {
    "minimal": "        # The evidence service builds tundra-tools: `tundravm lock` pins it.\n",
    "service": "        # The evidence service builds tundra-tools: `tundravm lock` pins it.\n",
    "cloud": "        # The EFI stub stays unpinned until `tundravm lock` pins its sha256.\n",
    "prover": (
        "        # The tundra-tools builds (evidence, volume and delivery tools) and the EFI\n"
        "        # stub package stay unpinned until `tundravm lock` pins them.\n"
    ),
}
"""The comment above ``allow=`` in the test module of a template with :data:`EXPECTED_FINDINGS`."""


def render_recipe_template(
    *,
    title: str,
    filename: str,
    release: str,
    template: str = DEFAULT_TEMPLATE,
    backend: str | None = None,
) -> str:
    """Return the starter image source ``tundravm new --template TEMPLATE`` writes.

    It names no build backend: the loader picks one for the host (``TUNDRAVM_BACKEND``
    or the project file's ``backend`` override it). *backend* (a key of
    :data:`EXPLICIT_BACKENDS`) appends an explicit ``backend = ...`` binding instead, for a
    file that must build with that one wherever it runs; ``new`` never passes it.
    """
    constants = _CLOUD_CONSTANTS if template in ("cloud", "prover") else ""
    source = Template(_SOURCES[template]).substitute(
        title=title,
        template=template,
        filename=filename,
        release=release,
        constants=constants,
    )
    if backend is None:
        return source
    if backend not in EXPLICIT_BACKENDS:
        raise ValidationError(
            f"{backend!r} is not a build backend.",
            hint=f"Use one of: {', '.join(EXPLICIT_BACKENDS)}.",
        )
    name, instance = EXPLICIT_BACKENDS[backend]
    return f"{source}\nfrom tundravm.backends import {name}  # noqa: E402\n\nbackend = {instance}\n"


def render_test_module(*, filename: str, template: str = DEFAULT_TEMPLATE) -> str:
    """Return ``tests/test_<name>.py``: lint, compile and golden-tree tests for the image.

    It imports only ``tundravm`` and ``tundravm.testing``.
    """
    expected = EXPECTED_FINDINGS[template]
    subject = "plan(IMAGE, profile)"
    if expected:
        codes = ", ".join(f'"{code}"' for code in expected)
        lint_call = f"{_ALLOW_COMMENTS[template]}        assert_clean({subject}, allow=({codes},))"
    else:
        lint_call = f"        assert_clean({subject})"
    names = [f'"{name}"' for name in TEMPLATE_VARIANTS[template]]
    profiles = ", ".join(names) + ("," if len(names) == 1 else "")
    return Template(TEST_TEMPLATE).substitute(
        filename=filename, lint_call=lint_call, profiles=profiles
    )


def render_pyproject(
    *,
    name: str,
    recipe: str = "image.py",
    backend: str | None = None,
    source: TundravmSource | None = None,
) -> str:
    """Return the ``pyproject.toml`` ``new`` writes.

    Its dependency is tundravm from git at *source* (default :func:`tundravm_source`),
    and it ends with the ``[tool.tundravm]`` table naming the image *recipe* and, when given,
    *backend* (``None``: this host's, chosen when a command runs).
    """
    source = tundravm_source() if source is None else source
    if source.pinned:
        why = "# tundravm is not on PyPI: this pins the commit `tundravm new` ran from."
    else:
        why = (
            "# tundravm is not on PyPI, and `tundravm new` could not tell which commit\n"
            f"# it ran from, so this follows {source.rev}."
        )
    comment = (
        f'{why}\n# Move it with `uv add "tundravm @ git+{source.url}@COMMIT"`; commit uv.lock.'
    )
    table = render_table(image=recipe, backend=backend)
    return Template(PYPROJECT_TEMPLATE).substitute(
        name=name, comment=comment, requirement=source.requirement, table=table
    )


FIRST_STEPS: tuple[tuple[str, str], ...] = (
    ("uv run tundravm lock", "pin every source (tundra-tools needs git read access)"),
    ("uv run tundravm build --tree-only mkosi", "the tree the tests and CI compare; commit it"),
    ("uv run pytest", "lint, compile and committed-tree tests"),
    ("uv run tundravm build --dev", "build the dev profile"),
    ("uv run tundravm run --mode simulated --dev", "boot it without TDX, its services up"),
)
"""The loop after ``uv sync`` in a new project, as ``new`` prints it and its README lists."""


def render_steps(steps: Sequence[tuple[str, str]], indent: str) -> str:
    """*steps* as aligned ``command  why`` lines."""
    width = max(len(command) for command, _ in steps)
    return "\n".join(f"{indent}{command:<{width}}  {why}" for command, why in steps)


def render_readme(*, title: str, filename: str, template: str, tests: bool) -> str:
    """Return the ``README.md`` ``new`` writes: the commands of the operator's loop."""
    steps = FIRST_STEPS if tests else tuple(s for s in FIRST_STEPS if "pytest" not in s[0])
    commands = [
        ("uv run tundravm show", "what the image contains, profile by profile"),
        ("uv run tundravm check", "declarations, lint, lock and tree drift"),
        *steps,
        ("uv run tundravm build", "build production into build/<image>/production/<id>/"),
    ]
    lines = render_steps(commands, "    ")
    return Template(README_TEMPLATE).substitute(
        title=title, filename=filename, template=template, commands=lines
    )


def render_workflow(*, recipe: str, tree: str = "mkosi") -> str:
    """Return the GitHub Actions workflow ``tundravm new --ci`` writes."""
    return WORKFLOW_TEMPLATE.format(recipe=recipe, tree=tree)


__all__ = [
    "DEFAULT_TEMPLATE",
    "EXPLICIT_BACKENDS",
    "EDITABLE_INSTALL",
    "EXPECTED_FINDINGS",
    "FIRST_STEPS",
    "GITIGNORE_BLOCK",
    "GITIGNORE_MARKER",
    "PYPROJECT_TEMPLATE",
    "README_TEMPLATE",
    "RECIPE_TEMPLATE",
    "TEMPLATES",
    "TEMPLATE_VARIANTS",
    "TEST_TEMPLATE",
    "TUNDRAVM_FALLBACK_REV",
    "TUNDRAVM_REPOSITORY",
    "TemplateName",
    "TundravmSource",
    "WORKFLOW_PATH",
    "WORKFLOW_TEMPLATE",
    "render_pyproject",
    "render_readme",
    "render_steps",
    "render_recipe_template",
    "render_test_module",
    "render_workflow",
    "source_from_direct_url",
    "tundravm_source",
]
