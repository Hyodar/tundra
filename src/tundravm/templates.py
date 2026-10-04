"""File templates for ``tundravm init``: four starter recipes and the project scaffold.

Each recipe template is assembled from the sections below with
:class:`string.Template` (``$name`` placeholders; the generated Python holds no
``$``), so the rendered source is a pure function of its arguments.
"""

from __future__ import annotations

from string import Template
from typing import Literal

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
    "cloud": "service plus azure and gcp variants, backports and an EFI stub "
    "pinned to a Debian snapshot",
    "prover": "cloud plus a TPM-sealed key, an encrypted disk, secrets and the tdxs "
    "attestation service",
}
"""Template name -> one-line description, in ``init --list-templates`` order."""

DEFAULT_TEMPLATE: TemplateName = "service"

_HEADER = r'''"""$title image recipe ($template template).

Inspect:  tundravm inspect $filename
Lint:     tundravm lint $filename
Compile:  tundravm compile $filename --out mkosi
Lock:     tundravm lock $filename
Build:    tundravm bake $filename --out build
"""

'''

_MINIMAL = r'''from tundravm import File, Fragment, Package, Recipe, Unit, Variant
$backend_import

HELLO_UNIT = """\
[Unit]
Description=Print the $title banner

[Service]
Type=oneshot
ExecStart=/usr/bin/cat /etc/motd

[Install]
WantedBy=minimal.target
"""

backend = $backend_expr

recipe = Recipe(
    name="$title",
    base="$base",
    common=Fragment(
        "base",
        items=(
            # Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
            Package("linux-image-amd64"),
            Package("systemd"),
            Package("systemd-sysv"),
            Package("udev"),
            Package("kmod"),
            Package("systemd-boot-efi"),
            Package("ca-certificates"),
            File("/etc/motd", "$title\n"),
            Unit("hello.service", HELLO_UNIT, enabled=True),
        ),
    ),
    variants=(Variant("default", target="qemu"),),
)
'''

_APP_IMPORTS = {
    "service": (
        "from tundravm import (\n"
        "    Diagnostic,\n"
        "    File,\n"
        "    Fragment,\n"
        "    Init,\n"
        "    Package,\n"
        "    Recipe,\n"
        "    Resolved,\n"
        "    Service,\n"
        "    User,\n"
        "    Variant,\n"
        ")\n"
        "$backend_import\n"
        "from tundravm.declarative.utils import Composite, DevTools\n"
    ),
    "cloud": (
        "from tundravm import (\n"
        "    Diagnostic,\n"
        "    File,\n"
        "    Fragment,\n"
        "    Init,\n"
        "    Package,\n"
        "    Recipe,\n"
        "    Resolved,\n"
        "    Service,\n"
        "    User,\n"
        "    Variant,\n"
        ")\n"
        "$backend_import\n"
        "from tundravm.declarative.utils import Backports, Composite, DevTools, EfiStub\n"
    ),
    "prover": (
        "from tundravm import (\n"
        "    Diagnostic,\n"
        "    Disk,\n"
        "    File,\n"
        "    Fragment,\n"
        "    Init,\n"
        "    Key,\n"
        "    Package,\n"
        "    Recipe,\n"
        "    Resolved,\n"
        "    RuntimeTools,\n"
        "    Secret,\n"
        "    SecretFile,\n"
        "    Secrets,\n"
        "    Service,\n"
        "    User,\n"
        "    Variant,\n"
        ")\n"
        "$backend_import\n"
        "from tundravm.declarative.utils import (\n"
        "    TUNDRA_TOOLS,\n"
        "    Backports,\n"
        "    Composite,\n"
        "    DevTools,\n"
        "    EfiStub,\n"
        "    Tdxs,\n"
        ")\n"
    ),
}

_APP = r'''
APP_VERSION = "0.1.0"
APP_PORT = 8080
$constants

@dataclass(frozen=True, slots=True, kw_only=True)
class App(Composite):
    """The app: its account, config file, a first-boot step, the service and a lint rule.

    Ship ``/usr/bin/app`` with a ``Build(...)`` or a ``Package(...)``. Fragment name: ``app``.
    """

    version: str
    port: int

    def compose(self) -> Fragment:
        config = f"version={self.version}\nlisten=0.0.0.0:{self.port}\n"
        return Fragment(
            "app",
            items=(
                User("app", home="/var/lib/app"),
                File("/etc/app/app.conf", config),
                # Runs in runtime-init before the service starts (after_init=True).
                Init("app-state", "mkdir -p /var/lib/app && chown app:app /var/lib/app"),
                Service(
                    "app",
                    "/usr/bin/app --config /etc/app/app.conf",
                    description=f"app {self.version}",
                    user="app",
                    working_dir="/var/lib/app",
                    restart="on-failure",
                    wanted_by="minimal.target",
                ),
            ),
            checks=(self.check_port,),
        )

    def check_port(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", message, variant=resolved.variant),)


backend = $backend_expr
'''

