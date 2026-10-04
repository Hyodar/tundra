"""Command-line interface for recipe files: ``tundravm <command> RECIPE``.

Every command loads an ``Image`` from a Python recipe file via
:func:`tundravm.recipe.load_recipe`, applies the requested profile selection,
and runs one lifecycle step. Exit codes: 0 success, 2 SDK error
(``E_*`` codes), 1 unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from . import __version__
from .check import cmd_check
from .diff import cmd_diff
from .errors import TdxError, ValidationError
from .image import Image
from .lockfile import recipe_digest
from .recipe import load_recipe

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SDK_ERROR = 2


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    """Run the CLI and return an exit code (never raises for SDK errors)."""
    out = stdout if stdout is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace, TextIO], int] = args.handler
    try:
        return handler(args, out)
    except TdxError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return EXIT_SDK_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tundravm",
        description="Build, inspect, lock, and bake TDX VM image recipes.",
        epilog=(
            "RECIPE is a Python file that binds an Image to `img` or defines a "
            "zero-argument `build() -> Image` function. Use --attr to pick another name."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    explain = _add_command(
        sub, "explain", _cmd_explain, help="Show what the recipe will produce (dry run)."
    )
    explain.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    digest = _add_command(
        sub, "digest", _cmd_digest, help="Print the recipe digest used by lockfiles."
    )
    del digest

    compile_cmd = _add_command(
        sub, "compile", _cmd_compile, help="Emit the mkosi project tree for the recipe."
    )
    compile_cmd.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Destination directory (default: <build_dir>/mkosi).",
    )
    compile_cmd.add_argument(
        "--force", action="store_true", help="Re-emit even if the recipe is unchanged."
    )
    compile_cmd.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the tree at --out is stale relative to the recipe.",
    )

    lock = _add_command(sub, "lock", _cmd_lock, help="Write the lockfile for the recipe.")
    lock.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Lockfile path (default: <build_dir>/tundravm.lock).",
    )

    bake = _add_command(sub, "bake", _cmd_bake, help="Compile and build the image.")
    bake.add_argument(
        "--out", type=Path, default=None, help="Build output directory (default: <build_dir>)."
    )
    bake.add_argument(
        "--frozen", action="store_true", help="Refuse to build if the lockfile is stale."
    )
    bake.add_argument(
        "--lock",
        action="store_true",
        help="Write the lockfile first, then build with --frozen semantics.",
    )
    bake.add_argument("--force", action="store_true", help="Force recompilation.")

    check = _add_command(sub, "check", _cmd_check, help="Lint the recipe and report diagnostics.")
    check.add_argument("--json", action="store_true", help="Emit diagnostics as JSON.")
    check.add_argument("--strict", action="store_true", help="Treat warnings as errors (exit 1).")

    diff = _add_command(
        sub, "diff", _cmd_diff, help="Show how the recipe differs from a compiled tree."
    )
    diff.add_argument(
        "--against",
        type=Path,
        default=None,
        help="Compiled mkosi tree to compare with (default: <build_dir>/mkosi).",
    )
    diff.add_argument("--stat", action="store_true", help="Only list changed files.")
    diff.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colorize output (default: %(default)s).",
    )

    new = sub.add_parser(
        "new",
        help="Write a starter recipe file.",
        description="Write a starter recipe file you can edit and run with the other commands.",
    )
    new.set_defaults(handler=_cmd_new)
    new.add_argument("path", type=Path, help="Recipe file to create (must not exist).")
    new.add_argument(
        "--base", default="debian/trixie", help="Base distribution (default: %(default)s)."
    )
    new.add_argument(
        "--backend",
        choices=sorted(BACKEND_SNIPPETS),
        default="lima",
        help="Build backend to wire in (default: %(default)s).",
    )
    new.add_argument(
        "--force", action="store_true", help="Overwrite the file if it already exists."
    )
    return parser


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
    parser.add_argument(
        "--attr",
        default=None,
        help="Name of the Image instance or factory function inside RECIPE.",
    )
    parser.add_argument(
        "--pythonpath",
        action="append",
        default=None,
        metavar="DIR",
        help="Extra import directory for the recipe (repeatable). CWD is always included.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "-p",
        "--profile",
        action="append",
        default=None,
        metavar="NAME",
        help="Profile to operate on (repeatable).",
    )
    selection.add_argument(
        "--all-profiles", action="store_true", help="Operate on every declared profile."
    )
    return parser


@contextmanager
def _selected(img: Image, args: argparse.Namespace) -> Iterator[Image]:
    """Activate the profiles requested on the command line."""
    if args.all_profiles:
        with img.all_profiles():
            yield img
        return
    names: list[str] | None = args.profile
    if not names:
        yield img
        return
    known = sorted(img.state.profiles)
    unknown = [name for name in names if name not in img.state.profiles]
    if unknown:
        raise ValidationError(
            f"Unknown profile(s): {', '.join(unknown)}.",
            hint=f"Declared profiles: {', '.join(known)}",
            context={"recipe": str(args.recipe)},
        )
    with img.profiles(*names):
        yield img


def _load(args: argparse.Namespace) -> Image:
    extra: list[str] = [os.getcwd(), *(args.pythonpath or [])]
    return load_recipe(args.recipe, attr=args.attr, extra_paths=extra)


def _cmd_explain(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        profiles = _operation_profiles(img, args)
        if args.json:
            payload = {name: img.explain(profile=name) for name in profiles}
            print(json.dumps(payload, indent=2, sort_keys=True), file=out)
            return EXIT_OK
        for index, name in enumerate(profiles):
            if index:
                print(file=out)
            print(img.summary(profile=name).rstrip(), file=out)
    return EXIT_OK


def _operation_profiles(img: Image, args: argparse.Namespace) -> list[str]:
    if args.all_profiles:
        return sorted(img.state.profiles)
    if args.profile:
        return list(args.profile)
    return [img.default_profile]


def _cmd_digest(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        payload = img._recipe_payload(profile_names=img._active_profiles)
    print(recipe_digest(payload), file=out)
    return EXIT_OK


def _cmd_check(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        return cmd_check(args, out, img)


def _cmd_diff(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        return cmd_diff(args, out, img)


def _cmd_compile(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    destination = args.out if args.out is not None else Path(img.build_dir) / "mkosi"
    with _selected(img, args):
        if args.check:
            args.against = destination
            args.stat = True
            args.color = "never"
            return cmd_diff(args, out, img)
        result = img.compile(destination, force=args.force)
    print(f"compiled {result.path}", file=out)
    print(f"  profiles: {', '.join(result.profiles)}", file=out)
    print(f"  digest:   {result.digest}", file=out)
    return EXIT_OK


def _cmd_lock(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        path = img.lock(args.path)
    print(f"locked {path}", file=out)
    return EXIT_OK


def _cmd_bake(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        if args.lock:
            lock_path = img.lock()
            print(f"locked {lock_path}", file=out)
        result = img.bake(args.out, frozen=args.frozen or args.lock, force=args.force)
    for name in sorted(result.profiles):
        profile_result = result.profiles[name]
        print(f"baked {name}", file=out)
        for target in sorted(profile_result.artifacts):
            print(f"  {target:<6} {profile_result.artifacts[target].path}", file=out)
        if profile_result.report_path is not None:
            print(f"  report {profile_result.report_path}", file=out)
    return EXIT_OK


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

RECIPE_TEMPLATE = '''"""{title} image recipe.

