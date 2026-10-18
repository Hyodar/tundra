"""File templates for ``tundravm init``: four starter recipes and the project scaffold.

Each recipe template is assembled from the sections below with
:class:`string.Template` (``$name`` placeholders; the generated Python holds no
``$``), so the rendered source is a pure function of its arguments.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from string import Template
from typing import Literal
from urllib.parse import unquote, urlparse

from .project import render_table

BACKEND_SNIPPETS: dict[str, tuple[str, str]] = {
    "lima": (
        "from tundravm.backends import LimaMkosiBackend",
        'LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")',
    ),
    "nix": ("from tundravm.backends import NixMkosiBackend", "NixMkosiBackend()"),
    "local": ("from tundravm.backends import LocalLinuxBackend", "LocalLinuxBackend()"),
    "inprocess": (
        "from tundravm.backends.inprocess import InProcessBackend",
        "InProcessBackend()",
    ),
}

TemplateName = Literal["minimal", "service", "cloud", "prover"]

TEMPLATES: dict[str, str] = {
    "minimal": "packages, one file and one verbatim unit; a single qemu variant",
    "service": "an App fragment (generated service, config, first-boot step, lint rule) "
    "plus a dev variant",
    "cloud": "service with one UKI for qemu, azure and gcp, backports and an EFI stub "
    "pinned to a Debian snapshot",
    "prover": "cloud plus a TPM-sealed key, an encrypted disk, SSH key delivery and the tdxs "
    "attestation service",
}
"""Template name -> one-line description, in ``init --list-templates`` order."""

DEFAULT_TEMPLATE: TemplateName = "service"

_HEADER = r'''"""$title image recipe ($template template).

Inspect:  tundravm inspect $filename
Lint:     tundravm lint $filename
Lock:     tundravm lock $filename
Compile:  tundravm compile $filename --tree mkosi
Build:    tundravm bake $filename --build build
"""

'''

_MINIMAL = r"""from tundravm import Debian, File, Package, Recipe, Task, Variant
$backend_import

backend = $backend_expr