_SERVICE_RECIPE = r"""
recipe = Recipe(
    name="$title",
    base="$base",
    common=Fragment(
        "base",
        items=(
            # Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
            Package("linux-image-amd64"),
            Package("systemd"),
            Package("systemd-sysv"),
            Package("udev"),
            Package("kmod"),
            Package("systemd-boot-efi"),
            Package("ca-certificates"),
            App(version=APP_VERSION, port=APP_PORT),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", parent="default", add=DevTools()),  # never ship it
    ),
)
"""

_CLOUD_CONSTANTS = r"""
# Packages and the EFI stub come from this Debian snapshot, so rebuilds see the same archive.
SNAPSHOT = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
EFI_STUB_VERSION = "255.4-1"
"""

_CLOUD_RECIPE = r"""
recipe = Recipe(
    name="$title",
    base="$base",
    mirror=SNAPSHOT,
    tools_mirror=SNAPSHOT,
    common=Fragment(
        "base",
        items=(
            # Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
            Package("linux-image-amd64"),
            Package("systemd"),
            Package("systemd-sysv"),
            Package("udev"),
            Package("kmod"),
            Package("systemd-boot-efi"),
            Package("ca-certificates"),
            Package("curl", role="build"),
            Backports(),
            EfiStub(snapshot=SNAPSHOT, version=EFI_STUB_VERSION),
            App(version=APP_VERSION, port=APP_PORT),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
        Variant("gcp", parent="default", target="gcp"),
        Variant("dev", parent="default", add=DevTools()),  # never ship it
    ),
)
"""

_PROVER_RECIPE = r"""
# Boot-time init: TPM-sealed key -> encrypted /persistent -> secrets over HTTP.
key = Key("key_persistent", output="/tmp/key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key, mapper="cryptroot")
secrets = Secrets(
    store=disk,
    entries=(Secret("app_token", (SecretFile("/etc/app/token", owner="app"),)),),
)

recipe = Recipe(
    name="$title",
    base="$base",
    mirror=SNAPSHOT,
    tools_mirror=SNAPSHOT,
    common=Fragment(
        "base",
        items=(
            # Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
            Package("linux-image-amd64"),
            Package("systemd"),
            Package("systemd-sysv"),
            Package("udev"),
            Package("kmod"),
            Package("systemd-boot-efi"),
            Package("ca-certificates"),
            Package("cryptsetup"),
            Package("curl", role="build"),
            Backports(),
            EfiStub(snapshot=SNAPSHOT, version=EFI_STUB_VERSION),
            # The key, disk and secrets steps run the tundra-tools binaries Tdxs builds from.
            key,
            disk,
            secrets,
            RuntimeTools(TUNDRA_TOOLS),
            Tdxs(source=TUNDRA_TOOLS, after_init=True),
            App(version=APP_VERSION, port=APP_PORT),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
        Variant("gcp", parent="default", target="gcp"),
        Variant("dev", parent="default", add=DevTools()),  # never ship it
    ),
)
"""