Inspect:  tundravm explain {filename}
Compile:  tundravm compile {filename}
Build:    tundravm bake {filename} --lock
"""

from tundravm import Image
{backend_import}
from tundravm.modules import Devtools

img = Image(base="{base}", backend={backend_expr})
img.install("systemd", "curl", "jq")
img.file("/etc/motd", content="{title}\\n")
img.user("app", system=True, shell="/bin/false")
img.service("app", command="/usr/bin/true")
img.debloat(enabled=True)
img.output_targets("qemu")

with img.profile("dev"):
    img.apply(Devtools())
'''


def render_recipe_template(*, title: str, filename: str, base: str, backend: str) -> str:
    """Return the starter recipe source for ``tundravm new``."""
    backend_import, backend_expr = BACKEND_SNIPPETS[backend]
    return RECIPE_TEMPLATE.format(
        title=title,
        filename=filename,
        base=base,
        backend_import=backend_import,
        backend_expr=backend_expr,
    )


def _cmd_new(args: argparse.Namespace, out: TextIO) -> int:
    path: Path = args.path
    if path.exists() and not args.force:
        raise ValidationError(
            f"Refusing to overwrite existing file: {path}",
            hint="Pass --force to overwrite it.",
            context={"path": str(path)},
        )
    if path.suffix != ".py":
        raise ValidationError(
            f"Recipe files must end in .py: {path}",
            context={"path": str(path)},
        )
    title = path.stem.replace("_", "-").replace(" ", "-") or "tundravm"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_recipe_template(
            title=title, filename=path.name, base=args.base, backend=args.backend
        ),
        encoding="utf-8",
    )
    print(f"wrote {path}", file=out)
    print(f"next: tundravm explain {path}", file=out)
    return EXIT_OK


__all__ = [
    "BACKEND_SNIPPETS",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_SDK_ERROR",
    "build_parser",
    "main",
    "render_recipe_template",
]
