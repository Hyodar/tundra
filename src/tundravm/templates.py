"""File templates for ``tundravm new``: stage 1 of the attested node and the project scaffold.

Each file is rendered with :class:`string.Template` (``$name`` placeholders; the generated
Python holds no ``$``), so the rendered source is a pure function of its arguments.
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
from urllib.parse import unquote, urlparse

from .capabilities import BUILD_HOST_PACKAGES, USERNS_KNOBS
from .errors import ValidationError
from .project import render_table

EXPLICIT_BACKENDS: dict[str, tuple[str, str]] = {
    "lima": ("LimaMkosiBackend", 'LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")'),
    "local": ("LocalLinuxBackend", "LocalLinuxBackend()"),
}
"""``render_recipe_template(backend=KIND)``: the class and the instance a file that must build
with one backend, whatever the host, binds to ``backend``."""

RECIPE_TEMPLATE = r'''"""$title: the boot, stage 1 of the guide.

Debian $release from one snapshot, with systemd, the TEE baseline (NTS time, a volatile
journal, sysctl hardening), DHCP, a firewall that drops inbound traffic nothing exposes,
filtered egress, and a dev profile with a serial console and root password "tdx".
"""

from tundravm import Debian, Image
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Network

image = Image(
    "$title",
    base=Debian("$release", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    network=Network(egress=Egress()),
    dev=DevAccess(password="tdx"),
)
'''
"""The image ``new`` writes, stage 1 of the attested node, with ``$``-placeholders."""

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
        assert_clean(plan(IMAGE, profile))


def test_compiles_every_profile() -> None:
    for profile in PROFILES:
        service = plan(IMAGE, profile).service("tdxs")
        assert service.user == "tdxs"


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
    can fetch it). A release wheel pins the commit it was built from
    (:func:`release_source`). Anything else follows :data:`TUNDRAVM_FALLBACK_REV`.
    """
    from . import _version

    try:
        raw = metadata.distribution("tundravm").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        raw = None
    source = source_from_direct_url(raw)
    if source.pinned:
        return source
    return release_source(_version.version, _version.commit) or source


def release_source(version: str, commit: str | None) -> TundravmSource | None:
    """The commit a wheel of *version* built from *commit* pins, or ``None``.

    The build hook records both in ``tundravm/_version.py``. A release (``2.0.0``) and a
    build between tags (``2.0.0.post1.dev3+gabc1234``) pin *commit*; a build of an edited
    tree (a ``.d<date>`` local part) or one that recorded no commit pins nothing, since no
    commit holds what it was built from.
    """
    if commit is None or not _COMMIT.fullmatch(commit):
        return None
    local = version.partition("+")[2]
    if any(re.fullmatch(r"d\d{8}", part) for part in local.split(".")):
        return None
    return TundravmSource(TUNDRAVM_REPOSITORY, commit, pinned=True)


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

[tool.ruff]
line-length = 100

$table"""

README_TEMPLATE = r"""# $title: a tundravm image (`$filename`)

$commands
"""

GITIGNORE_MARKER = "/build/*"

GITIGNORE_BLOCK = f"""\
# tundravm build directories; the lockfile stays committed for `tundravm lock --check`
/.tundravm/
{GITIGNORE_MARKER}
!/build/tundravm.lock
"""

WORKFLOW_PATH = ".github/workflows/image.yml"

WORKFLOW_TEMPLATE = """\
# Generated by `tundravm new --ci`: the image's pipeline.
#
# Every push and pull request: check fails fast on a stale uv.lock, lint errors, a stale
# build/tundravm.lock or a committed mkosi/ tree that differs from what the image compiles
# to; build then builds the production profile (the one a tag releases), verifies it
# against its manifest and uploads its manifest, measurements and SBOM.
#
# A v* tag also rebuilds that build on a second runner (`verify --rebuild` compares every
# artifact), attests the provenance of every released file and creates the GitHub release
# with the image, the manifest, measurements.json, the SBOM and SHA256SUMS. GitHub attests
# public repositories only (a private one needs GitHub Enterprise Cloud): a private
# repository releases without provenance and says so in a warning.
#
# Commit uv.lock (`uv sync`), build/tundravm.lock (`tundravm lock`) and mkosi/
# (`tundravm build --tree-only mkosi`). The runner installs the packages `tundravm doctor`
# names for a bare Ubuntu host. Nothing is cached between runs: a production build reads no
# cache, and each build starts from the lock alone.
#
# No job hands a token to the build. A private source (`Git(url, ref, auth="VAR")`) reads
# VAR from the host: add `VAR: ${{ secrets.NAME }}` to the env of build and rebuild, NAME a
# secret holding a token that reads that repository (github.token reads this one only).
name: image

on:
  push:
    branches: ["**"]
    tags: ["v*"]
  pull_request:

permissions:
  contents: read

concurrency:
  group: image-${{ github.ref }}
  cancel-in-progress: ${{ github.ref_type != 'tag' }}

