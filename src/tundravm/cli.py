"""Command-line interface for recipe files: ``tundravm <command> RECIPE``.

Every recipe command loads the ``Recipe`` a Python file binds (see
:func:`tundravm.recipe.load_file`) and runs one lifecycle step from
:mod:`tundravm.declarative.lifecycle` on the selected variants: repeat
``--variant NAME``, or omit it for every declared variant. ``measure`` and
``deploy`` read the ``bake-result.json`` manifest a ``bake`` wrote. ``init``
bootstraps a recipe project and ``ci`` runs lint, compile and lock checks in
one go; ``completion`` prints a shell completion script. With no arguments the
help and a quickstart are printed. Exit codes: 0 success, 2 SDK error (``E_*``
codes) or a usage error (unknown verbs and flags get a "did you mean"), 1 for a
failed check (``lint``, ``compile --check``, ``lock --check``, ``diff``, ``ci``,
``doctor``) or an unexpected failure.
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
from dataclasses import MISSING, fields
from pathlib import Path
from typing import TextIO, cast, get_args

from . import __version__
from ._image import Image
from .backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend, Requirement
from .backends.base import BuildBackend
from .check import failing, render_as, render_summary
from .completion import SHELLS, Shell, render_completion
from .declarative.lifecycle import (
    DEPLOY_TARGETS,
    Artifact,
    Backend,
    BackendKind,
    Deployment,
    DeployTarget,
    Lock,
    Measurements,
    ProbeRunner,
    Scheme,
    bake_image,
    check_report,
    deploy,
    lock_image,
    measure,
    probe,
    read_artifacts,
    read_lock,
    requirements_of,
    run_probe,
    select_artifact,
    using_lock,
    write_lock,
)
from .declarative.model import Target
from .declarative.resolve import resolve
from .diff import _wants_color, cmd_diff, diff_against
from .errors import TdxError, ValidationError
from .explain import (
    describe,
    diff_variants,
    render,
    render_markdown,
    render_variant_diff,
    render_variant_diff_markdown,
)
from .formats import annotation_path, format_help, resolve_format, workflow_command
from .lockfile import LockDrift, recipe_digest
from .measure import PlaceholderMeasurementWarning
from .measure import rtmr as rtmr_measure
from .observability import Event, JsonReporter, TextReporter, render_bake_summary
from .recipe import RecipeFile, load_file
from .templates import (
    BACKEND_SNIPPETS,
    GITIGNORE_BLOCK,
    GITIGNORE_MARKER,
    WORKFLOW_PATH,
    render_recipe_template,
    render_workflow,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SDK_ERROR = 2

MEASUREMENT_SCHEMES: tuple[Scheme, ...] = ("rtmr", "azure", "gcp")
BACKEND_KINDS: tuple[BackendKind, ...] = get_args(BackendKind)
LOCK_FILENAME = "tundravm.lock"

QUICKSTART = """\
quickstart:
  tundravm init . --name node                 write node.py and check its build backend
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
        return handler(args, out)
    except TdxError as exc:
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
            "`tundravm completion bash|zsh|fish` prints a shell completion script."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    _add_init(sub)

    inspect = _add_command(
        sub, "inspect", _cmd_inspect, help="Show what the recipe will produce (dry run)."
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
        "Pins already in the lockfile are kept while their source is unchanged; "
        "--update NAME resolves that source again. Drift is reported one section per "
        "line: `~` changed, `+` only in the recipe, `-` only in the lockfile, e.g. "
        "`~ variants.default.packages: +htop -jq`. Source builds are pinned under "
        "`fetches` and drift as `+ sources.<name>` (unpinned) or "
        "`~ sources.<name>: <old> -> <new>`."
    )
    lock.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Lockfile path (default: build/tundravm.lock).",
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
            "per drifted section, markdown a table. " + format_help()
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

    bake = _add_command(sub, "bake", _cmd_bake, help="Compile and build the image.")
    bake.add_argument(
        "--out", type=Path, default=None, help="Build output directory (default: build)."
    )
    bake.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help=(
            "Bake frozen against this lockfile (default: build/tundravm.lock when it "
            "exists; without one the bake is unpinned)."
        ),
    )
    bake.add_argument(
        "--backend",
        choices=BACKEND_KINDS,
        default=None,
        help="Build backend (default: the recipe file's `backend`).",
    )
    bake.epilog = (
        "Progress goes to stderr as `[variant] step ... ok (1.2s)` lines (a live timer on a "
        "terminal); the summary table goes to stdout. Backend output is hidden unless "
        "--verbose, but its last lines are shown when the build fails. The artifact "
        "manifest is OUT/bake-result.json, which `measure` and `deploy` read."
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
            "Target setting (repeatable). qemu: memory, cpus, ssh_port, tdx, daemonize; "
            "azure: storage_account, resource_group, location, vm_size; "
            "gcp: project, bucket, zone, machine_type."
        ),
    )
    deploy_cmd.add_argument(
        "--allow-placeholder",
        action="store_true",
        help="Deploy a simulated (in-process) artifact anyway.",
    )

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
        "Prints `ok STEP: ...` or `FAIL STEP: ...` per step (and `skip STEP` after a "
        "failure) and exits 1 on the first failure, including a missing lockfile."
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
        help="Bootstrap a recipe project, optionally with a GitHub Actions workflow.",
        description=(
            "Write <name>.py from the starter template and a .gitignore block for build/ "
            "that keeps build/tundravm.lock committed. With --ci github, also write "
            f"{WORKFLOW_PATH}, which posts `inspect --format markdown` to the job summary "
            "and runs `tundravm ci`."
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
    init.add_argument(
        "--force", action="store_true", help="Overwrite the recipe and workflow if they exist."
    )
    init.add_argument(
        "--no-doctor",
        action="store_true",
        help="Skip probing the chosen backend's host tools (`tundravm doctor --backend`).",
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


def _add_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Callable[[argparse.Namespace, TextIO], int],
    *,
    help: str,
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help, description=help)
    parser.set_defaults(handler=handler)
    parser.add_argument("recipe", type=Path, help="Path to the recipe .py file.")
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
        help="Apply this lockfile's source pins (default: build/tundravm.lock if present).",
    )