recipe = Recipe(
    name="$title",
    # Debian with its kernel and systemd boot set: recipe.platform.packages lists them.
    platform=Debian("$release"),
    items=(
        Package("ca-certificates"),
        File.text("/etc/motd", "$title\n"),
        # A boot task: runs once per boot as its own system user, sandboxed.
        Task("hello", "/usr/bin/cat /etc/motd", description="Print the $title banner"),
    ),
    variants=(Variant("default", outputs=("qemu",)),),
)
"""

_APP_IMPORTS = {
    "service": (
        "from tundravm import (\n"
        "    Debian,\n"
        "    Declaration,\n"
        "    Diagnostic,\n"
        "    Package,\n"
        "    Recipe,\n"
        "    Resolved,\n"
        "    Service,\n"
        "    Variant,\n"
        "    component,\n"
        ")\n"
        "$backend_import\n"
        "from tundravm.declarative.utils import (\n"
        "    Composite,\n"
        "    ConfigFile,\n"
        "    DevTools,\n"
        "    HttpEndpoint,\n"
        "    TeeBase,\n"
        ")\n"
    ),
    "cloud": (
        "from tundravm import (\n"
        "    Debian,\n"
        "    Declaration,\n"
        "    Diagnostic,\n"
        "    Package,\n"
        "    Recipe,\n"
        "    Resolved,\n"
        "    Service,\n"
        "    Variant,\n"
        "    component,\n"
        ")\n"
        "$backend_import\n"
        "from tundravm.declarative.utils import (\n"
        "    Backports,\n"
        "    Composite,\n"
        "    ConfigFile,\n"
        "    DevTools,\n"
        "    HttpEndpoint,\n"
        "    TeeBase,\n"
        ")\n"
    ),
    "prover": (
        "from tundravm import (\n"
        "    Debian,\n"
        "    Declaration,\n"
        "    Diagnostic,\n"
        "    Disk,\n"
        "    Key,\n"
        "    Package,\n"
        "    Recipe,\n"
        "    Resolved,\n"
        "    RuntimeTools,\n"
        "    Secrets,\n"
        "    Service,\n"
        "    Variant,\n"
        "    component,\n"
        ")\n"
        "$backend_import\n"
        "from tundravm.declarative.utils import (\n"
        "    TUNDRA_TOOLS,\n"
        "    Backports,\n"
        "    Composite,\n"
        "    ConfigFile,\n"
        "    DevTools,\n"
        "    HttpEndpoint,\n"
        "    Tdxs,\n"
        "    TeeBase,\n"
        ")\n"
    ),
}

_APP = r'''
APP_VERSION = "0.1.0"
APP_PORT = 8080
$constants

@component
class App(Composite):
    """The app: its config file, the service and a lint rule.

    Ship ``/usr/bin/app`` with a ``GoBinary``, a ``Build`` or a ``Package``. Fragment name: ``app``.
    """

    version: str
    port: int

    @property
    def http(self) -> HttpEndpoint:
        """Where the app answers HTTP inside the image, for a component that probes it."""
        return HttpEndpoint("127.0.0.1", self.port, provider=self)

    def compose(self) -> Iterator[Declaration]:
        config = {"version": self.version, "listen": f"0.0.0.0:{self.port}"}
        yield ConfigFile.env("/etc/app/app.conf", config, sort_keys=False)
        # Runs as system user "app" (declared for you), sandboxed by Hardening.strict(),
        # restarted on failure; state="app" is /var/lib/app, made by systemd, owned by
        # app and its working directory. It waits for every runtime-init step (needs=).
        # ports= is where it listens: TeeBase's firewall opens it.
        yield Service(
            "app",
            "/usr/bin/app --config /etc/app/app.conf",
            description=f"app {self.version}",
            state="app",
            ports=(self.port,),
        )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", "error", message),)


backend = $backend_expr
'''

_SERVICE_RECIPE = r"""
recipe = Recipe(
    name="$title",
    # Debian with its kernel and systemd boot set: recipe.platform.packages lists them.
    platform=Debian("$release"),
    items=(
        # NTS time, DHCP, a firewall opening only declared ports, RAM-only journal, sysctl
        # hardening, DNS, no apt timers: opt out per flag.
        TeeBase(),
        Package("ca-certificates"),
        App(version=APP_VERSION, port=APP_PORT),
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("dev", inherits="default", add=DevTools(), release=False),  # never ship it
    ),
)
"""

_CLOUD_CONSTANTS = r"""
# Packages and the EFI stub come from this snapshot.debian.org snapshot, so rebuilds see the
# same archive. The stub version is the systemd-boot-efi trixie ships in it.
SNAPSHOT = "20251113T083151Z"
EFI_STUB_VERSION = "257.8-1~deb13u1"
"""

_CLOUD_RECIPE = r"""
recipe = Recipe(
    name="$title",
    # Debian with its kernel, systemd boot set and an EFI stub from the same snapshot.
    platform=Debian("$release", snapshot=SNAPSHOT, efi_stub=EFI_STUB_VERSION),
    items=(
        # NTS time, DHCP, a firewall opening only declared ports, RAM-only journal, sysctl
        # hardening, DNS, no apt timers: opt out per flag.
        TeeBase(),
        Package("ca-certificates"),
        Package("curl", role="build"),
        Backports(),
        App(version=APP_VERSION, port=APP_PORT),
    ),
    variants=(
        # One UKI, one set of measurements, three disks: the cloud glue is gated at boot.
        Variant("default", outputs=("qemu", "azure", "gcp")),
        Variant("dev", inherits="default", add=DevTools(), release=False),  # never ship it
    ),
)
"""

_PROVER_RECIPE = r"""
# Boot-time init: TPM-sealed key -> encrypted /persistent -> root's SSH key over HTTP.
key = Key("key_persistent", output="/tmp/key_persistent")
# device=None picks the largest whole /dev/sd* disk, boot disk included: name the device and
# pick format="never" or "on_initialize" for production (lint: disk-auto-format).
disk = Disk(
    "disk_persistent",
    mount="/persistent",
    device=None,
    format="on_fail",
    key=key,
    mapper="cryptroot",
)
# tundra-tools delivers root's SSH key only. Deliver an app secret to a SecretFile or
# SecretEnv with Secrets(entries=..., delivery=ExternalDelivery(...)) naming what writes it.
secrets = Secrets(store=disk)

