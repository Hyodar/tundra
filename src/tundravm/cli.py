"""Command-line interface for recipe files: ``tundravm <command> RECIPE``.

Every recipe command loads the ``Recipe`` a Python file binds (see
:func:`tundravm.recipe.load_file`) and runs one lifecycle step from
:mod:`tundravm.declarative.lifecycle` on the selected variants: repeat
``--variant NAME``, or omit it for every declared variant. ``measure`` and
``deploy`` read the ``bake-result.json`` manifest a ``bake`` wrote. ``init``
bootstraps a recipe project and ``ci`` runs lint, compile and lock checks in
one go; ``status`` reports where the project stands and ``clean`` removes build
output; ``completion`` prints a shell completion script. With no arguments the
help and a quickstart are printed. Exit codes: 0 success, 2 SDK error (``E_*``
codes) or a usage error (unknown verbs and flags get a "did you mean"), 1 for a
failed check (``lint``, ``compile --check``, ``lock --check``, ``diff``, ``ci``,
``doctor``, an untrusted ``attest``, a failing ``evidence``) or an unexpected
failure. A recipe file that fails to import (syntax error, missing module, an
exception while it runs) is an ``E_VALIDATION`` error naming ``file:line``;
``--traceback`` raises SDK errors with the Python traceback.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import re
import sys
import warnings
from collections.abc import Callable, Sequence
from dataclasses import MISSING, dataclass, fields, replace
from pathlib import Path
from typing import TextIO, cast, get_args

from . import __version__
from ._source import SOURCES_DIRNAME
from .attestation import attest, render_attestation
from .backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend, Requirement
from .backends.base import BuildBackend
from .backends.local_linux import cloud_tools
from .check import Diagnostic as CheckDiagnostic
from .check import failing, render_as, render_summary
from .clean import PARTS, clean_paths, owner_hint, remove, sudo_runner
from .completion import SHELLS, Shell, render_completion
from .declarative._compile import emit
from .declarative._lowered import Lowered
from .declarative.bom import SBOM_FORMATS, sbom
from .declarative.lifecycle import (
    DEPLOY_TARGETS,
    NO_LOCK,
    Artifact,
    Backend,
    BackendKind,
    Deployment,
    DeployTarget,
    Lock,
    Measurements,
    NoLock,
    ProbeRunner,
    Qemu,
    Scheme,
    bake_image,
    check_report,
    deploy,
    fetch_image,
    lock_image,
    lock_status,
    measure,
    probe,
    read_artifacts,
    read_lock,
    read_variant_tree,
    requirements_of,
    run_probe,
    select_artifact,
    using_lock,
    write_lock,
)
from .declarative.model import Target
from .declarative.resolve import resolve
from .diff import _wants_color, cmd_diff, diff_against
from .errors import LockfileError, TdxError, ValidationError
from .evidence import EVIDENCE_FORMATS, evidence
from .explain import (
    describe,
    diff_variants,
    explain_why,
    render,
    render_markdown,
    render_variant_diff,
    render_variant_diff_markdown,
    render_why,
    render_why_markdown,
)
from .formats import annotation_path, format_help, resolve_format, workflow_command
from .lockfile import LockDrift
from .measure import PlaceholderMeasurementWarning
from .measure import rtmr as rtmr_measure
from .measure.policy import policy_payload, write_policy
from .observability import Event, JsonReporter, TextReporter, render_bake_summary
from .project import (
    DEFAULTS,
    PATH_KEYS,
    RESOLUTION_HELP,
    TABLE,
    ConfigKey,
    ProjectConfig,
    find_project,
    render_table,
)
from .recipe import RecipeFile, load_file
from .status import Invocation, project_status, render_status
from .templates import (
    BACKEND_SNIPPETS,
    DEFAULT_TEMPLATE,
    EDITABLE_INSTALL,
    GITIGNORE_BLOCK,
    GITIGNORE_MARKER,
    TEMPLATES,
    WORKFLOW_PATH,
    render_pyproject,
    render_readme,
    render_recipe_template,
    render_test_module,
    render_workflow,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SDK_ERROR = 2

MEASUREMENT_SCHEMES: tuple[Scheme, ...] = ("rtmr", "azure", "gcp")
BACKEND_KINDS: tuple[BackendKind, ...] = get_args(BackendKind)

QUICKSTART = """\
quickstart:
  tundravm init . --name node                 scaffold node.py, its tests and pyproject.toml
  tundravm inspect node.py                    show what the image will contain
  tundravm lint node.py                       report every recipe diagnostic
  tundravm bake node.py --backend inprocess   simulated build, no VM or root needed"""


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    runner: ProbeRunner | None = None,
) -> int:
    """Run the CLI and return an exit code (never raises for SDK errors).

    *runner* replaces the host-tool probe ``doctor`` and ``init`` run (tests).
    Usage errors exit 2 through argparse's ``SystemExit``.
    """
    out = stdout if stdout is not None else sys.stdout
    parser = build_parser()
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens:
        print(parser.format_help(), file=out)
        print(QUICKSTART, file=out)
        return EXIT_OK
    args = _parse(parser, tokens)
    args.runner = runner
    handler: Callable[[argparse.Namespace, TextIO], int] = args.handler
    try:
        _apply_project(parser, args)
        return handler(args, out)
    except TdxError as exc:
        if getattr(args, "traceback", False):
            raise
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return EXIT_SDK_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _parse(parser: argparse.ArgumentParser, tokens: list[str]) -> argparse.Namespace:
    """``parse_args`` with "did you mean" for an unknown verb or flag."""
    verbs = _subcommands(parser)
    for token in tokens:
        if token == "--" or token in verbs:
            break
        if token.startswith("-"):
            if not any(
                flag.startswith(token.split("=")[0]) for flag in parser._option_string_actions
            ):
                parser.error(f"unrecognized arguments: {token}{_did_you_mean([token], parser)}")
            continue
        choices = ", ".join(verbs)
        match = difflib.get_close_matches(token, list(verbs), n=1)
        hint = f"did you mean {match[0]!r}?" if match else f"choose from {choices}"
        parser.error(f"unknown command {token!r} ({hint})")
    args, extras = parser.parse_known_args(tokens)
    if extras:
        command = verbs[args.command]
        command.error(f"unrecognized arguments: {' '.join(extras)}{_did_you_mean(extras, command)}")
    return args


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _did_you_mean(extras: Sequence[str], parser: argparse.ArgumentParser) -> str:
    """`` (did you mean --format?)`` for the unknown flags in *extras* with a close match."""
    flags = list(parser._option_string_actions)
    found = [
        match[0]
        for token in extras
        if token.startswith("-")
        and (match := difflib.get_close_matches(token.split("=")[0], flags, n=1))
    ]
    return f" (did you mean {', '.join(dict.fromkeys(found))}?)" if found else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tundravm",
        description="Inspect, lint, compile, lock, bake, measure, and deploy TDX VM image recipes.",
        epilog=(
            "RECIPE is a Python file that binds a tundravm.Recipe to `recipe` or defines a "
            "zero-argument `build() -> Recipe` function; --attr picks another name. A "
            "module-level `backend` is the build backend `bake` uses unless --backend is given. "
            "`tundravm completion bash|zsh|fish` prints a shell completion script. "
            + RESOLUTION_HELP
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    _add_init(sub)

    inspect = _add_command(
        sub, "inspect", _cmd_inspect, help="Show what the recipe will produce (dry run)."
    )
    inspect.add_argument(
        "--why",
        default=None,
        metavar="SUBJECT",
        help=(
            "Explain one emitted object of one --variant instead: an absolute image path, "
            "unit:NAME, package:NAME, hook:NAME or init:NAME. Prints the declarations that "
            "produce it with the fragment chain and overlay steps (declared, added, "
            "replaced, removed), the compiled-tree files that hold it and the lines the "
            "compiler generated for it (runtime-init ordering, enablement links, drop-ins)."
        ),
    )
    inspect_format = inspect.add_mutually_exclusive_group()
    inspect_format.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default=None,
        help=(
            "Output format (default: text). json holds the recipe digest the lockfile "
            "records and one entry per variant; markdown renders one section per variant "
            "with tables, ready for $GITHUB_STEP_SUMMARY."
        ),
    )
    inspect_format.add_argument("--json", action="store_true", help="Shorthand for --format json.")
    _add_lockfile(inspect)
    inspect.add_argument(
        "--diff-variants",
        nargs=2,
        default=None,
        metavar=("A", "B"),
        help=(
            "Print which declarations differ between variants A and B after resolution "
            "(added, removed or changed, matched by identity) instead of describing them."
        ),
    )

    lint = _add_command(
        sub,
        "lint",
        _cmd_lint,
        help="Report every recipe diagnostic: declarations, fragment checks, compiler rules.",
    )
    lint_format = lint.add_mutually_exclusive_group()
    lint_format.add_argument(
        "--format",
        choices=("auto", "text", "json", "github", "markdown"),
        default=None,
        help=(
            "Output format (default: auto). github prints `::error file=RECIPE,title=CODE::"
            "message` lines that show inline on the pull request; markdown prints a table. "
            + format_help("--json")
        ),
    )
    lint_format.add_argument("--json", action="store_true", help="Shorthand for --format json.")
    lint.add_argument("--strict", action="store_true", help="Treat warnings as errors (exit 1).")
    _add_lockfile(lint)

    compile_cmd = _add_command(
        sub, "compile", _cmd_compile, help="Emit the mkosi project tree for the recipe."
    )
    compile_cmd.add_argument(
        "--out", type=Path, default=None, help="Destination directory (default: build/mkosi)."
    )
    _add_lockfile(compile_cmd)
    compile_cmd.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the tree at --out is stale relative to the recipe.",
    )
    compile_cmd.add_argument(
        "--format",
        choices=("auto", "text", "markdown", "github"),
        default=None,
        help=(
            "Report format for --check (default: auto); text lists the changed files. "
            + format_help()
        ),
    )

    diff = _add_command(
        sub, "diff", _cmd_diff, help="Show how the recipe differs from a compiled tree."
    )
    diff.add_argument(
        "--against",
        type=Path,
        default=None,
        help="Compiled mkosi tree to compare with (default: build/mkosi).",
    )
    _add_lockfile(diff)
    diff_format = diff.add_mutually_exclusive_group()
    diff_format.add_argument(
        "--format",
        choices=("auto", "text", "stat", "markdown", "github"),
        default=None,
        help=(
            "Output format (default: auto). text is a unified diff, stat lists changed "
            "files, markdown wraps both for a PR comment, github annotates each changed "
            "file. " + format_help("--stat")
        ),
    )
    diff_format.add_argument(
        "--stat", action="store_true", help="Only list changed files (--format stat)."
    )
    diff.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colorize output (default: %(default)s).",
    )

    lock = _add_command(sub, "lock", _cmd_lock, help="Write the lockfile for the recipe.")
    lock.epilog = (
        RESOLUTION_HELP
        + " "
        + (
            "Pins already in the lockfile are kept while their source is unchanged; "
            "--update NAME resolves that source again. Drift is reported one section per "
            "line: `~` changed, `+` only in the recipe, `-` only in the lockfile, e.g. "
            "`~ variants.default.packages: +htop -jq`. Source builds are pinned under "
            "`fetches` and drift as `+ sources.<name>: source <name> is not pinned` or "
            "`~ sources.<name>: <old> -> <new>`; --check never touches the network. Locking "
            "tries every source and writes nothing unless all resolve: the error lists each "
            "failed source as `<name>: git <url> @ <ref>: <reason>` and exits 2."
        )
    )
    lock.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="Lockfile to write or check (default: build/tundravm.lock).",
    )
    lock.add_argument(
        "--update",
        action="append",
        default=None,
        metavar="SOURCE",
        help="Resolve this source build again even if it is pinned (repeatable).",
    )
    lock_mode = lock.add_mutually_exclusive_group()
    lock_mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write; print the drift and exit 1 if the lockfile is stale, "
            "or print `lock is up to date` and exit 0."
        ),
    )
    lock_mode.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Do not touch the network: reuse the existing lockfile's source pins and fail "
            "if a source build would need resolving."
        ),
    )
    lock.add_argument(
        "--format",
        choices=("auto", "text", "github", "markdown"),
        default=None,
        help=(
            "Drift report format for --check (default: auto): github prints one ::error "
            "per drifted section, markdown a table. When sources fail to resolve, github "
            "also prints one ::error per failed source. " + format_help()
        ),
    )
    lock.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Print which sections changed since the existing lockfile before writing it "
            "(with --check, print the drift without writing)."
        ),
    )

    fetch_cmd = _add_command(
        sub,
        "fetch",
        _cmd_fetch,
        help="Check out the recipe's source builds on this host, at their lockfile pins.",
    )
    fetch_cmd.epilog = (
        RESOLUTION_HELP
        + " "
        + (
            "A built kernel's source (Kernel with config=) is fetched too, as `kernel` "
            "(`kernel-<variant>` where a variant's kernel source differs). "
            "Each source lands in OUT/.sources/<name>-<pin12>-<id8>, fetched as the invoking "
            "user (git credentials and SSH agent apply); a complete checkout is verified and "
            "kept (git: HEAD is the pin and the tree is clean, submodules initialised when "
            "asked for; http: the files match the manifest written at fetch time), and one "
            "that changed fails until --force checks it out again. Sources the "
            "lockfile does not pin are resolved first, as `lock` would, unless the recipe's "
            "policy forbids unpinned sources. `bake` fetches the same way before it builds "
            "and mounts OUT/.sources into the build."
        )
    )
    fetch_cmd.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="Fetch this lockfile's pins (default: build/tundravm.lock when it exists).",
    )
    fetch_cmd.add_argument(
        "--out", type=Path, default=None, help="Build output directory (default: build)."
    )
    fetch_cmd.add_argument(
        "--force",
        action="store_true",
        help=(
            "Check every source out again, replacing its checkout (repairs one that was "
            "modified or left incomplete)."
        ),
    )

    bake = _add_command(sub, "bake", _cmd_bake, help="Compile and build the image.")
    bake.add_argument(
        "--out", type=Path, default=None, help="Build output directory (default: build)."
    )
    bake.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help=(
            "Bake frozen against this lockfile (default: the [tool.tundravm] lockfile, "
            "which must exist; else build/tundravm.lock when it exists; without one the "
            "bake is unpinned)."
        ),
    )
    bake.add_argument(
        "--backend",
        choices=BACKEND_KINDS,
        default=None,
        help="Build backend (default: the recipe file's `backend`).",
    )
    bake.add_argument(
        "--no-fetch",
        action="store_true",
        help=(
            "Do not fetch the source builds first; build from the checkouts already in "
            "OUT/.sources (e.g. copied to an air-gapped host) and fail if one is missing."
        ),
    )
    bake.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Give the build sandbox no network (mkosi --with-network=no): build only from "
            "what `tundravm fetch` put in OUT/.sources, including the Go/Cargo/.NET "
            "dependency caches, and fail before mkosi when one is missing."
        ),
    )
    bake.add_argument(
        "--verify-reproducible",
        action="store_true",
        help=(
            "Bake the same variants a second time into OUT/.reproduce (same lockfile, same "
            "fetched sources, compiled and built again) and compare every artifact's sha256; "
            "a mismatch prints a table per artifact and fails with E_REPRODUCIBILITY, keeping "
            "the second build. bake-result.json records declarative.reproducible."
        ),
    )
    bake.epilog = (
        RESOLUTION_HELP
        + " "
        + (
            "Progress goes to stderr as `[variant] step ... ok (1.2s)` lines (a live timer on a "
            "terminal); the summary table goes to stdout. Backend output is hidden unless "
            "--verbose, but its last lines are shown when the build fails. The artifact "
            "manifest is OUT/bake-result.json, which `measure` and `deploy` read."
        )
    )
    output = bake.add_mutually_exclusive_group()
    output.add_argument(
        "-v", "--verbose", action="store_true", help="Echo every backend output line."
    )
    output.add_argument(
        "-q", "--quiet", action="store_true", help="Print only the final summary and errors."
    )
    output.add_argument(
        "--json-logs",
        action="store_true",
        help="Write progress events to stdout as JSON lines (no summary).",
    )
    bake.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colorize progress output (default: %(default)s).",
    )

    measure_cmd = _add_manifest_command(
        sub, "measure", _cmd_measure, help="Derive expected TDX measurements of a baked variant."
    )
    measure_cmd.add_argument(
        "--scheme", default="rtmr", choices=MEASUREMENT_SCHEMES, help="Measurement scheme."
    )
    measure_cmd.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    measure_cmd.add_argument(
        "--export-policy",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Also write a verifier policy for Tdxs.from_policy(): JSON with schema_version, "
            "scheme, tool, tool_version, artifact {path, sha256} and registers RTMR0..RTMR2 "
            "(RTMR3 when measured). rtmr only; placeholder values need --allow-placeholder "
            "and carry a note saying they are placeholders."
        ),
    )
    measure_cmd.add_argument(
        "--allow-placeholder",
        action="store_true",
        help=(
            "Without measured-boot/dstack-mr (and always for azure/gcp), print placeholder "
            "values derived from artifact digests instead of failing. Not real measurements. "
            "Also required to measure a simulated (in-process) artifact."
        ),
    )

    deploy_cmd = _add_manifest_command(
        sub, "deploy", _cmd_deploy, help="Deploy the baked artifact of one variant."
    )
    deploy_cmd.add_argument(
        "--target", required=True, choices=tuple(DEPLOY_TARGETS), help="Deploy target."
    )
    deploy_cmd.add_argument(
        "--param",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=(
            "Target setting (repeatable). qemu: memory, cpus, ssh_port, tdx, daemonize, "
            "forward (HOST:GUEST[,HOST:GUEST...]); "
            "azure: storage_account, resource_group, location, vm_size, gallery, secure_boot "
            "(needs signed=true), signed; "
            "gcp: project, bucket, zone, machine_type."
        ),
    )
    deploy_cmd.add_argument(
        "--attach",
        action="store_true",
        help=(
            "qemu only: run QEMU in the foreground with the serial console on this terminal "
            "(Ctrl-A X quits, Ctrl-A C toggles the monitor) instead of detaching it."
        ),
    )
    deploy_cmd.add_argument(
        "--allow-simulated-artifact",
        action="store_true",
        help="Deploy a simulated (in-process) artifact anyway.",
    )

    _add_attest(sub)

    _add_sbom(sub)

    _add_evidence(sub)

    doctor_cmd = sub.add_parser(
        "doctor",
        help="Check the host tools the build backends need.",
        description=(
            "Report Python/tundravm versions and probe backend tools. With --backend, "
            "probe only that backend; with RECIPE, probe its backend and lint it. Exit 1 "
            "if a required tool is missing."
        ),
    )
    doctor_cmd.set_defaults(handler=_cmd_doctor)
    doctor_cmd.add_argument(
        "recipe", type=Path, nargs="?", default=None, help="Optional recipe .py file."
    )
    doctor_cmd.add_argument(
        "--backend", choices=BACKEND_KINDS, default=None, help="Backend to probe."
    )
    _add_import_options(doctor_cmd)

    ci = _add_command(
        sub,
        "ci",
        _cmd_ci,
        help="Run lint --strict, compile --check and lock --check; stop at the first failure.",
    )
    ci.epilog = (
        RESOLUTION_HELP
        + " "
        + (
            "Prints `ok STEP: ...` or `FAIL STEP: ...` per step (and `skip STEP` after a "
            "failure) and exits 1 on the first failure, including a missing lockfile."
        )
    )
    ci.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Committed mkosi tree to check (default: build/mkosi).",
    )
    ci.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="Lockfile to check (default: build/tundravm.lock).",
    )
    ci.add_argument(
        "--format",
        choices=("auto", "text", "github"),
        default=None,
        help="Report format for failing steps (default: auto). " + format_help(),
    )

    _add_status(sub)
    _add_clean(sub)
    _add_config(sub)

    completion = sub.add_parser(
        "completion",
        help="Print a shell completion script (bash, zsh or fish).",
        description=(
            "Print a static completion script for the verbs, flags and flag choices of "
            "this tundravm version; its header says where to install it."
        ),
        epilog=(
            "bash: source <(tundravm completion bash) in ~/.bashrc. "
            "zsh: source <(tundravm completion zsh) in ~/.zshrc after compinit. "
            "fish: tundravm completion fish > ~/.config/fish/completions/tundravm.fish."
        ),
    )
    completion.set_defaults(handler=_cmd_completion)
    completion.add_argument("shell", choices=SHELLS, help="Shell to generate the script for.")
    return parser


def _add_init(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    init = sub.add_parser(
        "init",
        help="Scaffold a recipe project: recipe, tests, pyproject.toml, README and CI.",
        description=(
            "Write <name>.py from a starter template (--template; --list-templates shows "
            "them), tests/test_<name>.py using tundravm.testing, a pyproject.toml and a "
            "README.md when the directory has none, and a .gitignore block for build/ that "
            f"keeps build/tundravm.lock committed. With --ci github, also write {WORKFLOW_PATH}, "
            "which posts `inspect --format markdown` to the job summary and runs `tundravm ci`. "
            "Then lint the new recipe and probe the chosen backend."
        ),
    )
    init.set_defaults(handler=_cmd_init)
    init.add_argument(
        "dir", type=Path, nargs="?", default=Path("."), help="Project directory (default: .)."
    )
    init.add_argument(
        "--name",
        default=None,
        help="Recipe name; writes NAME.py (default: the directory name).",
    )
    init.add_argument(
        "--template",
        choices=tuple(TEMPLATES),
        default=DEFAULT_TEMPLATE,
        help="Starter recipe (default: %(default)s); see --list-templates.",
    )
    init.add_argument(
        "--list-templates",
        action="store_true",
        help="Print the templates with a one-line description each, then exit.",
    )
    init.add_argument(
        "--base", default="debian/trixie", help="Base distribution (default: %(default)s)."
    )
    init.add_argument(
        "--backend",
        choices=sorted(BACKEND_SNIPPETS),
        default="lima",
        help="Build backend to wire in (default: %(default)s).",
    )
    init.add_argument(
        "--ci",
        choices=("github", "none"),
        default="none",
        help="CI workflow to write (default: %(default)s).",
    )
    tests = init.add_mutually_exclusive_group()
    tests.add_argument(
        "--with-tests",
        dest="tests",
        action="store_true",
        default=True,
        help="Write tests/test_<name>.py: lint, compile and golden-tree tests (default).",
    )
    tests.add_argument(
        "--no-tests", dest="tests", action="store_false", help="Do not write the tests module."
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the recipe, tests module and workflow if they exist "
        "(an existing pyproject.toml or README.md is always kept).",
    )
    init.add_argument(
        "--no-doctor",
        action="store_true",
        help="Skip probing the chosen backend's host tools (`tundravm doctor --backend`).",
    )


def _add_status(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status = _add_command(
        sub,
        "status",
        _cmd_status,
        help="Report where the project stands: lock, sources, tree, artifacts, next step.",
    )
    status.epilog = (
        RESOLUTION_HELP
        + " "
        + (
            "Read-only and network-free; always exits 0. One line per item with a verdict "
            "(ok, stale, missing, n/a; lint says error when the recipe has errors), then "
            "`next: COMMAND`, the single most useful command to run (or `everything is up "
            "to date`). Artifacts are stale when the recipe or the tree they were baked "
            "from changed since, or (with --verify) when their bytes no longer match the "
            "sha256 the bake recorded."
        )
    )
    status.add_argument(
        "--out", type=Path, default=None, help="Build output directory (default: build)."
    )
    status.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="Lockfile to report on (default: build/tundravm.lock).",
    )
    status.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Hash each artifact and compare it with the sha256 the bake recorded "
            "(integrity verified or mismatch); without it integrity is unchecked."
        ),
    )
    status.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help=(
            "Output format (default: %(default)s). json is one object per section, each "
            "with a `verdict`; markdown is a table per section."
        ),
    )


def _add_config(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config = sub.add_parser(
        "config",
        help="Print the project configuration the commands resolve and where each value came from.",
        description=(
            "Print recipe, out, tree, lockfile and backend as every command would use "
            "them here, each with its origin: flag (given on this command line), "
            f"pyproject (the [{TABLE}] table), default (built in) or, for backend, recipe "
            "(the kind of the `backend` the recipe file binds, loaded to find it). A "
            "recipe or lockfile that does not exist is marked so."
        ),
        epilog=RESOLUTION_HELP,
    )
    config.set_defaults(handler=_cmd_config)
    config.add_argument(
        "recipe", type=Path, nargs="?", default=None, help="Recipe .py file (overrides the table)."
    )
    config.add_argument("--out", type=Path, default=None, help="Build output directory.")
    config.add_argument("--tree", type=Path, default=None, help="Compiled mkosi tree.")
    config.add_argument("--lockfile", type=Path, default=None, help="Lockfile.")
    config.add_argument("--backend", choices=BACKEND_KINDS, default=None, help="Build backend.")
    config.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help=(
            "Output format (default: %(default)s); json maps each key to value and origin, "
            "plus exists for recipe and lockfile."
        ),
    )
    _add_import_options(config)


def _add_clean(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    clean = sub.add_parser(
        "clean",
        help="Remove build output: source checkouts, the tree, artifacts, mkosi state.",
        description=(
            "Remove the chosen parts of the build output directory (--out, default: the "
            "recipe's build). With no part flag, list what --all would remove and exit 0. "
            "The lockfile is removed only when --lockfile names it."
        ),
        epilog=(
            "A path this user cannot delete (root-owned mkosi output) is removed with "
            "`sudo rm -rf` when sudo is available; otherwise it is reported and clean "
            "exits 1."
        ),
    )
    clean.set_defaults(handler=_cmd_clean)
    clean.add_argument(
        "recipe",
        type=Path,
        nargs="?",
        default=None,
        help="Recipe .py file (optional with --out).",
    )
    _add_import_options(clean)
    clean.add_argument(
        "--out", type=Path, default=None, help="Build output directory (default: build)."
    )
    clean.add_argument(
        "--sources", action="store_true", help="Remove OUT/.sources (the fetched checkouts)."
    )
    clean.add_argument("--tree", action="store_true", help="Remove OUT/mkosi.")
    clean.add_argument(
        "--artifacts",
        action="store_true",
        help="Remove each OUT/<variant>/ artifact directory and OUT/bake-result.json.",
    )
    clean.add_argument(
        "--state",
        action="store_true",
        help=(
            "Remove OUT/.mkosi (incl. the cached tools tree), mkosi state in OUT/mkosi and "
            "OUT/.reproduce (the second build of bake --verify-reproducible)."
        ),
    )
    clean.add_argument("--all", action="store_true", help="All of the parts above.")
    clean.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="Also remove this lockfile (never removed otherwise).",
    )
    clean.add_argument(
        "--dry-run", action="store_true", help="Print what would be removed; remove nothing."
    )


def _add_attest(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    help_text = "Check a running image's TDX quote against a verifier policy."
    attest_cmd = sub.add_parser(
        "attest",
        help=help_text,
        description=(
            help_text + " Asks the image's tdxs issuer for a quote bound to a nonce, reads "
            "MRTD and RTMR0..RTMR3 from it and compares them with the policy `measure "
            "--export-policy` wrote. It checks measurements only; collateral verification "
            "is done by a Tdxs validator."
        ),
        epilog=(
            "Prints one line per register (match, mismatch, or unchecked when the policy "
            "does not hold it), the nonce check (the quote's report_data must be "
            "SHA-256(nonce)) and `verdict: trusted` or `verdict: untrusted`. Exit 0 when "
            "trusted, 1 when untrusted, 2 on an SDK error (unreachable issuer, not a TDX "
            "quote, a bad or placeholder policy)."
        ),
    )
    attest_cmd.set_defaults(handler=_cmd_attest)
    attest_cmd.add_argument(
        "--endpoint",
        required=True,
        metavar="URL",
        help=(
            "The tdxs issuer, which speaks JSON lines: unix:PATH or a path (the image's "
            "/var/tdxs.sock, e.g. forwarded with `ssh -L ./tdxs.sock:/var/tdxs.sock`) or "
            "tcp://HOST:PORT."
        ),
    )
    attest_cmd.add_argument(
        "--policy",
        type=Path,
        required=True,
        metavar="FILE",
        help="Verifier policy from `tundravm measure --export-policy FILE`.",
    )
    attest_cmd.add_argument(
        "--nonce",
        default=None,
        metavar="HEX",
        help="Challenge the quote must bind, as hex (default: 32 random bytes).",
    )
    attest_cmd.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help=(
            "Output format (default: %(default)s). json holds endpoint, platform, source, "
            "quote_version, nonce {value, report_data, verdict}, registers {NAME: {actual, "
            "expected, verdict}}, trusted and verdict; markdown is a table."
        ),
    )
    attest_cmd.add_argument(
        "--traceback",
        action="store_true",
        help="On an error, raise it with the full Python traceback instead of one message.",
    )


def _add_sbom(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    help_text = "Write the software bill of materials of a baked variant."
    sbom_cmd = sub.add_parser(
        "sbom",
        help=help_text,
        description=(
            help_text + " It merges mkosi's package manifest next to the artifact (every "
            "installed distribution package and version), the lockfile's source pins (each "
            "source build, kernel and efi-stub with URL, ref, commit or sha256, and the "
            "paths a build installs) and the recipe metadata (base, arch, snapshot, mirror, "
            "recipe, tree and artifact digests, tundravm version)."
        ),
        epilog=(
            "Without a manifest (a simulated bake) the document says so and lists the "
            "packages the recipe declares, unversioned. Lists are sorted; set "
            "SOURCE_DATE_EPOCH to fix the creation time."
        ),
    )
    sbom_cmd.set_defaults(handler=_cmd_sbom)
    sbom_cmd.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=None,
        help="bake-result.json written by `tundravm bake` (or the bake --out directory).",
    )
    sbom_cmd.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Bake output directory holding bake-result.json, for MANIFEST (default: build).",
    )
    sbom_cmd.add_argument(
        "--variant", default=None, metavar="NAME", help="Baked variant (default: the only one)."
    )
    sbom_cmd.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help=(
            "Lockfile with the source pins (default: the one the bake recorded, else "
            "tundravm.lock in the bake directory; without one sources are not listed)."
        ),
    )
    sbom_cmd.add_argument(
        "--format",
        choices=SBOM_FORMATS,
        default="spdx-json",
        help="Document format (default: %(default)s).",
    )
    sbom_cmd.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write the document to FILE instead of stdout.",
    )


def _add_evidence(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    help_text = "Assemble auditor-facing evidence of a bake: digests, lock, integrity, SBOM."
    cmd = _add_command(sub, "evidence", _cmd_evidence, help=help_text)
    cmd.description = (
        help_text + " For each baked variant (--variant, default: every one in "
        "OUT/bake-result.json) it records the recipe and tree digests, the lockfile "
        "(verbatim, with its version and drift), each artifact with a fresh sha256 "
        "check, the recorded reproducibility verdict, the measurements policy "
        "(OUT/<variant>/policy.json or --policy), an SPDX SBOM, the lint summary, a "
        "provenance summary and the tool versions, and writes them to OUT/evidence "
        "with an evidence.json index holding every member's sha256."
    )
    cmd.epilog = (
        RESOLUTION_HELP
        + " "
        + (
            "Prints the index; exits 1 when the verdict is fail (an artifact changed or "
            "is gone, the lockfile drifted or is missing, lint errors, or a recorded "
            "non-reproducible bake). Set SOURCE_DATE_EPOCH to fix the timestamps."
        )
    )
    cmd.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Bake output directory holding bake-result.json (default: build).",
    )
    cmd.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help=(
            "Lockfile to record and check for drift (default: the one the bake recorded, "
            "else OUT/tundravm.lock)."
        ),
    )
    cmd.add_argument(
        "--policy",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Measurements policy from `measure --export-policy` for every selected variant "
            "(default: OUT/<variant>/policy.json when it exists)."
        ),
    )
    cmd.add_argument(
        "--bundle",
        type=Path,
        default=None,
        metavar="FILE",
        help="Also write the evidence as a deterministic FILE.tar.gz.",
    )
    cmd.add_argument(
        "--html",
        type=Path,
        default=None,
        metavar="FILE",
        help="Also write a self-contained HTML report with a verdict banner.",
    )
    cmd.add_argument(
        "--format",
        choices=EVIDENCE_FORMATS,
        default="text",
        help="Index format (default: %(default)s); json is evidence.json.",
    )


def _add_import_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attr",
        default=None,
        help="Name of the Recipe (or zero-argument factory) inside RECIPE.",
    )
    parser.add_argument(
        "--pythonpath",
        action="append",
        default=None,
        metavar="DIR",
        help="Extra import directory for the recipe (repeatable). CWD is always included.",
    )
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="On an error, raise it with the full Python traceback instead of one message.",
    )


def _add_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Callable[[argparse.Namespace, TextIO], int],
    *,
    help: str,
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help, description=help, epilog=RESOLUTION_HELP)
    parser.set_defaults(handler=handler)
    parser.add_argument(
        "recipe",
        type=Path,
        nargs="?",
        default=None,
        help=f"Path to the recipe .py file (default: recipe in [{TABLE}] of pyproject.toml).",
    )
    _add_import_options(parser)
    parser.add_argument(
        "--variant",
        action="append",
        default=None,
        metavar="NAME",
        help="Variant to operate on (repeatable; default: every declared variant).",
    )
    return parser


def _add_manifest_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Callable[[argparse.Namespace, TextIO], int],
    *,
    help: str,
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help, description=help)
    parser.set_defaults(handler=handler)
    parser.add_argument(
        "manifest",
        type=Path,
        help="bake-result.json written by `tundravm bake` (or the bake --out directory).",
    )
    parser.add_argument(
        "--variant",
        default=None,
        metavar="NAME",
        help="Baked variant (default: the only one baked).",
    )
    return parser


def _add_lockfile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help=(
            "Apply this lockfile's source pins (default: build/tundravm.lock if present); "
            "lint also reports drift against it, as lint(recipe, lock=...) does."
        ),
    )


_PROJECT_FLAGS: dict[str, tuple[tuple[str, ConfigKey], ...]] = {
    "inspect": (("lockfile", "lockfile"),),
    "lint": (("lockfile", "lockfile"),),
    "compile": (("out", "tree"), ("lockfile", "lockfile")),
    "diff": (("against", "tree"), ("lockfile", "lockfile")),
    "lock": (("lockfile", "lockfile"),),
    "fetch": (("out", "out"), ("lockfile", "lockfile")),
    "bake": (("out", "out"), ("lockfile", "lockfile"), ("backend", "backend")),
    "ci": (("out", "tree"), ("lockfile", "lockfile")),
    "status": (("out", "out"), ("tree", "tree"), ("lockfile", "lockfile")),
    "evidence": (("out", "out"), ("lockfile", "lockfile")),
    "clean": (("out", "out"),),
    "doctor": (("backend", "backend"),),
    "config": (("out", "out"), ("tree", "tree"), ("lockfile", "lockfile"), ("backend", "backend")),
}
"""Per verb: the argument each ``[tool.tundravm]`` key defaults (``tree`` is compile's --out)."""

_RECIPE_OPTIONAL = frozenset({"doctor", "clean", "config"})

MISSING_RECIPE = (
    "the following arguments are required: recipe (no pyproject.toml with a "
    f"[{TABLE}] table in this directory or its parents; pass RECIPE, or add "
    f'[{TABLE}] recipe = "image.py" as `tundravm init` does)'
)


def _apply_project(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Fill RECIPE, --out, --lockfile and --backend from ``[tool.tundravm]`` when omitted.

    Records each value's origin (``flag``/``pyproject``) in ``args.origins``. The
    table's paths apply only when RECIPE is omitted or names the table's recipe. A
    configured lockfile stays selected when it does not exist: its readers say so
    (:func:`_missing_lock`) and never fall back to ``build/tundravm.lock``.
    """
    verb: str = args.command
    args.origins = {}
    args.project = None
    if not hasattr(args, "recipe") or verb == "completion":
        return
    project = find_project()
    args.project = project
    origins: dict[str, str] = args.origins
    configured = None if project is None else project.path_of("recipe")
    if args.recipe is not None:
        origins["recipe"] = "flag"
        if configured is None or not _same_path(args.recipe, configured):
            project = None
    elif configured is not None:
        args.recipe = configured
        origins["recipe"] = "pyproject"
    elif verb not in _RECIPE_OPTIONAL:
        _subcommands(parser)[verb].error(MISSING_RECIPE)
    for attr, key in _PROJECT_FLAGS.get(verb, ()):
        if getattr(args, attr, None) is not None:
            origins[key] = "flag"
            continue
        if project is None or project.get(key) is None:
            continue
        value: object = project.path_of(key) if key in PATH_KEYS else project.get(key)
        if key == "backend" and value not in BACKEND_KINDS:
            raise ValidationError(
                f"[{TABLE}] backend {value!r} in {project.path} is not a build backend.",
                hint=f"Use one of: {', '.join(BACKEND_KINDS)}",
            )
        setattr(args, attr, value)
        origins[key] = "pyproject"


def _same_path(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def _cmd_config(args: argparse.Namespace, out: TextIO) -> int:
    project: ProjectConfig | None = args.project
    values = _config_values(args)
    source = None if project is None else str(project.path)
    if args.format == "json":
        payload = {"pyproject": source, "values": values}
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return EXIT_OK
    print(f"pyproject: {source or f'none (no [{TABLE}] table here or above)'}", file=out)
    width = max(len(str(entry["value"] or "-")) for entry in values.values())
    for key, entry in values.items():
        shown = str(entry["value"] or "-")
        loaded = values["recipe"]["exists"] is True
        print(f"  {key:<8}  {shown:<{width}}  {_config_origin(key, entry, loaded)}", file=out)
    return EXIT_OK


def _config_values(args: argparse.Namespace) -> dict[str, dict[str, object]]:
    """Each key as the commands resolve it: value, origin and, for recipe and lockfile, existence.

    ``backend`` without ``--backend`` or the table's is the kind of the instance
    the recipe file binds (origin ``recipe``), loaded as ``bake`` loads it.
    """
    origins: dict[str, str] = args.origins
    recipe: Path | None = args.recipe
    values: dict[str, dict[str, object]] = {
        "recipe": {
            "value": None if recipe is None else str(recipe),
            "origin": origins.get("recipe", "default"),
            "exists": recipe is not None and recipe.is_file(),
        }
    }
    for key in ("out", "tree", "lockfile"):
        value = getattr(args, key)
        values[key] = (
            {"value": str(value), "origin": origins.get(key, "flag")}
            if value is not None
            else {"value": DEFAULTS[key], "origin": "default"}
        )
    values["lockfile"]["exists"] = Path(str(values["lockfile"]["value"])).is_file()
    if args.backend is not None:
        values["backend"] = {"value": args.backend, "origin": origins.get("backend", "flag")}
    elif recipe is not None and recipe.is_file():
        bound = _load(args).backend
        kind = None if bound is None else _backend_kind(bound)
        values["backend"] = {"value": kind, "origin": "default" if bound is None else "recipe"}
    else:
        values["backend"] = {"value": None, "origin": "default"}
    return values


def _backend_kind(backend: BuildBackend) -> str:
    """The ``--backend`` kind that builds *backend*'s class, else its ``name``."""
    for kind in BACKEND_KINDS:
        if type(Backend(kind).build_backend()) is type(backend):
            return kind
    return backend.name


def _config_origin(key: str, entry: dict[str, object], loaded: bool) -> str:
    """*entry*'s origin as ``config`` prints it, noting a path that does not exist.

    *loaded*: the recipe file exists, so an unset backend is one it does not bind.
    """
    origin = str(entry["origin"])
    if key == "backend" and origin == "recipe":
        return "recipe (the recipe file's `backend`)"
    if key == "backend" and entry["value"] is None:
        if loaded:
            return "default (the recipe file binds no `backend`; bake needs --backend)"
        return "default (the recipe file's `backend`)"
    if entry.get("exists") is False and entry["value"] is not None:
        origin += "; does not exist" + ("; run `tundravm lock`" if key == "lockfile" else "")
    return origin


def _load(args: argparse.Namespace) -> RecipeFile:
    extra: list[str] = [os.getcwd(), *(args.pythonpath or [])]
    return load_file(args.recipe, attribute=args.attr, extra_paths=extra)


def _variants(loaded: RecipeFile, args: argparse.Namespace) -> tuple[str, ...]:
    """The requested variants: ``--variant`` names, else every declared variant."""
    names: list[str] | None = args.variant
    if not names:
        return loaded.variants
    _require_variants(loaded, names, args.recipe)
    return tuple(dict.fromkeys(names))


def _require_variants(loaded: RecipeFile, names: Sequence[str], recipe: Path) -> None:
    unknown = [name for name in names if name not in loaded.variants]
    if unknown:
        raise ValidationError(
            f"Unknown variant(s): {', '.join(unknown)}.",
            hint=f"Declared variants: {', '.join(loaded.variants)}",
            context={"recipe": str(recipe)},
        )


def _listed(img: Lowered, names: tuple[str, ...] | None) -> tuple[str, ...]:
    return names if names is not None else (img.default_profile,)


def _cmd_inspect(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    fmt = args.format or ("json" if args.json else "text")
    if args.diff_variants is not None:
        return _inspect_diff(loaded, args, fmt, out)
    if args.why is not None:
        return _inspect_why(loaded, args, fmt, out)
    names = _variants(loaded, args)
    img = _with_lockfile(loaded.lowered(), args)
    variants = _listed(img, names)
    described = {name: _describe(loaded, img, name) for name in variants}
    if fmt == "json":
        payload = {"digest": img.select(variants).digest(), "variants": described}
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return EXIT_OK
    if fmt == "markdown":
        print(f"# tundravm: `{args.recipe.name}`\n", file=out)
        sections = [render_markdown(description) for description in described.values()]
        print("\n".join(sections).rstrip(), file=out)
        return EXIT_OK
    print("\n\n".join(render(d).rstrip() for d in described.values()), file=out)
    return EXIT_OK


def _inspect_diff(loaded: RecipeFile, args: argparse.Namespace, fmt: str, out: TextIO) -> int:
    if args.variant:
        raise ValidationError(
            "--diff-variants and --variant cannot be combined.",
            hint="--diff-variants A B already names both variants; drop --variant.",
        )
    a, b = args.diff_variants
    _require_variants(loaded, (a, b), args.recipe)
    diff = diff_variants(resolve(loaded.recipe, variant=a), resolve(loaded.recipe, variant=b))
    if fmt == "json":
        print(json.dumps(diff.to_dict(), indent=2, sort_keys=True), file=out)
    elif fmt == "markdown":
        print(render_variant_diff_markdown(diff, recipe=args.recipe.name), file=out)
    else:
        print(render_variant_diff(diff), file=out)
    return EXIT_OK


def _inspect_why(loaded: RecipeFile, args: argparse.Namespace, fmt: str, out: TextIO) -> int:
    names = _variants(loaded, args)
    if len(names) != 1:
        raise ValidationError(
            "--why explains one variant at a time.",
            hint=f"Pass --variant NAME (declared: {', '.join(loaded.variants)}).",
        )
    why = explain_why(
        loaded.recipe, names[0], args.why, lowered=_with_lockfile(loaded.lowered(), args)
    )
    if fmt == "json":
        print(json.dumps(why.to_dict(), indent=2, sort_keys=True), file=out)
    elif fmt == "markdown":
        print(render_why_markdown(why), file=out)
    else:
        print(render_why(why), file=out)
    return EXIT_OK


def _describe(loaded: RecipeFile, img: Lowered, name: str) -> dict[str, object]:
    """*name*'s dry-run description with the recipe's parent and resolved fragment names."""
    fragments = resolve(loaded.recipe, variant=name).fragments
    parent = loaded.recipe.variant(name).parent
    return describe(img, profile=name, parent=parent, fragments=fragments)


def _cmd_lint(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    lock: Lock | None = None
    selected: Lock | NoLock | None = None  # None: the image's own build/tundravm.lock
    if args.lockfile is not None:
        selected = NO_LOCK if _missing_lock(args) else read_lock(args.lockfile)
        lock = selected if isinstance(selected, Lock) else None
    diagnostics = check_report(loaded.recipe, loaded.image, variants=names, lock=selected)
    if lock is not None and not any(d.level == "error" for d in diagnostics):
        # as lint(recipe, lock=...): drift against a selected lockfile is a finding
        diagnostics.extend(
            CheckDiagnostic(
                level=d.level,
                code=d.code,
                message=d.message,
                hint=None,
                profile=d.variant or "common",
                subject=d.subject or None,
            )
            for d in lock_status(loaded.recipe, lock, variants=names)
        )
    fmt = resolve_format(args.format, alias="json" if args.json else None)
    print(render_as(diagnostics, fmt, recipe_path=args.recipe, strict=args.strict), file=out)
    return EXIT_FAILURE if failing(diagnostics, strict=args.strict) else EXIT_OK


def _with_lockfile(img: Lowered, args: argparse.Namespace) -> Lowered:
    """*img* reading the selected lockfile instead of ``build/tundravm.lock``.

    ``--lockfile`` must be readable; a configured one that does not exist is
    reported and read as no lockfile (:func:`_missing_lock`).
    """
    path: Path | None = getattr(args, "lockfile", None)
    if path is None:
        return img
    if not _missing_lock(args):
        read_lock(path)
    return replace(img, lock_file=path)


def _missing_lock(args: argparse.Namespace, then: str = "") -> bool:
    """Whether the selected lockfile is ``[tool.tundravm]``'s and does not exist; says so.

    The note goes to stderr; *then* says what the command does without it. A
    missing ``--lockfile`` is not this case: :func:`read_lock` fails on it.
    """
    path: Path | None = getattr(args, "lockfile", None)
    if path is None or args.origins.get("lockfile") != "pyproject" or path.is_file():
        return False
    print(f"note: {missing_lock_message(path)}{then}", file=sys.stderr)
    return True


def missing_lock_message(path: Path) -> str:
    """What every reader of a configured lockfile *path* that does not exist reports."""
    return f"configured lockfile {path} does not exist; run `tundravm lock` to create it"


def _cmd_diff(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    img = loaded.lowered()
    if args.against is None:
        args.against = Path(img.build_dir) / "mkosi"
    return _cmd_diff_loaded(args, out, img, _variants(loaded, args))


def _cmd_compile(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    img = loaded.lowered()
    destination = args.out if args.out is not None else Path(img.build_dir) / "mkosi"
    if args.check:
        fmt = resolve_format(args.format)
        args.against = destination
        args.format = "stat" if fmt == "text" else fmt
        args.stat = False
        args.color = "never"
        return _cmd_diff_loaded(args, out, img, _variants(loaded, args))
    names = _variants(loaded, args)
    selected = _with_lockfile(img, args).select(names)
    result, _ = emit(selected, destination)
    tree = read_variant_tree(destination, result.profiles)
    print(f"compiled {result.path}", file=out)
    print(f"  variants:      {', '.join(result.profiles)}", file=out)
    print(f"  recipe_digest: {selected.digest()}", file=out)
    print(f"  tree_digest:   {tree.digest}", file=out)
    return EXIT_OK


def _cmd_diff_loaded(
    args: argparse.Namespace, out: TextIO, img: Lowered, names: tuple[str, ...] | None
) -> int:
    return cmd_diff(args, out, _with_lockfile(img, args), profiles=names)


def _cmd_lock(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    img = loaded.lowered()
    path = _lock_path(img, args.lockfile)
    if args.check:
        drift = img.select(names).lock_status(path)
        print(render_drift(drift, resolve_format(args.format), path), file=out)
        return EXIT_OK if drift.is_clean else EXIT_FAILURE
    previous = read_lock(path) if path.exists() else None
    if args.explain:
        if previous is not None:
            print(img.select(names).lock_status(path).render(), file=out)
        else:
            print(f"no lockfile at {path}; every section is new", file=out)
    try:
        locked = lock_image(
            img, names, previous=previous, update=tuple(args.update or ()), offline=args.offline
        )
    except LockfileError as exc:
        if exc.failures and resolve_format(args.format) == "github":
            print(_failure_annotations(exc, args.recipe), file=out)
        raise
    write_lock(locked, path)
    print(f"locked {path}", file=out)
    return EXIT_OK


def _failure_annotations(exc: LockfileError, recipe: str | Path) -> str:
    """One ``::error`` per source build *exc* could not resolve, on the recipe file."""
    path = annotation_path(recipe)
    return "\n".join(
        workflow_command(
            "error", f"{name}: {error.source}: {error.reason}", file=path, title=error.code
        )
        for name, error in exc.failures.items()
    )


def _lock_path(img: Lowered, path: Path | None) -> Path:
    return path if path is not None else img.lock_path


def render_drift(drift: LockDrift, fmt: str, lock_path: Path) -> str:
    """Render a lock drift report in a resolved format: text, github or markdown."""
    if fmt == "github":
        return drift.github(lock_path)
    if fmt == "markdown":
        return drift.markdown()
    return drift.render()


def _cmd_fetch(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    img = loaded.lowered()
    destination: Path = args.out if args.out is not None else Path(img.build_dir)
    lock_path = _lock_path(img, args.lockfile)
    missing = _missing_lock(args, "; fetching the refs as they resolve now")
    present = args.lockfile is not None or lock_path.exists()
    locked = read_lock(lock_path) if present and not missing else None
    err = sys.stderr
    reporter = TextReporter(err, color=_wants_color("auto", err))
    try:
        fetched = fetch_image(
            img, names, locked=locked, out=destination, reporter=reporter, force=args.force
        )
    finally:
        reporter.close()
    if not fetched:
        print("no source builds to fetch", file=out)
        return EXIT_OK
    print(f"fetched {destination / SOURCES_DIRNAME}", file=out)
    width = max(len(source.name) for source in fetched)
    for source in fetched:
        state = "kept" if source.cached else "fetched"
        deps = f"  deps: {source.deps}" if source.deps is not None else ""
        print(f"  {source.name:<{width}}  {source.pin[:12]}  {state:<7}{deps}".rstrip(), file=out)
    if locked is None and not missing:
        print(f"note: no lockfile at {lock_path}; fetched the refs as they resolve now", file=out)
    return EXIT_OK


def _cmd_bake(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    img = loaded.lowered()
    destination: Path = args.out if args.out is not None else Path(img.build_dir)
    lock_path = _lock_path(img, args.lockfile)
    if args.origins.get("lockfile") == "pyproject" and not lock_path.is_file():
        # frozen against the configured lockfile, never another one or none
        raise LockfileError(
            f"Configured lockfile {lock_path} does not exist.",
            hint="Run `tundravm lock` to create it; bake freezes against the configured lockfile.",
            context={"path": str(lock_path)},
        )
    if args.lockfile is not None or lock_path.exists():
        locked: Lock | None = read_lock(lock_path)
    else:
        locked = None
        if not (args.quiet or args.json_logs):
            print(
                f"note: no lockfile at {lock_path}; baking unpinned "
                f"(run `tundravm lock {args.recipe}` to freeze the bake)",
                file=sys.stderr,
            )
    backend = None if args.backend is None else Backend(args.backend).build_backend()
    if (
        backend is not None
        and args.origins.get("backend") == "pyproject"
        and loaded.backend is not None
        and loaded.backend.name == backend.name
    ):
        backend = None  # the recipe file's own instance carries its settings
    err = sys.stderr
    reporter = (
        JsonReporter(out)
        if args.json_logs
        else TextReporter(
            err, verbose=args.verbose, quiet=args.quiet, color=_wants_color(args.color, err)
        )
    )
    try:
        if locked is not None and args.json_logs:
            extra = {"source": "cli", "path": str(lock_path)}
            reporter.emit(Event("log", None, f"frozen against {lock_path}", 0.0, extra))
        result, artifacts = bake_image(
            img,
            names,
            locked=locked,
            backend=backend,
            out=destination,
            reporter=reporter,
            lock_source=None if locked is None else lock_path,
            fetch=not args.no_fetch,
            offline=args.offline,
            verify_reproducible=args.verify_reproducible,
        )
    finally:
        reporter.close()
    if args.json_logs:
        return EXIT_OK
    if not args.quiet:
        print(file=out)
    print(render_bake_summary(result), file=out)
    if args.verify_reproducible:
        print(f"reproducible: yes ({len(artifacts)} artifacts match a second build)", file=out)
    if artifacts:
        first = artifacts[0]
        manifest = destination / "bake-result.json"
        print(
            f"next: tundravm deploy {manifest} --variant {first.variant} --target {first.target}",
            file=out,
        )
    return EXIT_OK


def _artifact(args: argparse.Namespace, target: Target | None) -> Artifact:
    artifacts = read_artifacts(args.manifest)
    variant: str | None = args.variant
    if variant is None:
        baked = sorted({a.variant for a in artifacts})
        if len(baked) != 1:
            raise ValidationError(
                f"The manifest holds {len(baked)} variants; pass --variant NAME.",
                hint=f"Baked variants: {', '.join(baked) or '(none)'}",
                context={"manifest": str(args.manifest)},
            )
        variant = baked[0]
    return select_artifact(artifacts, variant=variant, target=target)


def _cmd_measure(args: argparse.Namespace, out: TextIO) -> int:
    scheme = cast(Scheme, args.scheme)
    artifact = _artifact(args, None if scheme == "rtmr" else cast(Target, scheme))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderMeasurementWarning)
        measurements = measure(artifact, scheme=scheme, allow_placeholder=args.allow_placeholder)
    if measurements.tool == "placeholder":
        print(PLACEHOLDER_BANNER, file=sys.stderr)
    if args.export_policy is not None:
        payload = policy_payload(
            scheme=measurements.scheme,
            tool=measurements.tool,
            values=dict(measurements.values),
            artifact_path=str(artifact.path),
            artifact_sha256=artifact.sha256,
            allow_placeholder=args.allow_placeholder,
        )
        write_policy(payload, args.export_policy)
        print(f"wrote policy {args.export_policy}", file=sys.stderr)
    if args.json:
        payload = {
            "artifact": str(artifact.path),
            "artifact_digest": measurements.artifact_digest,
            "scheme": measurements.scheme,
            "tool": measurements.tool,
            "values": dict(measurements.values),
            "variant": artifact.variant,
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return EXIT_OK
    print(render_measurements(measurements, artifact=artifact), file=out)
    return EXIT_OK


PLACEHOLDER_BANNER = (
    "PLACEHOLDER: not real measurements. These values are derived from artifact digests; "
    "never put them in an attestation policy."
)


def render_measurements(measurements: Measurements, *, artifact: Artifact) -> str:
    """Human-readable ``Measurements``: header, ``source:`` provenance line, aligned values."""
    lines = [
        f"measurements {artifact.variant} ({measurements.scheme})",
        f"source: {measurements.tool} ({artifact.path})",
    ]
    width = max((len(key) for key, _ in measurements.values), default=0)
    lines.extend(f"  {key:<{width}}  {value}" for key, value in measurements.values)
    return "\n".join(lines)


def _parse_params(raw: Sequence[str] | None) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw or ():
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValidationError(
                f"Invalid --param {item!r}.",
                hint="Use --param KEY=VALUE.",
            )
        params[key] = value
    return params


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def parse_deploy_target(target: Target, params: dict[str, str]) -> DeployTarget:
    """``--param`` values as the target's ``Qemu``/``Azure``/``Gcp`` settings."""
    kind = DEPLOY_TARGETS[target]
    known = {item.name: item for item in fields(kind)}
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise ValidationError(
            f"Unknown {target} parameter(s): {', '.join(unknown)}.",
            hint=f"{target} accepts: {', '.join(known)}",
        )
    values: dict[str, object] = {}
    for name, raw in params.items():
        kind_name = str(known[name].type)
        if kind_name.startswith("tuple[tuple[int, int]"):
            values[name] = _parse_forward(target, name, raw)
        elif kind_name == "int":
            try:
                values[name] = int(raw)
            except ValueError:
                raise ValidationError(
                    f"{target} parameter {name} must be an integer.",
                    hint=f"Pass a whole number, e.g. --param {name}=2",
                ) from None
        elif kind_name == "bool":
            if raw.lower() not in _TRUE | _FALSE:
                raise ValidationError(
                    f"{target} parameter {name} must be true or false.",
                    hint=f"Use one of: {', '.join(sorted(_TRUE | _FALSE))}",
                )
            values[name] = raw.lower() in _TRUE
        else:
            values[name] = raw
    missing = [n for n, f in known.items() if f.default is MISSING and n not in values]
    if missing:
        raise ValidationError(
            f"Missing {target} parameter(s): {', '.join(missing)}.",
            hint="Pass " + " ".join(f"--param {n}=..." for n in missing),
        )
    return kind(**values)  # type: ignore[arg-type]


def _parse_forward(target: Target, name: str, raw: str) -> tuple[tuple[int, int], ...]:
    pairs: list[tuple[int, int]] = []
    for item in filter(None, (part.strip() for part in raw.split(","))):
        host, sep, guest = item.partition(":")
        if not (sep and host.isdigit() and guest.isdigit()):
            raise ValidationError(
                f"{target} parameter {name} must be HOST:GUEST port pairs.",
                hint=f"Pass e.g. --param {name}=8443:443,9000:9000",
            )
        pairs.append((int(host), int(guest)))
    return tuple(pairs)


def _cmd_deploy(args: argparse.Namespace, out: TextIO) -> int:
    target = cast(Target, args.target)
    using = parse_deploy_target(target, _parse_params(args.param))
    if args.attach:
        if not isinstance(using, Qemu):
            raise ValidationError(
                f"--attach applies to --target qemu, not {target}.",
                hint="Drop --attach; a cloud VM's console is read with the cloud's CLI.",
            )
        using = replace(using, daemonize=False)
    artifact = _artifact(args, target)
    result = deploy(artifact, using=using, allow_simulated=args.allow_simulated_artifact)
    print(render_deployment(result, variant=artifact.variant), file=out)
    return EXIT_OK


def render_deployment(result: Deployment, *, variant: str) -> str:
    """Human-readable ``Deployment``: header line plus aligned key/value rows."""
    rows = [("deployment", result.id)]
    if result.endpoint is not None:
        rows.append(("endpoint", result.endpoint))
    rows.extend(result.metadata)
    width = max(len(key) for key, _ in rows)
    lines = [f"deployed {variant} to {result.target}"]
    lines.extend(f"  {key:<{width}}  {value}" for key, value in rows)
    return "\n".join(lines)


def probe_requirement(requirement: Requirement, runner: ProbeRunner) -> tuple[bool, str]:
    """Run one probe; return ``(ok, report line)``."""
    return probe(requirement, runner)


def _probe_backend(backend: BuildBackend, runner: ProbeRunner, out: TextIO) -> bool:
    """Print one backend's probe lines; return False if a required tool is missing."""
    requirements = requirements_of(backend)
    lines: list[str] = []
    ready = True
    for requirement in requirements:
        ok, line = probe(requirement, runner)
        ready = ready and (ok or requirement.optional)
        lines.append(line)
    status = "available" if ready else "unavailable"
    print(f"backend {backend.name}: {status}", file=out)
    if not requirements:
        print("  no external tools required", file=out)
    for line in lines:
        print(f"  {line}", file=out)
    return ready


def default_doctor_backends() -> tuple[BuildBackend, ...]:
    """The real backends ``doctor`` probes when no recipe or --backend is given."""
    return (
        LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB"),
        NixMkosiBackend(),
        LocalLinuxBackend(),
    )


def _probe_measurement_tools(runner: ProbeRunner, out: TextIO) -> None:
    """Print the optional RTMR measurement tools; they never fail ``doctor``."""
    print("measurement tools:", file=out)
    for requirement in rtmr_measure.requirements():
        _, line = probe(requirement, runner)
        print(f"  {line}", file=out)


def _probe_cloud_tools(
    loaded: RecipeFile, backend: BuildBackend, runner: ProbeRunner, out: TextIO
) -> None:
    """Print the optional host tools the recipe's azure/gcp variants bake with locally."""
    if not isinstance(backend, LocalLinuxBackend):
        return
    targets = {t for v in loaded.recipe.variants for t in (*v.targets, v.target) if t}
    requirements = cloud_tools(targets)
    if not requirements:
        return
    print("cloud image tools:", file=out)
    for requirement in requirements:
        _, line = probe(requirement, runner)
        print(f"  {line}", file=out)


def doctor(
    loaded: RecipeFile | None,
    out: TextIO,
    *,
    runner: ProbeRunner | None = None,
    backend: BuildBackend | None = None,
) -> int:
    """Environment report. Exit 1 if the probed backend lacks a required tool, else 0.

    *backend* (``--backend``) is probed alone; else the recipe's backend; else every
    real backend. With a recipe, its lint summary follows.
    """
    run = runner if runner is not None else run_probe
    print(f"tundravm {__version__}", file=out)
    print(f"python {platform.python_version()}", file=out)
    if loaded is None and backend is None:
        for candidate in default_doctor_backends():
            _probe_backend(candidate, run, out)
        _probe_measurement_tools(run, out)
        return EXIT_OK
    chosen = backend if backend is not None else (None if loaded is None else loaded.backend)
    if chosen is None:
        print("backend: none configured", file=out)
        ready = False
    else:
        ready = _probe_backend(chosen, run, out)
        if loaded is not None:
            _probe_cloud_tools(loaded, chosen, run, out)
    _probe_measurement_tools(run, out)
    if loaded is not None:
        report = check_report(loaded.recipe, loaded.image, variants=loaded.variants)
        print(f"lint: {render_summary(report)}", file=out)
    return EXIT_OK if ready else EXIT_FAILURE


def _cmd_attest(args: argparse.Namespace, out: TextIO) -> int:
    result = attest(args.endpoint, args.policy, nonce=args.nonce)
    print(render_attestation(result, args.format), file=out)
    return EXIT_OK if result.trusted else EXIT_FAILURE


def _cmd_doctor(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args) if args.recipe is not None else None
    backend = None if args.backend is None else Backend(args.backend).build_backend()
    return doctor(loaded, out, runner=args.runner, backend=backend)


def _cmd_sbom(args: argparse.Namespace, out: TextIO) -> int:
    if args.manifest is not None and args.out is not None:
        raise ValidationError(
            "Pass MANIFEST or --out, not both.",
            hint="MANIFEST is the bake-result.json (or its directory) that --out names.",
        )
    args.manifest = args.manifest or args.out or Path("build")
    artifact = _artifact(args, None)
    document = sbom(artifact, lock=_sbom_lock(args.manifest, args.lockfile))
    text = document.render(args.format)
    if args.output is not None or args.format.endswith("-json"):
        for note in document.notes:
            print(f"note: {note}", file=sys.stderr)
    if args.output is None:
        out.write(text)
        return EXIT_OK
    target: Path = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {args.format} {target}", file=sys.stderr)
    return EXIT_OK


def _sbom_lock(manifest: Path, flag: Path | None) -> Lock | None:
    """``--lockfile``, else the lockfile the bake recorded or ``tundravm.lock`` beside it."""
    if flag is not None:
        return read_lock(flag)
    base = manifest if manifest.is_dir() else manifest.parent
    try:
        payload = json.loads((base / "bake-result.json").read_text(encoding="utf-8"))
        recorded = (payload.get("declarative") or {}).get("lockfile")
    except (OSError, ValueError, AttributeError):
        recorded = None
    for candidate in (recorded and Path(recorded), base / "tundravm.lock"):
        if candidate and candidate.is_file():
            return read_lock(candidate)
    return None


def _cmd_evidence(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args) if args.variant else None
    base: Path = args.out if args.out is not None else Path(loaded.lowered().build_dir)
    lockfile = None if _missing_lock(args) else args.lockfile
    found = evidence(
        loaded.recipe,
        out=base,
        variants=names,
        lock=lockfile,
        policy=args.policy,
        recipe_path=args.recipe,
        runner=args.runner,
    )
    directory = found.write(base / "evidence")
    print(found.render(args.format), file=out, end="")
    print(f"wrote {directory}", file=sys.stderr)
    if args.bundle is not None:
        print(f"wrote {found.bundle(args.bundle)}", file=sys.stderr)
    if args.html is not None:
        target: Path = args.html
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(found.html(), encoding="utf-8")
        print(f"wrote {target}", file=sys.stderr)
    return EXIT_OK if found.passed else EXIT_FAILURE


def _cmd_completion(args: argparse.Namespace, out: TextIO) -> int:
    print(render_completion(build_parser(), cast(Shell, args.shell)), file=out, end="")
    return EXIT_OK


@dataclass(frozen=True, slots=True)
class _CiRun:
    """What every ``ci`` step shares: the recipe and the one lock loaded for every step.

    The lockfile at :attr:`path` is ``--lockfile``, else the image's own
    ``build/tundravm.lock``; it is read once into :attr:`locked` (``None``: there
    is none, so lint and the tree check apply no pins and the lock step fails).
    An unreadable one fails every step with :attr:`problem`.
    """

    loaded: RecipeFile
    args: argparse.Namespace
    fmt: str
    path: Path
    locked: Lock | None
    problem: TdxError | None = None

    @classmethod
    def load(cls, args: argparse.Namespace) -> _CiRun:
        loaded = _load(args)
        path = _lock_path(loaded.lowered(), args.lockfile)
        try:
            return cls(loaded, args, resolve_format(args.format), path, _lock_at(path))
        except TdxError as exc:
            return cls(loaded, args, resolve_format(args.format), path, None, exc)

    def lock(self) -> Lock | None:
        """The shared lock; raises the error that kept it from loading."""
        if self.problem is not None:
            raise self.problem
        return self.locked

    def variants(self) -> tuple[str, ...]:
        return _variants(self.loaded, self.args)


def _lock_at(path: Path) -> Lock | None:
    return read_lock(path) if path.is_file() else None


CiStep = Callable[[_CiRun], tuple[bool, str, str]]


def _ci_lint(run: _CiRun) -> tuple[bool, str, str]:
    locked = run.lock()
    diagnostics = check_report(
        run.loaded.recipe, None, variants=run.variants(), lock=locked or run.path
    )
    report = render_as(diagnostics, run.fmt, recipe_path=run.args.recipe, strict=True)
    return (
        not failing(diagnostics, strict=True),
        report if diagnostics else "",
        render_summary(diagnostics),
    )


def _ci_compile(run: _CiRun) -> tuple[bool, str, str]:
    locked = run.lock()
    img = run.loaded.lowered()
    out: Path | None = run.args.out
    destination = out if out is not None else Path(img.build_dir) / "mkosi"
    with using_lock(img, locked) as pinned:
        result = diff_against(pinned, destination, profiles=run.variants())
    if result.is_clean:
        return True, "", f"{destination} is up to date"
    fmt = "stat" if run.fmt == "text" else run.fmt
    report = result.render(fmt, root=annotation_path(destination))
    count = len(result.changes)
    verdict = (
        f"{count} file{'' if count == 1 else 's'} stale in {destination}; "
        f"run `tundravm compile {run.args.recipe} --out {destination}`"
    )
    return False, report.rstrip(), verdict


def _ci_lock(run: _CiRun) -> tuple[bool, str, str]:
    path = run.path
    locked = run.lock() or read_lock(path)  # no lockfile: read_lock says so
    img = run.loaded.lowered()
    scoped = img.select(run.variants())
    partial = not set(img.state.profiles) <= set(scoped.active)
    drift = scoped.drift(locked.lockfile, resolver=None, partial=partial)
    if drift.is_clean:
        return True, "", f"{path} is up to date"
    count = len(drift.sections) or 1
    verdict = (
        f"{count} section{'' if count == 1 else 's'} drifted from {path}; "
        f"run `tundravm lock {run.args.recipe}`"
    )
    return False, render_drift(drift, run.fmt, path), verdict


CI_STEPS: tuple[tuple[str, CiStep], ...] = (
    ("lint", _ci_lint),
    ("compile", _ci_compile),
    ("lock", _ci_lock),
)


def _cmd_ci(args: argparse.Namespace, out: TextIO) -> int:
    run = _CiRun.load(args)
    fmt = run.fmt
    for index, (name, step) in enumerate(CI_STEPS):
        try:
            ok, report, verdict = step(run)
        except TdxError as exc:
            hint = f" ({exc.hint})" if exc.hint else ""
            ok, verdict = False, f"[{exc.code}] {exc.args[0]}{hint}"
            report = (
                workflow_command("error", verdict, title=f"tundravm ci: {name}")
                if fmt == "github"
                else ""
            )
        if report:
            print(report, file=out)
        print(f"{'ok' if ok else 'FAIL'} {name}: {verdict}", file=out)
        if not ok:
            for skipped, _step in CI_STEPS[index + 1 :]:
                print(f"skip {skipped}", file=out)
            return EXIT_FAILURE
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    img = loaded.lowered()
    origins: dict[str, str] = args.origins
    call = Invocation(
        recipe="" if origins.get("recipe") == "pyproject" else str(args.recipe),
        out=args.out if origins.get("out") == "flag" else None,
        lockfile=args.lockfile if origins.get("lockfile") == "flag" else None,
        lock_configured=origins.get("lockfile") == "pyproject",
        variants=names if args.variant else (),
        tree_flag=origins.get("tree") != "pyproject",
        recipe_path=str(args.recipe),
    )
    status = project_status(
        loaded,
        names,
        out=args.out if args.out is not None else Path(img.build_dir),
        lock_path=_lock_path(img, args.lockfile),
        runner=args.runner if args.runner is not None else run_probe,
        invocation=call,
        verify=args.verify,
        tree=getattr(args, "tree", None),
    )
    print(render_status(status, args.format), file=out)
    return EXIT_OK


def _cmd_clean(args: argparse.Namespace, out: TextIO) -> int:
    if args.recipe is None and args.out is None:
        raise ValidationError(
            "clean needs RECIPE or --out DIR.",
            hint="Pass the recipe file (its build directory is cleaned) or --out DIR.",
        )
    variants: tuple[str, ...] = ()
    destination: Path | None = args.out
    if args.recipe is not None:
        loaded = _load(args)
        variants = loaded.variants
        if destination is None:
            destination = Path(loaded.lowered().build_dir)
    assert destination is not None
    chosen = PARTS if args.all else tuple(part for part in PARTS if getattr(args, part))
    listing = not chosen and args.lockfile is None
    paths = clean_paths(destination, PARTS if listing else chosen, variants=variants)
    if args.lockfile is not None and args.lockfile.is_file():
        paths.append(args.lockfile)
    if not paths:
        print(f"nothing to clean in {destination}", file=out)
        return EXIT_OK
    if args.dry_run or listing:
        for path in paths:
            print(f"would remove {path}", file=out)
        if listing:
            print(
                "pass --all to remove them, or --sources, --tree, --artifacts or --state "
                "for some (the lockfile stays unless --lockfile names it)",
                file=out,
            )
        return EXIT_OK
    sudo = sudo_runner()
    failed: list[Path] = []
    for path in paths:
        problem = remove(path, sudo=sudo)
        if problem is None:
            print(f"removed {path}", file=out)
        else:
            print(f"not removed {path}: {problem}", file=out)
            failed.append(path)
    if failed:
        print(owner_hint(failed), file=out)
        return EXIT_FAILURE
    return EXIT_OK


def _cmd_init(args: argparse.Namespace, out: TextIO) -> int:
    if args.list_templates:
        width = max(map(len, TEMPLATES))
        for template, summary in TEMPLATES.items():
            default = " (default)" if template == DEFAULT_TEMPLATE else ""
            print(f"{template:<{width}}  {summary}{default}", file=out)
        return EXIT_OK
    root: Path = args.dir
    name = _init_name(args.name, root)
    title = name.replace("_", "-")
    recipe = root / f"{name}.py"
    tests = root / "tests" / f"test_{re.sub(r'[^A-Za-z0-9_]', '_', name)}.py"
    files = {
        recipe: render_recipe_template(
            title=title,
            filename=recipe.name,
            base=args.base,
            backend=args.backend,
            template=args.template,
        )
    }
    if args.tests:
        files[tests] = render_test_module(filename=recipe.name, template=args.template)
    if args.ci == "github":
        files[root / WORKFLOW_PATH] = render_workflow(recipe=recipe.name)
    existing = [path for path in files if path.exists()]
    if existing and not args.force:
        raise ValidationError(
            f"Refusing to overwrite existing file(s): {', '.join(map(str, existing))}",
            hint="Pass --force to overwrite them.",
            context={"dir": str(root)},
        )
    pyproject = root / "pyproject.toml"
    had_pyproject = pyproject.exists()
    files_if_absent = {
        pyproject: render_pyproject(
            name=_project_name(name), recipe=recipe.name, backend=args.backend
        ),
        root / "README.md": render_readme(
            title=title, filename=recipe.name, template=args.template, tests=args.tests
        ),
    }
    for path, text in files.items():
        _init_write(path, text, "overwrote" if path in existing else "created", out)
    for path, text in files_if_absent.items():
        if path.exists():
            print(f"kept {path} (already exists)", file=out)
        else:
            _init_write(path, text, "created", out)
    print(_init_gitignore(root / ".gitignore"), file=out)
    if had_pyproject:
        print(
            f"note: {pyproject} already exists; add the dependencies with "
            "`uv add tundravm` and `uv add --dev pytest` (tundravm is not on PyPI yet: "
            f"`{EDITABLE_INSTALL}`)",
            file=out,
        )
        if "[tool.tundravm]" not in pyproject.read_text(encoding="utf-8"):
            print(
                "note: add this table to it so commands run without RECIPE:\n"
                + render_table(recipe=recipe.name, backend=args.backend).rstrip(),
                file=out,
            )
    _init_lint(recipe, out)
    _init_next(root, recipe, tests if args.tests else None, args.ci == "github", out)
    if not args.no_doctor:
        _init_doctor(args.backend, args.runner, out)
    return EXIT_OK


def _init_write(path: Path, text: str, verb: str, out: TextIO) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"{verb} {path}", file=out)


def _init_lint(recipe: Path, out: TextIO) -> None:
    """Lint the new recipe and print the summary; findings never fail ``init``."""
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in DIR
    try:
        loaded = load_file(recipe, extra_paths=[os.getcwd()])
    finally:
        sys.dont_write_bytecode = keep
    report = check_report(loaded.recipe, loaded.image, variants=loaded.variants)
    codes = sorted({diagnostic.code for diagnostic in report})
    detail = f" ({', '.join(codes)}; `tundravm lint {recipe.name}` lists them)" if codes else ""
    print(f"lint {recipe.name}: {render_summary(report)}{detail}", file=out)
    if "source-unpinned" in codes:
        print(f"  `tundravm lock {recipe.name}` pins the source builds", file=out)


def _init_next(root: Path, recipe: Path, tests: Path | None, github: bool, out: TextIO) -> None:
    """Print the numbered follow-up commands, run from *root*."""
    name = recipe.name
    steps = [
        (
            f"tundravm lock {name}",
            "record recipe sections and pin source repositories/downloads in build/tundravm.lock",
        ),
        (f"tundravm compile {name} --out mkosi", "write the mkosi tree; commit it"),
        *(
            [(f"uv run pytest {tests.parent.name}", f"run {tests.name} against mkosi/")]
            if tests is not None
            else []
        ),
        (f"tundravm ci {name} --out mkosi", "the lint, tree and lockfile checks CI runs"),
        (f"tundravm bake {name} --out build", "build the image"),
    ]
    width = max(len(command) for command, _ in steps)
    where = "" if root.resolve() == Path.cwd().resolve() else f" (from {root})"
    print(f"next{where}:", file=out)
    for number, (command, why) in enumerate(steps, 1):
        print(f"  {number}. {command:<{width}}  {why}", file=out)
    print(f"  tundravm is not on PyPI yet: `{EDITABLE_INSTALL}` uses a local checkout", file=out)
    if github:
        print("then commit mkosi/ and build/tundravm.lock; the workflow checks both", file=out)


def _init_doctor(kind: BackendKind, runner: ProbeRunner | None, out: TextIO) -> None:
    """Probe the backend ``init`` wired in; a missing tool is reported, never fatal."""
    print(f"\nchecking the {kind} backend (tundravm doctor --backend {kind}):", file=out)
    if doctor(None, out, runner=runner, backend=Backend(kind).build_backend()) != EXIT_OK:
        print(
            f"the {kind} backend is not ready: install the missing tools above, or bake "
            "with --backend inprocess for a simulated build",
            file=out,
        )


def _init_name(requested: str | None, root: Path) -> str:
    raw = requested if requested is not None else root.resolve().name
    raw = raw.removesuffix(".py")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.")
    if requested is not None and (not name or name != raw):
        raise ValidationError(
            f"Invalid recipe name: {requested!r}",
            hint="Use letters, digits, `-`, `_` and `.` only.",
        )
    return name or "image"


def _project_name(name: str) -> str:
    """*name* as a PEP 508 project name: it starts and ends with a letter or digit."""
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", name) or "image"


def _init_gitignore(path: Path) -> str:
    """Append the build/ block to *path* unless present; return the report line."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(GITIGNORE_BLOCK, encoding="utf-8")
        return f"created {path}"
    text = path.read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    if GITIGNORE_MARKER in lines:
        return f"kept {path} (already ignores build output)"
    separator = (
        "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    )
    path.write_text(text + separator + GITIGNORE_BLOCK, encoding="utf-8")
    report = f"updated {path}"
    if lines & {"build/", "/build/", "build", "/build"}:
        report += (
            "\nwarning: an existing `build/` rule also hides build/tundravm.lock;"
            " remove it or `git add -f build/tundravm.lock`"
        )
    return report


__all__ = [
    "BACKEND_KINDS",
    "BACKEND_SNIPPETS",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_SDK_ERROR",
    "MEASUREMENT_SCHEMES",
    "PLACEHOLDER_BANNER",
    "ProbeRunner",
    "build_parser",
    "default_doctor_backends",
    "doctor",
    "main",
    "parse_deploy_target",
    "probe_requirement",
    "render_deployment",
    "render_drift",
    "render_measurements",
    "render_recipe_template",
    "run_probe",
]