jobs:
  check:
    # A push already runs every branch here, so a pull request from one skips its second run.
    if: >-
      github.event_name != 'pull_request' ||
      github.event.pull_request.head.repo.full_name != github.repository
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - name: Checkout
        uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@v10.3.3

      - name: Sync dependencies
        run: uv sync --locked

      - name: Summarize the image
        run: |
          { echo '```text'; uv run tundravm show; echo '```'; } >> "$GITHUB_STEP_SUMMARY"

      - name: Check the image, the lockfile and the committed tree
        run: |
          uv run tundravm check --tree mkosi
          uv run tundravm lock --check

  build:
    needs: check
    runs-on: ubuntu-24.04
    timeout-minutes: 120
    env:
      TUNDRAVM_BACKEND: local
    steps:
      - name: Checkout
        uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@v10.3.3

      - name: Prepare the build host
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq %packages
          sudo sysctl -w %sysctl

      - name: Sync dependencies
        run: uv sync --locked

      - name: Check the build host
        run: uv run tundravm doctor --for build

      - name: Build the production profile
        run: uv run tundravm build

      - name: Verify the build against its manifest
        run: |
          uv run tundravm verify latest
          { echo '```text'; uv run tundravm show latest --measurements; echo '```'; } \\
            >> "$GITHUB_STEP_SUMMARY"

      - name: Collect the records and pack the build
        run: |
          build=$(readlink -f build/*/latest-production)
          mkdir -p "$RUNNER_TEMP/records"
          cp "$build/manifest.json" "$build/measurements.json" "$build/sbom.spdx.json" \\
            "$build/observations.json" "$RUNNER_TEMP/records/"
          tar -C "$(dirname "$build")" -cf "$RUNNER_TEMP/build.tar" "$(basename "$build")"

      - name: Upload the manifest, measurements and SBOM
        uses: actions/upload-artifact@v7.0.2
        with:
          name: records
          path: ${{ runner.temp }}/records/
          if-no-files-found: error

      - name: Upload the build directory
        if: github.ref_type == 'tag'
        uses: actions/upload-artifact@v7.0.2
        with:
          name: build
          path: ${{ runner.temp }}/build.tar
          if-no-files-found: error

  rebuild:
    needs: build
    if: github.ref_type == 'tag'
    runs-on: ubuntu-24.04
    timeout-minutes: 120
    env:
      TUNDRAVM_BACKEND: local
    steps:
      - name: Checkout
        uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@v10.3.3

      - name: Prepare the build host
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq %packages
          sudo sysctl -w %sysctl

      - name: Sync dependencies
        run: uv sync --locked

      - name: Check the build host
        run: uv run tundravm doctor --for build

      - name: Download the build directory
        uses: actions/download-artifact@v8.0.2
        with:
          name: build
          path: ${{ runner.temp }}

      - name: Rebuild independently and compare every artifact
        run: |
          mkdir "$RUNNER_TEMP/release"
          tar -C "$RUNNER_TEMP/release" -xpf "$RUNNER_TEMP/build.tar"
          uv run tundravm verify --rebuild "$RUNNER_TEMP"/release/*/

  release:
    needs: [build, rebuild]
    if: github.ref_type == 'tag'
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    permissions:
      contents: write
      id-token: write
      attestations: write
    steps:
      - name: Download the build directory
        uses: actions/download-artifact@v8.0.2
        with:
          name: build
          path: ${{ runner.temp }}

      - name: Unpack it and sum the released files
        run: |
          mkdir release
          tar -C release -xpf "$RUNNER_TEMP/build.tar"
          build=$(echo release/*)
          cd "$build"
          { jq -r '.artifacts | keys[] | select(contains("/") | not)' manifest.json
            echo manifest.json
            if [ -f manifest.sig ]; then echo manifest.sig; fi; } > "$RUNNER_TEMP/files.txt"
          xargs sha256sum < "$RUNNER_TEMP/files.txt" > SHA256SUMS
          echo SHA256SUMS >> "$RUNNER_TEMP/files.txt"
          cat SHA256SUMS
          echo "BUILD_DIR=$GITHUB_WORKSPACE/$build" >> "$GITHUB_ENV"

      - name: Attest the provenance of the released files
        id: attest
        if: github.event.repository.visibility == 'public'
        uses: actions/attest-build-provenance@v4.2.2
        with:
          subject-checksums: ${{ env.BUILD_DIR }}/SHA256SUMS

      - name: Check the attestation
        if: github.event.repository.visibility == 'public'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh attestation verify "$BUILD_DIR/payload.efi" --repo "$GITHUB_REPOSITORY"

      - name: Say a private repository releases without provenance
        if: github.event.repository.visibility != 'public'
        run: >-
          echo "::warning::no provenance attestation: GitHub attests public repositories, and
          private ones on GitHub Enterprise Cloud only"

      - name: Create the release
        working-directory: ${{ env.BUILD_DIR }}
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ github.ref_name }}
          ATTESTED: ${{ steps.attest.outcome == 'success' }}
        run: |
          {
            echo "Build $(basename "$PWD") of $GITHUB_SHA, rebuilt on a second runner: every"
            echo "artifact matched. Check a download with \\`sha256sum -c SHA256SUMS\\`."
            if [ "$ATTESTED" = true ]; then
              echo "Check its provenance with"
              echo "\\`gh attestation verify payload.efi --repo $GITHUB_REPOSITORY\\`."
            fi
          } > "$RUNNER_TEMP/notes.md"
          xargs gh release create "$TAG" --repo "$GITHUB_REPOSITORY" --verify-tag \\
            --title "$TAG" --notes-file "$RUNNER_TEMP/notes.md" < "$RUNNER_TEMP/files.txt"
"""


class _WorkflowTemplate(Template):
    """``%name`` placeholders: the workflow is full of ``$`` (shell and ``${{ }}``)."""

    delimiter = "%"


def render_workflow() -> str:
    """Return the GitHub Actions workflow ``tundravm new --ci`` writes.

    Its runner installs :data:`~tundravm.capabilities.BUILD_HOST_PACKAGES` and grants user
    namespaces with the AppArmor knob of :data:`~tundravm.capabilities.USERNS_KNOBS`: the
    fixes ``tundravm doctor`` names for a bare Ubuntu 24.04 host.
    """
    sysctl = next(f"{key}={on}" for key, _, on in USERNS_KNOBS if "apparmor" in key)
    return _WorkflowTemplate(WORKFLOW_TEMPLATE).substitute(
        packages=" ".join(BUILD_HOST_PACKAGES), sysctl=sysctl
    )


PROFILES: tuple[str, ...] = ("production", "dev")
"""The profiles the image ``new`` writes declares."""


def render_recipe_template(
    *,
    title: str,
    filename: str,
    release: str,
    backend: str | None = None,
) -> str:
    """Return the image ``tundravm new`` writes: stage 1 of the attested node.

    It names no build backend: the loader picks one for the host (``TUNDRAVM_BACKEND``
    or the project file's ``backend`` override it). *backend* (a key of
    :data:`EXPLICIT_BACKENDS`) appends an explicit ``backend = ...`` binding instead, for a
    file that must build with that one wherever it runs; ``new`` never passes it.
    """
    source = Template(RECIPE_TEMPLATE).substitute(title=title, filename=filename, release=release)
    if backend is None:
        return source
    if backend not in EXPLICIT_BACKENDS:
        raise ValidationError(
            f"{backend!r} is not a build backend.",
            hint=f"Use one of: {', '.join(EXPLICIT_BACKENDS)}.",
        )
    name, instance = EXPLICIT_BACKENDS[backend]
    return f"{source}\nfrom tundravm.backends import {name}  # noqa: E402\n\nbackend = {instance}\n"


def render_test_module(*, filename: str) -> str:
    """Return ``tests/test_<name>.py``: lint, compile and golden-tree tests for the image.

    It imports only ``tundravm`` and ``tundravm.testing``.
    """
    profiles = ", ".join(f'"{name}"' for name in PROFILES)
    return Template(TEST_TEMPLATE).substitute(filename=filename, profiles=profiles)


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
    ("uv run tundravm lock", "pin every source"),
    ("uv run tundravm build --tree-only mkosi", "the tree the tests and CI compare; commit it"),
    ("uv run pytest", "lint, compile and committed-tree tests"),
    ("uv run tundravm dev", "build the dev profile, boot it without TDX, probe it, follow it"),
)
"""The loop after ``uv sync`` in a new project, as ``new`` prints it and its README lists."""


def render_steps(steps: Sequence[tuple[str, str]], indent: str) -> str:
    """*steps* as aligned ``command  why`` lines."""
    width = max(len(command) for command, _ in steps)
    return "\n".join(f"{indent}{command:<{width}}  {why}" for command, why in steps)


def render_readme(*, title: str, filename: str, tests: bool) -> str:
    """Return the ``README.md`` ``new`` writes: the commands of the operator's loop."""
    steps = FIRST_STEPS if tests else tuple(s for s in FIRST_STEPS if "pytest" not in s[0])
    commands = [
        ("uv run tundravm show", "what the image contains, profile by profile"),
        ("uv run tundravm check", "declarations, lint, lock and tree drift"),
        *steps,
        ("uv run tundravm build", f"build production into build/{title}/production/<id>/"),
    ]
    lines = render_steps(commands, "    ")
    return Template(README_TEMPLATE).substitute(title=title, filename=filename, commands=lines)


__all__ = [
    "EXPLICIT_BACKENDS",
    "EDITABLE_INSTALL",
    "FIRST_STEPS",
    "GITIGNORE_BLOCK",
    "GITIGNORE_MARKER",
    "PROFILES",
    "PYPROJECT_TEMPLATE",
    "README_TEMPLATE",
    "RECIPE_TEMPLATE",
    "TEST_TEMPLATE",
    "TUNDRAVM_FALLBACK_REV",
    "TUNDRAVM_REPOSITORY",
    "TundravmSource",
    "WORKFLOW_PATH",
    "WORKFLOW_TEMPLATE",
    "render_pyproject",
    "render_readme",
    "render_steps",
    "render_recipe_template",
    "render_test_module",
    "render_workflow",
    "release_source",
    "source_from_direct_url",
    "tundravm_source",
]