recipe = Recipe(
    name="$title",
    # Debian with its kernel, systemd boot set and an EFI stub from the same snapshot.
    platform=Debian("$release", snapshot=SNAPSHOT, efi_stub=EFI_STUB_VERSION),
    items=(
        # NTS time, DHCP, a firewall opening only declared ports, RAM-only journal, sysctl
        # hardening, DNS, no apt timers: opt out per flag.
        TeeBase(),
        Package("ca-certificates"),
        Package("cryptsetup"),
        # Root logs in over SSH with the key Secrets delivers; socat relays /var/tdxs.sock.
        Package("openssh-server"),
        Package("socat"),
        Package("curl", role="build"),
        Backports(),
        # The key, disk and secrets steps run the tundra-tools binaries Tdxs builds from.
        key,
        disk,
        secrets,
        RuntimeTools(TUNDRA_TOOLS),
        Tdxs(source=TUNDRA_TOOLS, needs=[secrets.ready]),
        App(version=APP_VERSION, port=APP_PORT),
    ),
    variants=(
        # One UKI, one set of measurements, three disks: the cloud glue is gated at boot.
        Variant("default", outputs=("qemu", "azure", "gcp")),
        Variant("dev", inherits="default", add=DevTools(), release=False),  # never ship it
    ),
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
    + _APP
    + _PROVER_RECIPE,
}

RECIPE_TEMPLATE = _SOURCES[DEFAULT_TEMPLATE]
"""The default (``service``) template's ``$``-placeholder source."""

TEST_TEMPLATE = r'''"""Tests for $filename: lint, compile and the committed mkosi/ tree."""

import os
from pathlib import Path

import tundravm
from tundravm.testing import UPDATE_GOLDEN_ENV, assert_clean, assert_tree, compile_tree

PROJECT = Path(__file__).resolve().parent.parent
RECIPE = tundravm.load_recipe(PROJECT / "$filename")
GOLDEN = PROJECT / "mkosi"
LOCKFILE = PROJECT / "build" / "tundravm.lock"


def test_lints_clean() -> None:
$lint_call


def test_compiles_every_variant(tmp_path: Path) -> None:
    tree = compile_tree(RECIPE, path=tmp_path)
    assert set(tree.variants) == {$variants}
    assert tree.conf()


def test_matches_committed_tree() -> None:
    # mkosi/ is the committed output of `tundravm compile $filename --tree mkosi`. After an
    # intended recipe change, rewrite it with: TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests
    if not GOLDEN.is_dir() and os.environ.get(UPDATE_GOLDEN_ENV) != "1":
        raise AssertionError(f"{GOLDEN} is missing: run `tundravm compile $filename --tree mkosi`")
    lock = tundravm.read_lock(LOCKFILE) if LOCKFILE.exists() else None
    assert_tree(tundravm.compile(RECIPE, lock=lock), GOLDEN)
'''

EDITABLE_INSTALL = "uv add --editable PATH/TO/tundravm"
"""How ``init`` tells users to use a local tundravm checkout (it is not on PyPI yet)."""

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

README_TEMPLATE = r"""# $title: a tundravm VM image recipe (`$filename`, $template template)

$commands
"""

GITIGNORE_MARKER = "/build/*"

GITIGNORE_BLOCK = f"""\
# tundravm build output; the lockfile stays committed for `tundravm lock --check`
{GITIGNORE_MARKER}
!/build/tundravm.lock
"""

WORKFLOW_PATH = ".github/workflows/tundravm.yml"

WORKFLOW_MKOSI_REV = "v26"
"""The mkosi tag the generated workflow's bake job installs."""