def _load(args: argparse.Namespace) -> RecipeFile:
    extra: list[str] = [os.getcwd(), *(args.pythonpath or [])]
    return load_file(args.recipe, attr=args.attr, extra_paths=extra)


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


def _listed(img: Image, names: tuple[str, ...] | None) -> tuple[str, ...]:
    return names if names is not None else (img.default_profile,)


def _cmd_inspect(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    fmt = args.format or ("json" if args.json else "text")
    if args.diff_variants is not None:
        return _inspect_diff(loaded, args, fmt, out)
    names = _variants(loaded, args)
    img = loaded.lowered()
    variants = _listed(img, names)
    described = {name: _describe(loaded, img, name) for name in variants}
    if fmt == "json":
        with img._operation_scope(variants) as active:
            digest = recipe_digest(img._recipe_payload(profile_names=active))
        payload = {"digest": digest, "variants": described}
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


def _describe(loaded: RecipeFile, img: Image, name: str) -> dict[str, object]:
    """*name*'s dry-run description with the recipe's parent and resolved fragment names."""
    fragments = resolve(loaded.recipe, variant=name).fragments
    parent = loaded.recipe.variant(name).parent
    return describe(img, profile=name, parent=parent, fragments=fragments)


def _cmd_lint(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    diagnostics = check_report(loaded.recipe, loaded.image, variants=names)
    fmt = resolve_format(args.format, alias="json" if args.json else None)
    print(render_as(diagnostics, fmt, recipe_path=args.recipe, strict=args.strict), file=out)
    return EXIT_FAILURE if failing(diagnostics, strict=args.strict) else EXIT_OK


def _lockfile(args: argparse.Namespace) -> Lock | None:
    path: Path | None = getattr(args, "lockfile", None)
    return None if path is None else read_lock(path)


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
    locked = _lockfile(args)
    if locked is None:
        result = img.compile(destination, force=True, profiles=names)
    else:
        with using_lock(img, locked):
            result = img.compile(destination, force=True, profiles=names)
    print(f"compiled {result.path}", file=out)
    print(f"  variants: {', '.join(result.profiles)}", file=out)
    print(f"  digest:   {result.digest}", file=out)
    return EXIT_OK


def _cmd_diff_loaded(
    args: argparse.Namespace, out: TextIO, img: Image, names: tuple[str, ...] | None
) -> int:
    locked = _lockfile(args)
    if locked is None:
        return cmd_diff(args, out, img, profiles=names)
    with using_lock(img, locked):
        return cmd_diff(args, out, img, profiles=names)


def _cmd_lock(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    img = loaded.lowered()
    path = _lock_path(img, args.path)
    if args.check:
        drift = img.lock_status(path, profiles=names)
        print(render_drift(drift, resolve_format(args.format), path), file=out)
        return EXIT_OK if drift.is_clean else EXIT_FAILURE
    previous = read_lock(path) if path.exists() else None
    if args.explain:
        if previous is not None:
            print(img.lock_status(path, profiles=names).render(), file=out)
        else:
            print(f"no lockfile at {path}; every section is new", file=out)
    locked = lock_image(
        img, names, previous=previous, update=tuple(args.update or ()), offline=args.offline
    )
    write_lock(locked, path)
    print(f"locked {path}", file=out)
    return EXIT_OK


def _lock_path(img: Image, path: Path | None) -> Path:
    return path if path is not None else Path(img.build_dir) / LOCK_FILENAME


def render_drift(drift: LockDrift, fmt: str, lock_path: Path) -> str:
    """Render a lock drift report in a resolved format: text, github or markdown."""
    if fmt == "github":
        return drift.github(lock_path)
    if fmt == "markdown":
        return drift.markdown()
    return drift.render()


def _cmd_bake(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    names = _variants(loaded, args)
    img = loaded.lowered()
    destination: Path = args.out if args.out is not None else Path(img.build_dir)
    lock_path = args.lockfile if args.lockfile is not None else _lock_path(img, None)
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
        )
    finally:
        reporter.close()
    if args.json_logs:
        return EXIT_OK
    if not args.quiet:
        print(file=out)
    print(render_bake_summary(result), file=out)
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
        if kind_name == "int":
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


def _cmd_deploy(args: argparse.Namespace, out: TextIO) -> int:
    target = cast(Target, args.target)
    using = parse_deploy_target(target, _parse_params(args.param))
    artifact = _artifact(args, target)
    result = deploy(artifact, using=using, allow_placeholder=args.allow_placeholder)
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
    _probe_measurement_tools(run, out)
    if loaded is not None:
        report = check_report(loaded.recipe, loaded.image, variants=loaded.variants)
        print(f"lint: {render_summary(report)}", file=out)
    return EXIT_OK if ready else EXIT_FAILURE


def _cmd_doctor(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args) if args.recipe is not None else None
    backend = None if args.backend is None else Backend(args.backend).build_backend()
    return doctor(loaded, out, runner=args.runner, backend=backend)


def _cmd_completion(args: argparse.Namespace, out: TextIO) -> int:
    print(render_completion(build_parser(), cast(Shell, args.shell)), file=out, end="")
    return EXIT_OK


CiStep = Callable[[RecipeFile, argparse.Namespace, str], tuple[bool, str, str]]


def _ci_lint(loaded: RecipeFile, args: argparse.Namespace, fmt: str) -> tuple[bool, str, str]:
    diagnostics = check_report(loaded.recipe, loaded.image, variants=_ci_variants(loaded, args))
    report = render_as(diagnostics, fmt, recipe_path=args.recipe, strict=True)
    return (
        not failing(diagnostics, strict=True),
        report if diagnostics else "",
        render_summary(diagnostics),
    )


def _ci_variants(loaded: RecipeFile, args: argparse.Namespace) -> tuple[str, ...]:
    names = _variants(loaded, args)
    return names if names is not None else loaded.variants


def _ci_compile(loaded: RecipeFile, args: argparse.Namespace, fmt: str) -> tuple[bool, str, str]:
    img = loaded.lowered()
    destination: Path = args.out if args.out is not None else Path(img.build_dir) / "mkosi"
    result = diff_against(img, destination, profiles=_variants(loaded, args))
    if result.is_clean:
        return True, "", f"{destination} is up to date"
    report = result.render("stat" if fmt == "text" else fmt, root=annotation_path(destination))
    count = len(result.changes)
    verdict = (
        f"{count} file{'' if count == 1 else 's'} stale in {destination}; "
        f"run `tundravm compile {args.recipe} --out {destination}`"
    )
    return False, report.rstrip(), verdict


def _ci_lock(loaded: RecipeFile, args: argparse.Namespace, fmt: str) -> tuple[bool, str, str]:
    img = loaded.lowered()
    path = _lock_path(img, args.lockfile)
    drift = img.lock_status(path, profiles=_variants(loaded, args))
    if drift.is_clean:
        return True, "", f"{path} is up to date"
    count = len(drift.sections) or 1
    verdict = (
        f"{count} section{'' if count == 1 else 's'} drifted from {path}; "
        f"run `tundravm lock {args.recipe}`"
    )
    return False, render_drift(drift, fmt, path), verdict


CI_STEPS: tuple[tuple[str, CiStep], ...] = (
    ("lint", _ci_lint),
    ("compile", _ci_compile),
    ("lock", _ci_lock),
)


def _cmd_ci(args: argparse.Namespace, out: TextIO) -> int:
    loaded = _load(args)
    fmt = resolve_format(args.format)
    for index, (name, step) in enumerate(CI_STEPS):
        try:
            ok, report, verdict = step(loaded, args, fmt)
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


def _cmd_init(args: argparse.Namespace, out: TextIO) -> int:
    root: Path = args.dir
    name = _init_name(args.name, root)
    recipe = root / f"{name}.py"
    workflow = root / WORKFLOW_PATH
    gitignore = root / ".gitignore"
    targets = [recipe, workflow] if args.ci == "github" else [recipe]
    existing = [path for path in targets if path.exists()]
    if existing and not args.force:
        raise ValidationError(
            f"Refusing to overwrite existing file(s): {', '.join(map(str, existing))}",
            hint="Pass --force to overwrite them.",
            context={"dir": str(root)},
        )
    contents = {
        recipe: render_recipe_template(
            title=name.replace("_", "-"), filename=recipe.name, base=args.base, backend=args.backend
        ),
        workflow: render_workflow(recipe=recipe.name),
    }
    for path in targets:
        verb = "overwrote" if path in existing else "created"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents[path], encoding="utf-8")
        print(f"{verb} {path}", file=out)
    print(_init_gitignore(gitignore), file=out)
    where = "" if root.resolve() == Path.cwd().resolve() else f" (from {root})"
    print(f"next{where}:", file=out)
    print(f"  tundravm compile {recipe.name} --out mkosi", file=out)
    print(f"  tundravm lock {recipe.name}", file=out)
    print(f"  tundravm ci {recipe.name} --out mkosi", file=out)
    if args.ci == "github":
        print("then commit mkosi/ and build/tundravm.lock; the workflow checks both", file=out)
        if not (root / "pyproject.toml").exists():
            print("note: the workflow runs `uv sync`; run `uv init && uv add tundravm`", file=out)
    if not args.no_doctor:
        _init_doctor(args.backend, args.runner, out)
    return EXIT_OK


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