_SOURCES: dict[str, str] = {
    "minimal": _HEADER + _MINIMAL,
    "service": _HEADER
    + "from dataclasses import dataclass\n\n"
    + _APP_IMPORTS["service"]
    + _APP
    + _SERVICE_RECIPE,
    "cloud": _HEADER
    + "from dataclasses import dataclass\n\n"
    + _APP_IMPORTS["cloud"]
    + _APP
    + _CLOUD_RECIPE,
    "prover": _HEADER
    + "from dataclasses import dataclass\n\n"
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
    # mkosi/ is the committed output of `tundravm compile $filename --out mkosi`. After an
    # intended recipe change, rewrite it with: TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests
    if not GOLDEN.is_dir() and os.environ.get(UPDATE_GOLDEN_ENV) != "1":
        raise AssertionError(f"{GOLDEN} is missing: run `tundravm compile $filename --out mkosi`")
    lock = tundravm.read_lock(LOCKFILE) if LOCKFILE.exists() else None
    assert_tree(tundravm.compile(RECIPE, lock=lock), GOLDEN)
'''

PYPROJECT_TEMPLATE = r"""[project]
name = "$name"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["tundravm"]

[dependency-groups]
dev = ["pytest"]
"""

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

WORKFLOW_TEMPLATE = """\
# Generated by `tundravm init`. Posts a dry-run summary of the image to the job
# page, then fails when the recipe has findings or the committed mkosi tree or
# lockfile is stale. Findings show inline on the pull request.
name: tundravm

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  recipe:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4

      - name: Sync dependencies
        run: uv sync

      - name: Summarize the image
        run: |
          uv run tundravm inspect {recipe} --format markdown >> "$GITHUB_STEP_SUMMARY"

      - name: Check recipe, compiled tree and lockfile
        run: uv run tundravm ci {recipe} --out {out}
"""

TEMPLATE_VARIANTS: dict[str, tuple[str, ...]] = {
    "minimal": ("default",),
    "service": ("default", "dev"),
    "cloud": ("default", "azure", "gcp", "dev"),
    "prover": ("default", "azure", "gcp", "dev"),
}
"""The variants each template declares."""

EXPECTED_FINDINGS: dict[str, tuple[str, ...]] = {
    "minimal": (),
    "service": (),
    "cloud": (),
    "prover": ("source-unpinned",),
}
"""Lint codes a freshly generated template reports by design (cleared by ``tundravm lock``)."""


def render_recipe_template(
    *,
    title: str,
    filename: str,
    base: str,
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
        base=base,
        backend_import=backend_import,
        backend_expr=backend_expr,
        constants=constants,
    )


def render_test_module(*, filename: str, template: str = DEFAULT_TEMPLATE) -> str:
    """Return ``tests/test_<name>.py``: lint, compile and golden-tree tests for the recipe."""
    expected = EXPECTED_FINDINGS[template]
    if expected:
        codes = ", ".join(f'"{code}"' for code in expected)
        lint_call = (
            "    # The tundra-tools builds track a branch until `tundravm lock` pins them.\n"
            f"    assert_clean(RECIPE, strict=True, allow=({codes},))"
        )
    else:
        lint_call = "    assert_clean(RECIPE, strict=True)"
    variants = ", ".join(f'"{name}"' for name in TEMPLATE_VARIANTS[template])
    return Template(TEST_TEMPLATE).substitute(
        filename=filename, lint_call=lint_call, variants=variants
    )


def render_pyproject(*, name: str) -> str:
    """Return the minimal ``pyproject.toml`` ``init`` writes when the project has none."""
    return Template(PYPROJECT_TEMPLATE).substitute(name=name)


def render_readme(*, title: str, filename: str, template: str, tests: bool) -> str:
    """Return the five-line ``README.md`` ``init`` writes when the project has none."""
    commands = [
        (f"tundravm inspect {filename}", "show what the image contains"),
        (f"tundravm compile {filename} --out mkosi", "write the mkosi tree; commit it"),
        ("uv run pytest tests", "lint, compile and golden-tree tests")
        if tests
        else (f"tundravm lint {filename}", "report recipe problems"),
        (f"tundravm bake {filename} --out build", "build the image"),
    ]
    width = max(len(command) for command, _ in commands)
    lines = "\n".join(f"    {command:<{width}}  {why}" for command, why in commands)
    return Template(README_TEMPLATE).substitute(
        title=title, filename=filename, template=template, commands=lines
    )


def render_workflow(*, recipe: str, out: str = "mkosi") -> str:
    """Return the GitHub Actions workflow ``tundravm init --ci github`` writes."""
    return WORKFLOW_TEMPLATE.format(recipe=recipe, out=out)


__all__ = [
    "BACKEND_SNIPPETS",
    "DEFAULT_TEMPLATE",
    "EXPECTED_FINDINGS",
    "GITIGNORE_BLOCK",
    "GITIGNORE_MARKER",
    "PYPROJECT_TEMPLATE",
    "README_TEMPLATE",
    "RECIPE_TEMPLATE",
    "TEMPLATES",
    "TEMPLATE_VARIANTS",
    "TEST_TEMPLATE",
    "TemplateName",
    "WORKFLOW_PATH",
    "WORKFLOW_TEMPLATE",
    "render_pyproject",
    "render_readme",
    "render_recipe_template",
    "render_test_module",
    "render_workflow",
]