WORKFLOW_TEMPLATE = """\
# Generated by `tundravm init`. The check job posts a dry-run summary of the
# image to the job page, then fails when the recipe has findings or the
# committed mkosi tree or lockfile is stale. Findings show inline on the pull
# request.
#
# pyproject.toml pins tundravm to a git commit (it is not on PyPI). Commit
# uv.lock (`uv lock`) and both jobs install exactly what it locks
# (`uv sync --frozen`); without it they resolve afresh on every run.
#
# The bake job is opt-in: it runs after check only when the repository variable
# TUNDRAVM_BAKE_BACKEND (Settings > Secrets and variables > Actions >
# Variables) names a backend: local or inprocess. (lima and nix need limactl
# or Nix, which ubuntu-latest does not have; install them in a step of your
# own first.) It installs mkosi {mkosi}, bakes the image twice to prove it
# reproducible, then uploads the evidence bundle, its HTML report,
# bake-result.json (the second build's too) and the built artifacts: mkosi's
# under build/<variant>/output/, the in-process backend's disk.* beside them.
# Evidence requires a matched second build and the lockfile, and real
# artifacts unless the backend is inprocess. When the bake fails after writing
# build/bake-result.json, evidence is still collected and uploaded, and the
# job fails on the bake's (or evidence's) exit status.
name: tundravm

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4

      - name: Sync dependencies
        run: if [ -f uv.lock ]; then uv sync --frozen; else uv sync; fi

      - name: Summarize the image
        run: |
          uv run tundravm inspect {recipe} --format markdown >> "$GITHUB_STEP_SUMMARY"

      - name: Check recipe, compiled tree and lockfile
        run: uv run tundravm ci {recipe} --tree {tree}

  bake:
    needs: check
    if: vars.TUNDRAVM_BAKE_BACKEND != ''
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4

      - name: Install mkosi
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq bubblewrap debian-archive-keyring python3-pefile
          pipx install git+https://github.com/systemd/mkosi.git@{mkosi}
          mkosi --version

      - name: Sync dependencies
        run: if [ -f uv.lock ]; then uv sync --frozen; else uv sync; fi

      - name: Fetch sources
        run: uv run tundravm fetch {recipe} --build build

      - name: Bake twice and compare
        run: >-
          uv run tundravm bake {recipe} --backend ${{{{ vars.TUNDRAVM_BAKE_BACKEND }}}}
          --build build --verify-reproducible

      - name: Collect evidence
        if: ${{{{ !cancelled() && hashFiles('build/bake-result.json') != '' }}}}
        run: >-
          uv run tundravm evidence {recipe} --build build
          --require ${{{{ vars.TUNDRAVM_BAKE_BACKEND == 'inprocess'
          && 'reproducible,lock' || 'reproducible,lock,real' }}}}
          --bundle evidence.tar.gz --html evidence.html

      - name: Upload evidence and artifacts
        if: ${{{{ !cancelled() && hashFiles('build/bake-result.json') != '' }}}}
        uses: actions/upload-artifact@v4
        with:
          name: tundravm-bake
          include-hidden-files: true
          path: |
            evidence.tar.gz
            evidence.html
            build/bake-result.json
            build/.reproduce/**/bake-result.json
            build/*/output/*
            build/*/*.qcow2
            build/*/*.vhd
            build/*/*.tar.gz
"""

TEMPLATE_VARIANTS: dict[str, tuple[str, ...]] = {
    "minimal": ("default",),
    "service": ("default", "dev"),
    "cloud": ("default", "dev"),
    "prover": ("default", "dev"),
}
"""The variants each template declares."""

EXPECTED_FINDINGS: dict[str, tuple[str, ...]] = {
    "minimal": (),
    "service": (),
    "cloud": ("source-unpinned",),
    "prover": ("disk-auto-format", "source-unpinned"),
}
"""Lint codes a freshly generated template reports by design: ``tundravm lock`` clears
``source-unpinned``, naming the disk's device clears ``disk-auto-format``."""

_ALLOW_COMMENTS: dict[str, str] = {
    "cloud": "    # The EfiStub package stays unpinned until `tundravm lock` pins its sha256.\n",
    "prover": (
        "    # The tundra-tools builds and the EfiStub package stay unpinned until\n"
        "    # `tundravm lock` pins them; the disk picks its device at boot until the\n"
        "    # recipe names one.\n"
    ),
}
"""The comment above ``allow=`` in the test module of a template with :data:`EXPECTED_FINDINGS`."""


