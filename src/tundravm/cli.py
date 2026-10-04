"""Command-line interface for recipe files: ``tundravm <command> RECIPE``.

Every command loads an ``Image`` from a Python recipe file via
:func:`tundravm.recipe.load_recipe`, applies the requested profile selection,
and runs one lifecycle step. ``measure`` and ``deploy`` read the
``bake-result.json`` a previous ``bake`` wrote. Exit codes: 0 success, 2 SDK
error (``E_*`` codes), 1 for a failed check (``check``, ``compile --check``, ``lock --check``,
``diff``) or an unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, cast, get_args

from . import __version__
from .backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend, Requirement
from .backends.base import BuildBackend
from .check import cmd_check
from .check import render as render_diagnostics
from .diff import _wants_color, cmd_diff
from .errors import TdxError, ValidationError
from .image import Image
from .lockfile import recipe_digest
from .models import DeployResult, OutputTarget
from .observability import Event, JsonReporter, TextReporter, render_bake_summary
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
        description="Build, inspect, lock, bake, measure, and deploy TDX VM image recipes.",
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
    lock.epilog = (
        "Drift is reported one section per line: `~` changed, `+` only in the recipe, "
        "`-` only in the lockfile, e.g. `~ profiles.default.packages: +htop -jq`. "
        "A lockfile from before section digests reports every section as `+`. "
        "Source builds are pinned under `fetches` and drift as `+ sources.<name>` "
        "(unpinned) or `~ sources.<name>: <old> -> <new>`."
    )
    lock.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Lockfile path (default: <build_dir>/tundravm.lock).",
    )
    lock.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write; print the drift and exit 1 if the lockfile is stale, "
            "or print `lock is up to date` and exit 0."
        ),
    )
    lock.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Do not touch the network: reuse the existing lockfile's source pins and fail "
            "if a source build would need resolving."
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
    bake.epilog = (
        "Progress goes to stderr as `[profile] step ... ok (1.2s)` lines (a live timer on a "
        "terminal); the summary table goes to stdout. Backend output is hidden unless "
        "--verbose, but its last lines are shown when the build fails."
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

    measure = _add_command(
        sub,
        "measure",
        _cmd_measure,
        help="Derive expected TDX measurements from the last bake.",
    )
    measure.add_argument(
        "--backend", required=True, choices=MEASUREMENT_BACKENDS, help="Measurement scheme."
    )
    measure.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    measure.add_argument(
        "--out", type=Path, default=None, help="Bake output directory if bake used --out."
    )

    deploy = _add_command(
        sub, "deploy", _cmd_deploy, help="Deploy the last baked artifact of one profile."
    )
    deploy.add_argument(
        "--target", required=True, choices=get_args(OutputTarget), help="Deploy target."
    )
    deploy.add_argument(
        "--out", type=Path, default=None, help="Bake output directory if bake used --out."
    )
    deploy.add_argument("--memory", default=None, help="Guest memory, e.g. 4GiB.")
    deploy.add_argument("--cpus", type=int, default=None, help="Guest vCPU count.")
    deploy.add_argument(
        "--param",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Adapter parameter (repeatable), e.g. --param ssh_port=2223.",
    )

    doctor_cmd = sub.add_parser(
        "doctor",
        help="Check the host tools the build backends need.",
        description=(
            "Report Python/tundravm versions and probe backend tools. With RECIPE, "
            "probe only its backend and run `check`; exit 1 if a required tool is missing."
        ),
    )
    doctor_cmd.set_defaults(handler=_cmd_doctor)
    doctor_cmd.add_argument(
        "recipe", type=Path, nargs="?", default=None, help="Optional recipe .py file."
    )
    doctor_cmd.add_argument(
        "--attr",
        default=None,
        help="Name of the Image instance or factory function inside RECIPE.",
    )
    doctor_cmd.add_argument(
        "--pythonpath",
        action="append",
        default=None,
        metavar="DIR",
        help="Extra import directory for the recipe (repeatable). CWD is always included.",
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
        if args.check:
            drift = img.lock_status(args.path)
            print(drift.render(), file=out)
            return EXIT_OK if drift.is_clean else EXIT_FAILURE
        if args.explain:
            current = args.path if args.path is not None else Path(img.build_dir) / "tundravm.lock"
            if current.exists():
                print(img.lock_status(current).render(), file=out)
            else:
                print(f"no lockfile at {current}; every section is new", file=out)
        path = img.lock(args.path, offline=args.offline)
    print(f"locked {path}", file=out)
    return EXIT_OK


def _cmd_bake(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    err = sys.stderr
    reporter = (
        JsonReporter(out)
        if args.json_logs
        else TextReporter(
            err, verbose=args.verbose, quiet=args.quiet, color=_wants_color(args.color, err)
        )
    )
    try:
        with _selected(img, args):
            if args.lock:
                lock_path = img.lock()
                if args.json_logs:
                    extra = {"source": "cli", "path": str(lock_path)}
                    reporter.emit(Event("log", None, f"locked {lock_path}", 0.0, extra))
                else:
                    print(f"locked {lock_path}", file=out)
            result = img.bake(
                args.out, frozen=args.frozen or args.lock, force=args.force, reporter=reporter
            )
    finally:
        reporter.close()
    if args.json_logs:
        return EXIT_OK
    if not args.quiet:
        print(file=out)
    print(render_bake_summary(result), file=out)
    for name in sorted(result.profiles):
        targets = sorted(result.profiles[name].artifacts)
        if targets:
            selector = "" if name == img.default_profile else f" --profile {name}"
            print(f"next: tundravm deploy {args.recipe}{selector} --target {targets[0]}", file=out)
            break
    return EXIT_OK


MEASUREMENT_BACKENDS = ("rtmr", "azure", "gcp")


def _single_profile(img: Image, args: argparse.Namespace, command: str) -> str:
    profiles = _operation_profiles(img, args)
    if len(profiles) != 1:
        raise ValidationError(
            f"`{command}` operates on exactly one profile; got {len(profiles)}.",
            hint="Pass a single --profile NAME.",
            context={"profiles": ", ".join(profiles)},
        )
    return profiles[0]


def _cmd_measure(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    with _selected(img, args):
        profile = _single_profile(img, args, "measure")
        if args.out is not None:
            img.last_bake(args.out)
        measurements = img.measure(backend=args.backend, profile=profile)
    if args.json:
        print(json.dumps(measurements.to_dict(), indent=2, sort_keys=True), file=out)
        return EXIT_OK
    print(f"measurements {profile} ({measurements.backend})", file=out)
    width = max((len(key) for key in measurements.values), default=0)
    for key, value in sorted(measurements.values.items()):
        print(f"  {key:<{width}}  {value}", file=out)
    return EXIT_OK


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


def _cmd_deploy(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args)
    params = _parse_params(args.param)
    with _selected(img, args):
        profile = _single_profile(img, args, "deploy")
        if args.out is not None:
            img.last_bake(args.out)
        result = img.deploy(
            target=cast(OutputTarget, args.target),
            profile=profile,
            parameters=params,
            memory=args.memory,
            cpus=args.cpus,
        )
    print(render_deploy_result(result, profile=profile), file=out)
    return EXIT_OK


def render_deploy_result(result: DeployResult, *, profile: str) -> str:
    """Human-readable ``DeployResult``: header line plus aligned key/value rows."""
    rows = [("deployment", result.deployment_id)]
    if result.endpoint is not None:
        rows.append(("endpoint", result.endpoint))
    rows.extend(sorted(result.metadata.items()))
    width = max(len(key) for key, _ in rows)
    lines = [f"deployed {profile} to {result.target}"]
    lines.extend(f"  {key:<{width}}  {value}" for key, value in rows)
    return "\n".join(lines)


ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Default ``doctor`` probe: run *argv* with a 10s timeout, capturing output."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)


def probe_requirement(requirement: Requirement, runner: ProbeRunner) -> tuple[bool, str]:
    """Run one probe; return ``(ok, report line)``."""
    try:
        result = runner(requirement.probe)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        label = "missing (optional)" if requirement.optional else "missing"
        return False, f"{label} {requirement.tool} — {requirement.hint}"
    text = (result.stdout or result.stderr or "").strip()
    version = text.splitlines()[0] if text else ""
    return True, f"ok {requirement.tool} {version}".rstrip()


def _backend_requirements(backend: BuildBackend) -> tuple[Requirement, ...]:
    requirements = getattr(backend, "requirements", None)
    return tuple(requirements()) if callable(requirements) else ()


def _probe_backend(backend: BuildBackend, runner: ProbeRunner, out: TextIO) -> bool:
    """Print one backend's probe lines; return False if a required tool is missing."""
    requirements = _backend_requirements(backend)
    lines: list[str] = []
    ready = True
    for requirement in requirements:
        ok, line = probe_requirement(requirement, runner)
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
    """The real backends ``doctor`` probes when no recipe is given."""
    return (
        LimaMkosiBackend(cpus=1, memory="1GiB", disk="10GiB"),
        NixMkosiBackend(),
        LocalLinuxBackend(),
    )


def doctor(img: Image | None, out: TextIO, *, runner: ProbeRunner | None = None) -> int:
    """Environment report. Exit 1 if *img*'s backend lacks a required tool, else 0."""
    probe = runner if runner is not None else run_probe
    print(f"tundravm {__version__}", file=out)
    print(f"python {platform.python_version()}", file=out)
    if img is None:
        for backend in default_doctor_backends():
            _probe_backend(backend, probe, out)
        return EXIT_OK
    if img.backend is None:
        print("backend: none configured", file=out)
        ready = False
    else:
        ready = _probe_backend(img.backend, probe, out)
    summary = render_diagnostics(img.check()).splitlines()[-1]
    print(f"check: {summary}", file=out)
    return EXIT_OK if ready else EXIT_FAILURE


def _cmd_doctor(args: argparse.Namespace, out: TextIO) -> int:
    img = _load(args) if args.recipe is not None else None
    return doctor(img, out)


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
    "MEASUREMENT_BACKENDS",
    "ProbeRunner",
    "build_parser",
    "default_doctor_backends",
    "doctor",
    "main",
    "probe_requirement",
    "render_deploy_result",
    "render_recipe_template",
    "run_probe",
]