def render_recipe_template(
    *,
    title: str,
    filename: str,
    release: str,
    backend: str,
    template: str = DEFAULT_TEMPLATE,
) -> str:
    """Return the starter recipe source ``tundravm init --template TEMPLATE`` writes."""
    backend_import, backend_expr = BACKEND_SNIPPETS[backend]
    constants = _CLOUD_CONSTANTS if template in ("cloud", "prover") else ""
    return Template(_SOURCES[template]).substitute(
        title=title,
        template=template,
        filename=filename,
        release=release,
        backend_import=backend_import,
        backend_expr=backend_expr,
        constants=constants,
    )


def render_test_module(*, filename: str, template: str = DEFAULT_TEMPLATE) -> str:
    """Return ``tests/test_<name>.py``: lint, compile and golden-tree tests for the recipe."""
    expected = EXPECTED_FINDINGS[template]
    if expected:
        codes = ", ".join(f'"{code}"' for code in expected)
        lint_call = f"{_ALLOW_COMMENTS[template]}    assert_clean(RECIPE, allow=({codes},))"
    else:
        lint_call = "    assert_clean(RECIPE)"
    variants = ", ".join(f'"{name}"' for name in TEMPLATE_VARIANTS[template])
    return Template(TEST_TEMPLATE).substitute(
        filename=filename, lint_call=lint_call, variants=variants
    )


def render_pyproject(
    *,
    name: str,
    recipe: str = "image.py",
    backend: str = "lima",
    source: TundravmSource | None = None,
) -> str:
    """Return the minimal ``pyproject.toml`` ``init`` writes when the project has none.

    Its dependency is tundravm from git at *source* (default :func:`tundravm_source`),
    and it ends with the ``[tool.tundravm]`` table naming *recipe* and *backend*.
    """
    source = tundravm_source() if source is None else source
    if source.pinned:
        why = "# tundravm is not on PyPI: this pins the commit `tundravm init` ran from."
    else:
        why = (
            "# tundravm is not on PyPI, and `tundravm init` could not tell which commit\n"
            f"# it ran from, so this follows {source.rev}."
        )
    comment = (
        f'{why}\n# Move it with `uv add "tundravm @ git+{source.url}@COMMIT"`; commit uv.lock.'
    )
    table = render_table(recipe=recipe, backend=backend)
    return Template(PYPROJECT_TEMPLATE).substitute(
        name=name, comment=comment, requirement=source.requirement, table=table
    )


def render_readme(*, title: str, filename: str, template: str, tests: bool) -> str:
    """Return the five-line ``README.md`` ``init`` writes when the project has none."""
    commands = [
        (f"tundravm inspect {filename}", "show what the image contains"),
        (f"tundravm compile {filename} --tree mkosi", "write the mkosi tree; commit it"),
        ("uv run pytest tests", "lint, compile and golden-tree tests")
        if tests
        else (f"tundravm lint {filename}", "report recipe problems"),
        (f"tundravm bake {filename} --build build", "build the image"),
    ]
    width = max(len(command) for command, _ in commands)
    lines = "\n".join(f"    {command:<{width}}  {why}" for command, why in commands)
    return Template(README_TEMPLATE).substitute(
        title=title, filename=filename, template=template, commands=lines
    )


def render_workflow(*, recipe: str, tree: str = "mkosi") -> str:
    """Return the GitHub Actions workflow ``tundravm init --ci github`` writes."""
    return WORKFLOW_TEMPLATE.format(recipe=recipe, tree=tree, mkosi=WORKFLOW_MKOSI_REV)


__all__ = [
    "BACKEND_SNIPPETS",
    "DEFAULT_TEMPLATE",
    "EDITABLE_INSTALL",
    "EXPECTED_FINDINGS",
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
    "WORKFLOW_MKOSI_REV",
    "WORKFLOW_PATH",
    "WORKFLOW_TEMPLATE",
    "render_pyproject",
    "render_readme",
    "render_recipe_template",
    "render_test_module",
    "render_workflow",
    "source_from_direct_url",
    "tundravm_source",
]
