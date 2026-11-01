"""The ``tundravm`` command line: the operator's loop in twelve commands.

``new`` scaffolds a project; ``check`` validates an image without building it; ``lock``
pins its inputs; ``build`` makes an immutable build directory (``tundravm.builds``); ``show``
reads an image or a build back, and diffs two builds; ``run`` starts a local instance and
``deploy`` a cloud one, each with a receipt (``tundravm.instances``); ``attest`` checks an
instance's evidence and ``secrets`` delivers credentials and unlocks volumes after a fresh
check (``tundravm.bootstrap``); ``verify`` checks a build directory against its manifest;
``convert`` turns an mkosi tree or unit files into source; ``doctor`` says what this host
can do.

Exit codes: 0 success; 1 a failed check (``check`` errors, ``lock --check`` drift, a
failing ``verify``, an ``attest`` that does not pass, a ``build --reproduce`` that differs,
a blocked ``doctor`` row, a ``lock --check`` without a lockfile); 2 an SDK error
(``error [E_CODE]: message`` on stderr, one ``tundravm.error/1`` JSON line with ``--json``)
or a usage error; 141 a reader that closed the pipe (``| head``), silently. Every ``--json`` report
carries ``"schema": "tundravm.<command>/1"``. Shell completion scripts are generated from
this parser: ``python -m tundravm.completion bash|zsh|fish``.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TextIO, cast

from . import __version__, bootstrap, builds, deployments, instances, show
from . import manifest as mf
from . import templates as scaffold
from ._describe import describe
from ._image._image import DEV_PROFILE
from ._image._lower import component_classes
from ._reporting import TextReporter
from ._source import (
    SOURCES_DIRNAME,
    CargoBuild,
    GoBuild,
    LocalSource,
    SourceBuild,
    checkout_rust_toolchain,
)
from .attestation import render_attestation
from .backends.base import BuildBackend
from .capabilities import AREAS, Capability, Host, ToolchainNeeds, capabilities, failed
from .check import (
    RULES,
    component_rules,
    explain,
    explain_command,
    failing,
    render,
    render_github,
    render_summary,
    shipped_rules,
    summarize,
)
from .declarative._lowered import Lowered
from .declarative.lifecycle import (
    NO_LOCK,
    Backend,
    BackendKind,
    Lock,
    ProbeRunner,
    check_report,
    lock_image,
    lock_status,
    read_lock,
    write_lock,
)
from .declarative.resolve import resolve
from .diagnostics import Diagnostic
from .diff import diff_against
from .errors import SourceError, StateError, TdxError, ValidationError
from .formats import in_github_actions, workflow_command
from .project import RESOLUTION_HELP, Settings, settings
from .recipe import RecipeFile, load_file
from .templates import (
    GITIGNORE_BLOCK,
    WORKFLOW_PATH,
    render_app_files,
    render_pyproject,
    render_readme,
    render_recipe_template,
    render_test_module,
    render_workflow,
)
from .toolchain import running_toolchain, toolchain_drift

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SDK_ERROR = 2
EXIT_BROKEN_PIPE = 141
"""A reader closed stdout (``show --json | head``): 128 + SIGPIPE, as a shell reports it."""

COMMANDS = (
    "new",
    "check",
    "lock",
    "build",
    "show",
    "run",
    "deploy",
    "attest",
    "secrets",
    "verify",
    "convert",
    "doctor",
)
RUN_ACTIONS = ("status", "logs", "stop", "ssh", "mode")
DEPLOY_ACTIONS = ("status", "logs", "mode", "upgrade", "rollback")
NEW_TEMPLATES = ("service", "prover")
DOCTOR_AREAS = ("build", "run", "tdx", "azure", "gcp")

QUICKSTART = """\
quickstart:
  tundravm new demo && cd demo           a project: demo.py, its tests, pyproject.toml
  tundravm lock                          pin every source: build/tundravm.lock
  tundravm build                         build, measure: build/demo/production/<id>/
  tundravm build --dev                   the dev profile: build/demo/development/<id>/
  tundravm run --mode simulated --dev    boot the dev build locally: instance demo-1
  tundravm run stop demo-1"""


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    runner: ProbeRunner | None = None,
) -> int:
    """Run the CLI and return an exit code (never raises for SDK errors).

    *runner* replaces the host-tool probe ``doctor`` and ``new`` run (tests). Usage errors
    exit 2 through argparse's ``SystemExit``.
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
        code = handler(args, out)
        out.flush()
        return code
    except TdxError as exc:
        if getattr(args, "traceback", False):
            raise
        if getattr(args, "json", False):
            print(json.dumps(error_payload(exc), separators=(",", ":")), file=sys.stderr)
        else:
            print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return EXIT_SDK_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_BROKEN_PIPE


def _silence_stdout() -> None:
    """Point stdout at /dev/null, so the flush at exit cannot fail on the closed pipe again."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def schema(verb: str) -> str:
    """The ``schema`` key of *verb*'s ``--json`` report."""
    return f"tundravm.{verb}/1"


ERROR_SCHEMA = schema("error")


def error_payload(exc: TdxError) -> dict[str, object]:
    """The JSON line an SDK error prints to stderr under ``--json``."""
    return {
        "schema": ERROR_SCHEMA,
        "code": exc.code,
        "message": str(exc.args[0]) if exc.args else "",
        "hint": exc.hint,
        "context": dict(exc.context),
    }


def json_report(verb: str, payload: Mapping[str, object]) -> str:
    return json.dumps({"schema": schema(verb), **payload}, indent=2, sort_keys=True)


def _parse(parser: argparse.ArgumentParser, tokens: list[str]) -> argparse.Namespace:
    """``parse_args`` with "did you mean" for an unknown command or flag."""
    verbs = _subcommands(parser)
    first = next((t for t in tokens if not t.startswith("-")), None)
    if first is not None and first not in verbs and tokens.index(first) == 0:
        match = difflib.get_close_matches(first, list(verbs), n=1)
        hint = f"did you mean {match[0]!r}?" if match else f"choose from {', '.join(verbs)}"
        parser.error(f"unknown command {first!r} ({hint})")
    args, extras = parser.parse_known_args(tokens)
    if isinstance(getattr(args, "rest", None), list):
        # `deploy upgrade INSTANCE --build B NAME=@FILE`: argparse gives a `*` positional
        # only the words before the first option; the credentials after it are its too
        args.rest += [token for token in extras if not token.startswith("-")]
        extras = [token for token in extras if token.startswith("-")]
    if extras:
        command = verbs[args.command]
        flags = list(command._option_string_actions)
        found = [
            m[0]
            for token in extras
            if token.startswith("-")
            and (m := difflib.get_close_matches(token.split("=")[0], flags, n=1))
        ]
        hint = f" (did you mean {', '.join(dict.fromkeys(found))}?)" if found else ""
        command.error(f"unrecognized arguments: {' '.join(extras)}{hint}")
    return args


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


# ── parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tundravm",
        description="Build, run and verify confidential VM images declared as one Image.",
        epilog=RESOLUTION_HELP,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--traceback", action="store_true", help="Raise errors with the Python traceback."
    )
    image = argparse.ArgumentParser(add_help=False)
    image.add_argument(
        "image",
        type=Path,
        nargs="?",
        default=None,
        metavar="IMAGE",
        help="The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).",
    )
    loading = image.add_argument_group("loading the image file")
    loading.add_argument(
        "--attr",
        default=None,
        metavar="NAME",
        help="The name the image (or a zero-argument factory) is bound to in the file.",
    )
    loading.add_argument(
        "--pythonpath",
        action="append",
        default=None,
        metavar="DIR",
        help="Extra import directory for the file (repeatable); the working directory is one.",
    )
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="Print a JSON report.")

    def command(
        name: str,
        handler: Callable[[argparse.Namespace, TextIO], int],
        help: str,
        description: str,
        *parents: argparse.ArgumentParser,
    ) -> argparse.ArgumentParser:
        found = sub.add_parser(
            name,
            help=help,
            description=description,
            parents=[*parents, common],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        found.set_defaults(handler=handler)
        return found

    new = command(
        "new",
        _cmd_new,
        "Create a project: an image file, its tests, pyproject.toml.",
        "Write NAME/NAME.py from a template, tests/test_NAME.py, pyproject.toml with\n"
        '[tool.tundravm] image = "NAME.py", README.md and .gitignore; --ci adds a GitHub\n'
        "workflow that runs check, lock --check and build. The service template also makes\n"
        'app/, the Go program its image builds through Local("app"); the prover template\n'
        "makes policies/, where the operator-approved verifier policies go.",
    )
    new.add_argument("name", metavar="NAME", help="Project directory and image name.")
    new.add_argument(
        "--template",
        choices=NEW_TEMPLATES,
        default="service",
        help="service: your Go program in app/ as a sandboxed web service, and a dev profile "
        "(default); prover: a "
        "service behind attested TLS, its state on an operator-unlocked volume, a secret.",
    )
    new.add_argument("--ci", action="store_true", help=f"Also write {WORKFLOW_PATH}.")

    check = command(
        "check",
        _cmd_check,
        "Check the image: declarations, lint, lock drift, tree drift.",
        "Everything short of a build: resolution and declaration errors, every lint rule,\n"
        "drift from BUILD/tundravm.lock when it exists, the toolchain against the lock's,\n"
        "and with --tree a committed mkosi tree against what the image compiles to.\n"
        "Exit 1 on an error (or a drifted lock or tree); warnings are reported only.\n"
        "In GitHub Actions the findings are workflow annotations. --explain CODE says what\n"
        "a finding's code checks and how to fix it, an image's own component code through\n"
        "the image (docs/reference/lint.md lists the rest).",
        image,
        as_json,
    )
    check.add_argument(
        "--profile",
        action="append",
        default=None,
        metavar="P",
        help="Check this profile (repeatable; default: every profile).",
    )
    check.add_argument(
        "--tree", type=Path, default=None, metavar="DIR", help="A committed tree to compare."
    )
    check.add_argument(
        "--explain",
        action="append",
        default=None,
        metavar="CODE",
        help="Print what this finding code checks and how to fix it (repeatable); checks nothing.",
    )

    lock = command(
        "lock",
        _cmd_lock,
        "Resolve or check the lock: snapshot, sources, toolchain.",
        "Write BUILD/tundravm.lock: the image's section digests, a pin per source, the\n"
        "toolchain. Pins already locked are kept while their source is unchanged; --update\n"
        "resolves the named inputs again (every one when none is named; `toolchain` records\n"
        "the running toolchain). --check never writes and never touches the network: it\n"
        "prints the drift and exits 1 when the lock is stale or missing. A production build\n"
        "never changes the lock; a development build writes a missing one and re-pins\n"
        "changed Local sources.",
        image,
        as_json,
    )
    mode = lock.add_mutually_exclusive_group()
    mode.add_argument(
        "--update",
        nargs="*",
        default=None,
        metavar="INPUT",
        help="Resolve these inputs again (no name: all of them).",
    )
    mode.add_argument("--check", action="store_true", help="Report drift; exit 1 when stale.")

    build = command(
        "build",
        _cmd_build,
        "Build into an immutable build directory, with its manifest.",
        "Build one profile into BUILD/<image>/production/<id>/ (development/ for a\n"
        "non-production profile): payload.efi, qemu.raw and each --target's disk,\n"
        "measurements.json, sbom.spdx.json, inputs/, tree/, manifest.json (the identity;\n"
        "<id> is its digest), manifest.sig with --sign, observations.json. The directory\n"
        "appears atomically and is never written again; latest-production points at the\n"
        "newest. A production build needs the lock (`tundravm lock`) and refuses a stale\n"
        "one; a development build writes a missing one and re-pins changed Local sources.\n"
        "--tree-only writes the standalone mkosi project instead and builds nothing.",
        image,
    )
    which = build.add_mutually_exclusive_group()
    which.add_argument(
        "--profile", default=None, metavar="P", help="The profile (default: production)."
    )
    which.add_argument(
        "--dev", action="store_true", help="The dev profile (DevAccess): non-production."
    )
    build.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="T[,T]",
        help="Build these of the image's targets= (default: all of them); qemu is always built.",
    )
    build.add_argument(
        "--reproduce",
        action="store_true",
        help="Build again in an independent workspace and record the comparison; exit 1 "
        "when an artifact differs.",
    )
    build.add_argument(
        "--tree-only",
        type=Path,
        default=None,
        metavar="DIR",
        help="Only write the mkosi tree to DIR.",
    )
    build.add_argument(
        "--offline", action="store_true", help="No network: build only what the lock pins."
    )
    build.add_argument(
        "--sign",
        type=Path,
        default=None,
        metavar="KEY",
        help="Sign the manifest with this SSH key.",
    )
    build.add_argument(
        "--clean-cache",
        action="store_true",
        help="Drop the image's workspace first (fetched sources, mkosi's tools tree and cache).",
    )
    build.add_argument("-v", "--verbose", action="store_true", help="Echo mkosi's output.")

    show_cmd = command(
        "show",
        _cmd_show,
        "Show the resolved image or a build; --diff two builds.",
        "Without BUILD: the image as it resolves, its name and file first, then each\n"
        "profile's packages, files, units, sources and kernel (--json adds where each\n"
        "declaration came from). With BUILD (a build directory, an id prefix, or `latest`):\n"
        "the report `build` printed, then the manifest's provenance and unchecked\n"
        "guarantees. --units, --tree and --measurements narrow it. --diff OLD NEW compares\n"
        "two builds: packages, files, units, boot payload, measurements, policy coverage.",
        as_json,
    )
    show_cmd.add_argument(
        "subject", nargs="?", default=None, metavar="BUILD", help="A build, or an image file."
    )
    part = show_cmd.add_mutually_exclusive_group()
    part.add_argument("--units", action="store_true", help="The unit files.")
    part.add_argument("--tree", action="store_true", help="The tree's files.")
    part.add_argument(
        "--measurements", action="store_true", help="The measurement records (a build)."
    )
    part.add_argument(
        "--diff", nargs=2, default=None, metavar=("OLD", "NEW"), help="Compare two builds."
    )
    show_cmd.add_argument(
        "--profile",
        action="append",
        default=None,
        metavar="P",
        help="Profiles of the image (repeatable; default: all); --profile dev is its dev profile.",
    )
    show_cmd.add_argument(
        "--dev",
        action="store_true",
        help="The latest development build (`show IMAGE --dev`: the image's dev profile).",
    )
    show_cmd.add_argument(
        "--attr", default=None, metavar="NAME", help="The image's name in its file."
    )

    run = command(
        "run",
        _cmd_run,
        "Run a build locally as a named instance; status|logs|stop|ssh|mode.",
        "tundravm run [BUILD] --mode simulated|tdx [--dev] [--detach]\n"
        "    boot qemu.raw through OVMF from its ESP on a snapshot (the build is never\n"
        "    written), forward every TCP port the image's firewall accepts or its services\n"
        "    declare, and the management port :8745, from 127.0.0.1, and write\n"
        "    .tundravm/instances/<image>-<n>/.\n"
        "    simulated: no TDX (KVM if usable, else TCG), verdicts are `simulated`; a\n"
        "      production build boots fail-closed (tdx-evidence.service refuses, no service\n"
        "      starts, nothing is delivered), a --dev build runs its services.\n"
        "    tdx: TDVF and a tdx-guest object; refused unless `doctor --for tdx` is ready.\n"
        "tundravm run status INSTANCE   the process, control-plane and application readiness\n"
        "tundravm run logs INSTANCE     the serial console\n"
        "tundravm run stop INSTANCE     quit QEMU\n"
        "tundravm run ssh INSTANCE      a shell, only on a development build that runs SSH\n"
        "tundravm run mode INSTANCE     the security mode and what it means for evidence",
        as_json,
    )
    run.add_argument(
        "subject", nargs="?", default=None, metavar="BUILD|ACTION", help="A build, or an action."
    )
    run.add_argument(
        "instance", nargs="?", default=None, metavar="INSTANCE", help="The instance of an action."
    )
    run.add_argument(
        "--mode", choices=instances.MODES, default=None, help="Security mode (required to start)."
    )
    run.add_argument(
        "--dev", action="store_true", help="The latest development build (default: production)."
    )
    run.add_argument(
        "--detach", action="store_true", help="Return once QEMU runs (default: show the console)."
    )
    run.add_argument("--memory", default="2G", help="Guest memory (default: %(default)s).")
    run.add_argument("--cpus", type=int, default=2, help="Guest CPUs (default: %(default)s).")
    run.add_argument(
        "--identity", type=Path, default=None, metavar="KEY", help="SSH key for `run ssh`."
    )

    deploy = command(
        "deploy",
        _cmd_deploy,
        "Deploy a build to Azure or GCP; status|logs|mode|upgrade|rollback.",
        "tundravm deploy azure|gcp --build BUILD [--volume NAME=create:SIZE|existing:ID]\n"
        "    publish the build's disk and start a TDX VM from it, with a receipt; every\n"
        "    volume the image declares needs --volume (GCP attaches it as tundravm-NAME;\n"
        "    Azure is refused: its disks have no name the image can find). Settings come\n"
        "    from [tool.tundravm.gcp] (project, bucket, zone, machine_type) and\n"
        "    [tool.tundravm.azure] (storage_account, resource_group, location, vm_size,\n"
        "    gallery), or TUNDRAVM_GCP_* / TUNDRAVM_AZURE_*. BUILD is never guessed.\n"
        "tundravm deploy status|logs|mode INSTANCE\n"
        "tundravm deploy upgrade INSTANCE --build BUILD --policy FILE [NAME=@FILE...]\n"
        "    the replacement workflow (GCP): launch the candidate, attest it and deliver its\n"
        "    credentials, stop the old one, move the volumes, unlock, wait for readiness\n"
        "tundravm deploy rollback INSTANCE --policy FILE [NAME=@FILE...]\n"
        "    the same back to the instance it replaced",
        as_json,
    )
    deploy.add_argument(
        "action",
        metavar="azure|gcp|ACTION",
        choices=(*deployments.CLOUDS, *DEPLOY_ACTIONS),
        help="Where to deploy, or an action.",
    )
    deploy.add_argument(
        "rest", nargs="*", metavar="ARG", help="INSTANCE, then NAME=@FILE credentials."
    )
    deploy.add_argument(
        "--build", default=None, metavar="BUILD", help="The build to deploy (required)."
    )
    deploy.add_argument(
        "--volume",
        action="append",
        default=[],
        metavar="NAME=create:SIZE|existing:ID",
        help="A disk for the image's volume NAME (repeatable).",
    )
    deploy.add_argument(
        "--policy",
        type=Path,
        default=None,
        metavar="FILE",
        help="The verifier policy (upgrade, rollback).",
    )
    deploy.add_argument(
        "--identity", type=Path, default=None, metavar="KEY", help="The operator's SSH key."
    )
    deploy.add_argument("--validator", default=None, metavar="ENDPOINT", help="A tdxs validator.")

    attest = command(
        "attest",
        _cmd_attest,
        "Verify an instance's evidence against a policy; delivers nothing.",
        "Ask the instance's tdxs issuer for a quote bound to a fresh nonce and check MRTD and\n"
        "RTMR0..2 against FILE: an operator-approved tundravm.policy/2 file, or a build's\n"
        "measurements.json (the record for the instance's platform). With --validator a tdxs\n"
        "validator judges the quote's signature and collateral; only then can it be\n"
        "`trusted`. Verdicts: trusted, partial, measurements-match, measurements-mismatch,\n"
        "simulated (accepted for a simulated instance), untrusted. Exit 0 when it passes.\n\n"
        + _wrapped(
            f"Channel: {bootstrap.UNVERIFIED}. The first contact POSTs the operator's SSH "
            "public key to :8745; the quote then comes over that SSH session. --issuer names "
            "an endpoint the operator relays instead."
        ),
        as_json,
    )
    attest.add_argument("instance", metavar="INSTANCE")
    attest.add_argument(
        "--policy", type=Path, required=True, metavar="FILE", help="The verifier policy."
    )
    attest.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="REPORT",
        help="Write the record (tundravm.attestation/2).",
    )
    attest.add_argument(
        "--identity",
        type=Path,
        default=None,
        metavar="KEY",
        help="The operator's SSH key (default: ~/.ssh/id_ed25519).",
    )
    attest.add_argument(
        "--issuer",
        default=None,
        metavar="ENDPOINT",
        help="unix:PATH or tcp://HOST:PORT instead of the SSH relay.",
    )
    attest.add_argument(
        "--validator",
        default=None,
        metavar="ENDPOINT",
        help="A tdxs validator (unix:PATH or tcp://HOST:PORT).",
    )

    secrets = command(
        "secrets",
        _cmd_secrets_help,
        "Deliver credentials or unlock a volume after a fresh attestation.",
        "tundravm secrets deliver INSTANCE --policy FILE NAME=@FILE...\n"
        "    attest again, then write each value to /run/tundravm/credentials/NAME (0600);\n"
        "    a production build needs `trusted`.\n"
        "tundravm secrets unlock INSTANCE VOLUME\n"
        "    attest again with the policy of the last `attest`, then hand the passphrase\n"
        "    (typed, or piped on stdin) to the OperatorKey() volume.\n\n"
        + _wrapped(
            f"Channel: {bootstrap.UNVERIFIED}. Values travel on the SSH command's stdin only: "
            "never on argv, in a log, a tree, a manifest or a receipt."
        ),
    )
    verbs = secrets.add_subparsers(dest="secrets_command", required=True, metavar="deliver|unlock")
    for name, help_text in (
        ("deliver", "Deliver NAME=@FILE credentials."),
        ("unlock", "Unlock an OperatorKey() volume."),
    ):
        action = verbs.add_parser(
            name,
            help=help_text,
            description=_wrapped(f"{help_text} Channel: {bootstrap.UNVERIFIED}."),
            parents=[common],
        )
        action.add_argument("instance", metavar="INSTANCE")
        if name == "deliver":
            action.set_defaults(handler=_cmd_deliver)
            action.add_argument(
                "--policy", type=Path, required=True, metavar="FILE", help="The verifier policy."
            )
            action.add_argument(
                "values", nargs="+", metavar="NAME=@FILE", help="A secret and the file holding it."
            )
        else:
            action.set_defaults(handler=_cmd_unlock)
            action.add_argument("volume", metavar="VOLUME")
        action.add_argument(
            "--identity", type=Path, default=None, metavar="KEY", help="The operator's SSH key."
        )
        action.add_argument(
            "--validator", default=None, metavar="ENDPOINT", help="A tdxs validator."
        )

    verify = command(
        "verify",
        _cmd_verify,
        "Check a build directory against its manifest.",
        "Offline: the manifest is canonical and its id is its digest and the directory's\n"
        "name; every artifact, input and the tree hash to what it records, and nothing else\n"
        "is there; the UKI and its root hash are the recorded ones. Each check is ok, failed,\n"
        "missing or unchecked. A signature is checked with --allowed-signers (an OpenSSH\n"
        "allowed-signers file, namespace tundravm-manifest); --require-signature fails\n"
        "without a good one. --rebuild builds the recorded inputs again in an independent\n"
        "workspace and compares (it runs the project's code). Exit 1 when a check fails.",
        as_json,
    )
    verify.add_argument(
        "build", metavar="BUILD", help="A build directory, an id prefix, or `latest`."
    )
    verify.add_argument(
        "--require-signature", action="store_true", help="Fail without a valid signature."
    )
    verify.add_argument(
        "--allowed-signers", type=Path, default=None, metavar="FILE", help="Who may sign."
    )
    verify.add_argument("--rebuild", action="store_true", help="Rebuild independently and compare.")
    verify.add_argument(
        "--attr", default=None, metavar="NAME", help="--rebuild: the image's name in its file."
    )
    verify.add_argument(
        "--pythonpath",
        action="append",
        default=None,
        metavar="DIR",
        help="--rebuild: extra import directory for the image file (repeatable).",
    )

    convert = command(
        "convert",
        _cmd_convert,
        "Turn an mkosi tree or systemd unit files into source.",
        "--from systemd: unit files (a file, or a directory) as an Image file, one Service per\n"
        "service with its drop-ins, timer or socket; where a unit differs from what the image\n"
        "implies a note says so, and what no slot holds is reported. --from mkosi: the tree\n"
        "resolved by mkosi itself, as an Image file; every setting it cannot carry is\n"
        "reported. Parsing a tree runs its executable mkosi.version or mkosi.configure:\n"
        "refused unless --allow-scripts.",
    )
    convert.add_argument("path", type=Path, metavar="PATH")
    convert.add_argument(
        "--from", dest="source", choices=("mkosi", "systemd"), required=True, help="What PATH is."
    )
    convert.add_argument(
        "--allow-scripts",
        action="store_true",
        help="Let mkosi run the tree's scripts while parsing.",
    )

    doctor = command(
        "doctor",
        _cmd_doctor,
        "Report what this host can build, run, deploy and verify.",
        "One row per capability: ready, blocked (with the fix), optional, unavailable or\n"
        "unsupported. --for limits it to one: build, run (simulated), tdx, azure, gcp. Exit 1\n"
        "when a build row (or, with --for, a row of that area) is blocked. build:go and\n"
        "build:rustup block only when IMAGE has a Go source, or a Cargo source that pins a\n"
        "Rust toolchain; without an image they are optional.",
        image,
        as_json,
    )
    doctor.add_argument(
        "--for", dest="area", choices=DOCTOR_AREAS, default=None, help="One capability."
    )
    return parser


# ── shared ───────────────────────────────────────────────────────────────


def _wrapped(text: str) -> str:
    """*text* wrapped for a raw-formatted description."""
    return textwrap.fill(text, width=86)


def _settings(args: argparse.Namespace) -> Settings:
    return settings(getattr(args, "image", None))


def _loaded(args: argparse.Namespace, found: Settings) -> RecipeFile:
    if found.image is None:
        raise ValidationError(
            "No image file: pass IMAGE.",
            hint='Or name it in pyproject.toml ([tool.tundravm] image = "image.py"), or set '
            "TUNDRAVM_IMAGE.",
        )
    extra = [os.getcwd(), *(getattr(args, "pythonpath", None) or [])]
    loaded = load_file(found.image, attribute=getattr(args, "attr", None), extra_paths=extra)
    if found.backend is not None and (
        loaded.backend is None or loaded.backend.name != found.backend
    ):
        loaded.backend = Backend(cast(BackendKind, found.backend)).build_backend()
        loaded.image = None
    return loaded


def _lowered(loaded: RecipeFile, found: Settings) -> Lowered:
    return replace(loaded.lowered(), lock_file=found.lockfile, build_dir=found.build)


def _locked(found: Settings) -> Lock | None:
    return read_lock(found.lockfile) if found.lockfile.is_file() else None


def _profiles(img: Lowered, given: Sequence[str] | None) -> tuple[str, ...]:
    if not given:
        return img.profile_names
    img.select(given)
    return tuple(dict.fromkeys(given))


def _err(text: str) -> None:
    print(text, file=sys.stderr)


# ── new ──────────────────────────────────────────────────────────────────


def _cmd_new(args: argparse.Namespace, out: TextIO) -> int:
    name = args.name
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", name):
        raise ValidationError(
            f"Invalid project name {name!r}.",
            hint="Use lowercase letters, digits, `-` and `_`, starting with a letter.",
        )
    root = Path(name)
    if root.exists() and any(root.iterdir()):
        raise ValidationError(f"{root} exists and is not empty.", hint="Pick another NAME.")
    stem = name.replace("-", "_")
    filename = f"{stem}.py"
    template = args.template
    source = scaffold.tundravm_source()
    files: dict[Path, str] = {
        root / filename: render_recipe_template(
            title=name, filename=filename, release="trixie", template=template
        ),
        root / "tests" / f"test_{stem}.py": render_test_module(
            filename=filename, template=template
        ),
        root / "pyproject.toml": render_pyproject(name=name, recipe=filename, source=source),
        root / "README.md": render_readme(
            title=name, filename=filename, template=template, tests=True
        ),
        root / ".gitignore": GITIGNORE_BLOCK,
    }
    for rel, text in render_app_files(title=name, template=template).items():
        files[root / rel] = text
    if template == "prover":
        files[root / "policies" / "README.md"] = POLICIES_README.replace("{name}", name)
    if args.ci:
        files[root / WORKFLOW_PATH] = render_workflow(recipe=filename)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"created {path}", file=out)
    if source.note is not None:
        print(f"note: {source.note}", file=out)
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loaded = load_file(root / filename, extra_paths=[os.getcwd()])
    finally:
        sys.dont_write_bytecode = keep
    report = check_report(loaded.recipe, loaded.image, variants=loaded.variants)
    codes = sorted({d.code for d in report})
    detail = f" ({', '.join(codes)})" if codes else ""
    print(f"check {filename}: {render_summary(report)}{detail}", file=out)
    steps = (("cd " + name, ""), ("uv sync", ""), *scaffold.FIRST_STEPS)
    lines = scaffold.render_steps(steps, "  ").splitlines()
    print("next:", *(line.rstrip() for line in lines), sep="\n", file=out)
    return EXIT_OK


POLICIES_README = """\
# Verifier policies

The operator-approved copies of a build's predictions: `attest` and `secrets deliver`
check an instance against one of these files.

    tundravm build --target gcp
    jq '.records[] | select(.target == "gcp" and .boot == "disk") | .policy' \\
        build/{name}/latest-production/measurements.json > policies/{name}-gcp.json

Review it, add the platform registers you approve from a quote of a host you trust
(`platform_approval`: MRTD, RTMR0), and commit it. A policy without them verifies as
`partial` at best.
"""


# ── check ────────────────────────────────────────────────────────────────


def _cmd_check(args: argparse.Namespace, out: TextIO) -> int:
    found = _settings(args)
    if args.explain:
        print(_explained(args, found), file=out)
        return EXIT_OK
    loaded = _loaded(args, found)
    names = loaded.variants
    if args.profile:
        unknown = [name for name in args.profile if name not in loaded.variants]
        if unknown:
            raise ValidationError(
                f"Unknown profile(s): {', '.join(unknown)}.",
                hint=f"Profiles: {', '.join(loaded.variants)}.",
            )
        names = tuple(dict.fromkeys(args.profile))
    locked = _locked(found)
    diagnostics = check_report(
        loaded.recipe, None, variants=names, lock=found.lockfile if locked else NO_LOCK
    )
    resolved = not any(d.severity == "error" for d in diagnostics)
    if locked is None:
        _err(
            f"note: no lockfile at {found.lockfile}: a production build needs one (lock-missing); "
            "`tundravm lock` writes it"
        )
    elif resolved:
        diagnostics.extend(lock_status(loaded.recipe, locked, variants=names))
        diagnostics.extend(_toolchain(loaded.backend, locked))
    drift = None
    if args.tree is not None and resolved and not Path(args.tree).is_dir():
        diagnostics.append(
            Diagnostic(
                "tree-drift",
                "error",
                f"{args.tree} is not a directory: there is no committed tree to compare",
                subject=args.tree,
                hint=f"Write it with `tundravm build --tree-only {args.tree}` and commit it.",
            )
        )
    elif args.tree is not None and resolved:
        img = _lowered(loaded, found)
        drift = diff_against(img, args.tree, profiles=names)
        for change in drift.changes:
            diagnostics.append(
                Diagnostic(
                    "tree-drift",
                    "error",
                    _TREE_DRIFT[change.status].format(tree=args.tree),
                    subject=change.path,
                    hint=f"Rewrite it with `tundravm build --tree-only {args.tree}` and review "
                    "the diff.",
                )
            )
    bad = failing(diagnostics)
    if args.json:
        payload = {
            "diagnostics": [d.to_dict() for d in diagnostics],
            "summary": summarize(diagnostics),
            "tree": None if drift is None else drift.to_dict(),
            "passed": not bad,
        }
        print(json_report("check", payload), file=out)
        return EXIT_FAILURE if bad else EXIT_OK
    annotate = in_github_actions()
    if annotate:
        print(render_github(diagnostics, found.image or ""), file=out)
    else:
        print(render(diagnostics), file=out)
    if drift is not None and drift.is_clean:
        print(f"tree {args.tree} is up to date", file=out)
    declared = component_rules(component_classes(loaded.recipe))
    if not annotate and (explained := explain_command(diagnostics, declared, image=args.image)):
        print(explained, file=out)
    return EXIT_FAILURE if bad else EXIT_OK


def _explained(args: argparse.Namespace, found: Settings) -> str:
    """``check --explain``: tundravm's codes and the shipped components' as they are; a code
    neither has is looked up in the components of the image, given or the project's."""
    shipped = shipped_rules()
    if found.image is None or all(code in RULES or code in shipped for code in args.explain):
        return explain(args.explain, shipped)
    loaded = _loaded(args, found)
    return explain(args.explain, {**component_rules(component_classes(loaded.recipe)), **shipped})


_TREE_DRIFT = {
    "added": "missing from {tree}: the image compiles it",
    "removed": "in {tree}, but the image does not compile it",
    "modified": "modified in {tree}: differs from what the image compiles to",
    "mode": "mode differs in {tree} from what the image compiles to",
}
"""A ``tree-drift`` message per status: the committed tree is the old side of the diff."""


def _toolchain(backend: BuildBackend | None, locked: Lock) -> tuple[Diagnostic, ...]:
    recorded = locked.lockfile.toolchain
    if recorded is None:
        return ()
    return toolchain_drift(recorded, running_toolchain(backend, locked=recorded))


# ── lock ─────────────────────────────────────────────────────────────────


def _cmd_lock(args: argparse.Namespace, out: TextIO) -> int:
    found = _settings(args)
    loaded = _loaded(args, found)
    img = _lowered(loaded, found)
    path = found.lockfile
    if args.check:
        if not path.is_file():
            return _no_lockfile(args, path, out)
        drift = img.select(img.profile_names).lock_status(path)
        if args.json:
            print(
                json_report(
                    "lock",
                    {
                        "path": str(path),
                        "clean": drift.is_clean,
                        "drift": [] if drift.is_clean else drift.render().splitlines(),
                    },
                ),
                file=out,
            )
        else:
            print(drift.github(path) if in_github_actions() else drift.render(), file=out)
        return EXIT_OK if drift.is_clean else EXIT_FAILURE
    previous = read_lock(path) if path.is_file() else None
    update: tuple[str, ...] = ()
    if args.update is not None:
        update = tuple(args.update) or tuple(img.lock_sources())
    locked = lock_image(img, img.profile_names, previous=previous, update=update)
    write_lock(locked, path)
    if args.json:
        print(
            json_report(
                "lock",
                {"path": str(path), "recipe_digest": locked.recipe_digest, "updated": list(update)},
            ),
            file=out,
        )
        return EXIT_OK
    print(f"locked {path}", file=out)
    recorded = locked.lockfile.toolchain
    if recorded is not None and recorded.mkosi is not None:
        print(f"  toolchain: {recorded.summary()}", file=out)
    return EXIT_OK


def _no_lockfile(args: argparse.Namespace, path: Path, out: TextIO) -> int:
    """``lock --check`` without a lockfile: drift like any other, exit 1."""
    line = f"no lockfile at {path}: run `tundravm lock` and commit it"
    if args.json:
        print(json_report("lock", {"path": str(path), "clean": False, "drift": [line]}), file=out)
    elif in_github_actions():
        message = f"No lockfile at {path}. Run `tundravm lock` and commit the lockfile."
        print(workflow_command("error", message, title="lock drift"), file=out)
    else:
        print(line, file=out)
    return EXIT_FAILURE


# ── build ────────────────────────────────────────────────────────────────


def _service_ports(loaded: RecipeFile, profile: str) -> tuple[int, ...]:
    """The TCP ports *profile*'s services and units say they listen on (``ports=``)."""
    found: set[int] = set()
    for item in resolve(loaded.recipe, variant=profile).items:
        for port in getattr(item, "ports", ()) or ():
            number = port if isinstance(port, int) else getattr(port, "number", None)
            if isinstance(number, int) and getattr(port, "protocol", "tcp") == "tcp":
                found.add(number)
    return tuple(sorted(found))


def _cmd_build(args: argparse.Namespace, out: TextIO) -> int:
    found = _settings(args)
    loaded = _loaded(args, found)
    img = _lowered(loaded, found)
    targets = builds.parse_targets(args.target)
    if args.tree_only is not None:
        if args.reproduce or args.sign or args.offline or args.clean_cache:
            raise ValidationError(
                "--tree-only builds nothing: --reproduce, --sign, --offline and --clean-cache "
                "do not apply.",
                hint="Drop them, or drop --tree-only.",
            )
        profiles = None
        if args.profile or args.dev:
            profiles = [builds.choose_profile(img, args.profile, dev=args.dev)]
        if targets is not None:
            for name in profiles or img.profile_names:
                img = replace(
                    img,
                    targets={**dict(img.targets), name: builds.choose_targets(img, name, targets)},
                )
        locked = _locked(found)
        if locked is not None:
            img = replace(img, lock_file=found.lockfile)
        argv = builds.export_tree(img, profiles, args.tree_only)
        print(f"wrote {args.tree_only}", file=out)
        for name, options in zip(profiles or img.profile_names, argv, strict=True):
            words = ("mkosi -C", str(args.tree_only), options, "build")
            print(f"  {name}: {' '.join(word for word in words if word)}", file=out)
        return EXIT_OK
    profile = builds.choose_profile(img, args.profile, dev=args.dev)
    if img.backend is None:
        raise ValidationError(
            "No build backend.", hint="Set TUNDRAVM_BACKEND or [tool.tundravm] backend."
        )
    reporter = TextReporter(sys.stderr, verbose=args.verbose)
    try:
        outcome = builds.build(
            img,
            root=found.build,
            profile=profile,
            targets=targets,
            offline=args.offline,
            reproduce=args.reproduce,
            sign_key=args.sign,
            clean_cache=args.clean_cache,
            reporter=reporter,
            ports=_service_ports(loaded, profile),
        )
    finally:
        reporter.close()
    result = outcome.build
    for name in outcome.repinned:
        print(f"lock: {name} re-pinned, local source changed", file=out)
    if outcome.lock_written:
        print(f"lock written: {found.lockfile} (commit it)", file=out)
    state = "built" if outcome.created else "already built (identical; left untouched)"
    print(f"{state} {result.path}", file=out)
    for line in show.render_report(show.report(result)):
        print(line, file=out)
    for item in result.manifest.get("unchecked", ()):
        print(f"  unchecked: {item}", file=out)
    if outcome.reproduction is not None:
        rec = outcome.reproduction
        detail = ", ".join(rec.get("differ") or ()) or rec.get("error", "")
        print(f"reproduction: {rec['state']}" + (f" ({detail})" if detail else ""), file=out)
        if rec["state"] != "matched":
            return EXIT_FAILURE
    return EXIT_OK


# ── show ─────────────────────────────────────────────────────────────────


def _is_image(subject: str | None) -> bool:
    return subject is not None and subject.endswith(".py") and Path(subject).is_file()


def _cmd_show(args: argparse.Namespace, out: TextIO) -> int:
    if args.diff is not None:
        found = settings(None)
        old, new = (builds.resolve(ref, root=found.build, image=None) for ref in args.diff)
        report = show.diff_builds(old, new)
        print(
            json_report("show", {"diff": report}) if args.json else show.render_diff(report),
            file=out,
        )
        return EXIT_OK
    if _is_image(args.subject) or (args.subject is None and not args.dev and _project_image()):
        return _show_image(args, out)
    if args.profile:
        raise ValidationError(
            "--profile selects profiles of an image file, not of a build.", hint="Drop it."
        )
    found = settings(None)
    try:
        build = _resolve_build(args.subject, found, dev=args.dev)
    except StateError as exc:
        if args.dev and args.subject is None and found.image is not None:
            exc.hint = f"{exc.hint} The image's dev profile is `tundravm show --profile dev`."
        raise
    if args.units:
        units = show.build_units(build)
        print(
            json_report("show", {"units": units}) if args.json else show.render_units(units),
            file=out,
        )
    elif args.tree:
        rows = show.build_tree(build)
        print(
            json_report("show", {"tree": rows}) if args.json else show.render_files(rows), file=out
        )
    elif args.measurements:
        records = show.build_measurements(build)
        print(
            json_report("show", {"measurements": records})
            if args.json
            else show.render_measurements(records),
            file=out,
        )
    else:
        print(
            json_report("show", show.build_report(build))
            if args.json
            else show.render_build(build),
            file=out,
        )
    return EXIT_OK


def _resolve_build(ref: str | None, found: Settings, *, dev: bool) -> builds.Build:
    """BUILD, else the latest production (with *dev*, development) build."""
    if dev and ref is not None:
        raise ValidationError(
            "--dev picks the latest development build; BUILD already names one.",
            hint="Drop --dev, or drop BUILD.",
        )
    return builds.resolve(ref, root=found.build, image=None, dev=dev)


def _project_image() -> bool:
    return settings(None).image is not None


def _show_image(args: argparse.Namespace, out: TextIO) -> int:
    if args.measurements:
        raise ValidationError(
            "An image has no measurements until it is built.",
            hint="Run `tundravm build`, then `tundravm show latest --measurements`.",
        )
    args.image = Path(args.subject) if args.subject else None
    found = _settings(args)
    loaded = _loaded(args, found)
    img = _lowered(loaded, found)
    given = [*(args.profile or ()), *([DEV_PROFILE] if args.dev else [])]
    names = show.ordered_profiles(loaded.variants, _profiles(img, given))
    locked = _locked(found)
    if args.units:
        units = show.image_units(img, names, locked)
        print(
            json_report("show", {"units": units}) if args.json else show.render_units(units),
            file=out,
        )
        return EXIT_OK
    if args.tree:
        rows = show.image_tree(img, names, locked)
        print(
            json_report("show", {"tree": rows}) if args.json else show.render_files(rows), file=out
        )
        return EXIT_OK
    described = {}
    for name in names:
        declared = name in loaded.variants
        fragments = resolve(loaded.recipe, variant=name).fragments if declared else ()
        inherits = loaded.recipe.variant(name).inherits if declared else None
        described[name] = describe(img, profile=name, inherits=inherits, fragments=fragments)
    report = show.image_report(img, names, described, file=found.image)
    if args.json:
        report["provenance"] = show.provenance(loaded.recipe, names)
    print(json_report("show", report) if args.json else show.render_image(report), file=out)
    return EXIT_OK


# ── run ──────────────────────────────────────────────────────────────────


def _instance(found: Settings, name: str | None, verb: str, action: str) -> instances.Instance:
    if not name:
        raise ValidationError(
            f"`{verb} {action}` needs INSTANCE.", hint=f"tundravm {verb} {action} INSTANCE"
        )
    return instances.open_instance(found.instances, name)


def _cmd_run(args: argparse.Namespace, out: TextIO) -> int:
    found = settings(None)
    if args.subject in RUN_ACTIONS:
        instance = _instance(found, args.instance, "run", args.subject)
        if instance.platform != "qemu":
            raise ValidationError(
                f"{instance.id} is a {instance.platform} deployment.",
                hint=f"Use `tundravm deploy {args.subject} {instance.id}`.",
            )
        return _run_action(args.subject, instance, args, out)
    if args.instance is not None:
        raise ValidationError(
            f"Unknown run action {args.subject!r}.", hint=f"Actions: {', '.join(RUN_ACTIONS)}."
        )
    if args.mode is None:
        raise ValidationError(
            "run needs --mode simulated or --mode tdx.",
            hint="The security mode is always explicit: --mode simulated boots without TDX "
            "(add --dev to start the services), --mode tdx needs `tundravm doctor --for tdx`.",
        )
    build = _resolve_build(args.subject, found, dev=args.dev)
    instance = instances.launch(
        found.instances,
        build,
        mode=args.mode,
        detach=args.detach,
        out=out,
        memory=args.memory,
        cpus=args.cpus,
    )
    if args.detach:
        print(f"stop it with `tundravm run stop {instance.id}`", file=out)
    return EXIT_OK


def _run_action(
    action: str, instance: instances.Instance, args: argparse.Namespace, out: TextIO
) -> int:
    if action == "status":
        report = instances.status(instance)
        if args.json:
            print(json_report("run", report), file=out)
        else:
            for key in ("id", "mode", "state", "services", "control_plane", "application"):
                if key in report:
                    print(f"{key:<14} {report[key]}", file=out)
            build = cast(Mapping[str, object], report["build"])
            print(f"{'build':<14} {build['id']} ({build['kind']})", file=out)
        return EXIT_OK
    if action == "logs":
        for line in instances.logs(instance):
            print(line, file=out)
        return EXIT_OK
    if action == "stop":
        stopped = instances.stop(instance)
        print(f"{'stopped' if stopped else 'was not running:'} {instance.id}", file=out)
        return EXIT_OK
    if action == "mode":
        return _mode(instance, args, out)
    instances.ssh_permitted(instance)
    session = bootstrap.Session(instance, bootstrap.identity_path(args.identity))
    argv = session.argv("")[:-1]
    print(
        f"note: host key not verified through the attested endpoint ({bootstrap.UNVERIFIED})",
        file=sys.stderr,
    )
    os.execvp(argv[0], argv)


def _mode(instance: instances.Instance, args: argparse.Namespace, out: TextIO) -> int:
    build = instance.data["build"]
    production = instance.mode == "tdx" and build["kind"] == "production"
    report = {
        "id": instance.id,
        "mode": instance.mode,
        "platform": instance.platform,
        "build_kind": build["kind"],
        "production_evidence": production,
        "meaning": "a TDX guest whose evidence a production policy can accept"
        if production
        else (
            instances.FAIL_CLOSED
            if instances.fail_closed(instance)
            else "no TDX: evidence is `simulated`, never production"
            if instance.mode == "simulated"
            else "a TDX guest of a development build: never production"
        ),
    }
    if args.json:
        print(json_report(args.command, report), file=out)
    else:
        print(
            f"{instance.id}: {instance.mode} on {instance.platform}, {build['kind']} build: "
            f"{report['meaning']}",
            file=out,
        )
    return EXIT_OK


# ── deploy ───────────────────────────────────────────────────────────────


def _credentials(values: Sequence[str]) -> list[str]:
    bad = [v for v in values if "=@" not in v]
    if bad:
        raise ValidationError(f"{bad[0]!r} is not NAME=@FILE.", hint="Credentials are NAME=@FILE.")
    return list(values)


def _passphrases(build: builds.Build) -> Callable[[str], bytes]:
    """Each operator-key volume's passphrase, asked for when the workflow reaches its
    unlock (a refused upgrade never prompts)."""
    del build
    return bootstrap.read_passphrase


def _cmd_deploy(args: argparse.Namespace, out: TextIO) -> int:
    found = settings(None)
    action = args.action
    if action in deployments.CLOUDS:
        if args.rest:
            raise ValidationError(
                f"Unexpected {' '.join(args.rest)}.", hint=f"tundravm deploy {action} --build BUILD"
            )
        if args.build is None:
            raise ValidationError(
                f"deploy {action} needs --build BUILD.",
                hint="A deployment names its build explicitly: "
                "--build build/<image>/production/<id>.",
            )
        build = builds.resolve(args.build, root=found.build, image=None)
        instance = deployments.deploy(
            found.instances, action, build, deployments.parse_volumes(args.volume), out=out
        )
        if args.json:
            print(json_report("deploy", instance.data), file=out)
        else:
            print(f"deployed {build.id} to {action}: instance {instance.id}", file=out)
            print(f"  address   {instance.data.get('address') or 'unknown'}", file=out)
            print(f"  firewall  {instance.data['firewall']}", file=out)
            print(f"  receipt   {instance.path / instances.INSTANCE}", file=out)
        return EXIT_OK
    instance = _instance(found, args.rest[0] if args.rest else None, "deploy", action)
    rest = args.rest[1:]
    if action in ("status", "logs", "mode") and rest:
        raise ValidationError(
            f"Unexpected {' '.join(rest)}.", hint=f"tundravm deploy {action} INSTANCE"
        )
    if action == "status":
        report = deployments.status(instance)
        if args.json:
            print(json_report("deploy", report), file=out)
        else:
            for key, value in report.items():
                if key != "build":
                    print(f"{key:<14} {value}", file=out)
        return EXIT_OK
    if action == "logs":
        out.write(deployments.logs(instance))
        return EXIT_OK
    if action == "mode":
        return _mode(instance, args, out)
    if args.policy is None:
        raise ValidationError(
            f"deploy {action} needs --policy FILE.",
            hint="The candidate is attested before it gets anything.",
        )
    credentials = _credentials(rest)
    if action == "upgrade":
        if args.build is None:
            raise ValidationError(
                "deploy upgrade needs --build BUILD.", hint="Name the new build explicitly."
            )
        build = builds.resolve(args.build, root=found.build, image=None)
        candidate = deployments.upgrade(
            found.instances,
            instance,
            build,
            policy=args.policy,
            credentials=credentials,
            passphrases=_passphrases(build),
            identity=args.identity,
            validator=args.validator,
            out=out,
        )
        print(f"upgraded {instance.id} -> {candidate.id}", file=out)
        return EXIT_OK
    previous = deployments.rollback(
        found.instances,
        instance,
        policy=args.policy,
        credentials=credentials,
        passphrases=_passphrases(instance.build),
        identity=args.identity,
        validator=args.validator,
        out=out,
    )
    print(f"rolled back {instance.id} -> {previous.id}", file=out)
    return EXIT_OK


# ── attest, secrets ──────────────────────────────────────────────────────


def _cmd_attest(args: argparse.Namespace, out: TextIO) -> int:
    found = settings(None)
    instance = instances.open_instance(found.instances, args.instance)
    result = bootstrap.attest(
        instance, args.policy, identity=args.identity, issuer=args.issuer, validator=args.validator
    )
    if args.json:
        print(
            json_report(
                "attest",
                {
                    **json.loads(render_attestation(result, "json")),
                    "channel": bootstrap.UNVERIFIED if args.issuer is None else args.issuer,
                },
            ),
            file=out,
        )
    else:
        print(render_attestation(result, "text"), file=out)
        if args.issuer is None:
            print(f"channel: {bootstrap.UNVERIFIED}", file=out)
    if args.output is not None:
        result.save(args.output)
        _err(f"wrote {args.output}")
    return EXIT_OK if result.passed else EXIT_FAILURE


def _cmd_secrets_help(args: argparse.Namespace, out: TextIO) -> int:
    del args
    print("tundravm secrets deliver|unlock: see `tundravm secrets --help`", file=out)
    return EXIT_SDK_ERROR


def _cmd_deliver(args: argparse.Namespace, out: TextIO) -> int:
    found = settings(None)
    instance = instances.open_instance(found.instances, args.instance)
    result, names = bootstrap.deliver(
        instance, args.policy, args.values, identity=args.identity, validator=args.validator
    )
    print(f"delivered {', '.join(names)} to {instance.id} (verdict {result.verdict})", file=out)
    print(f"channel: {bootstrap.UNVERIFIED}", file=out)
    return EXIT_OK


def _cmd_unlock(args: argparse.Namespace, out: TextIO) -> int:
    found = settings(None)
    instance = instances.open_instance(found.instances, args.instance)
    result = bootstrap.unlock(
        instance,
        args.volume,
        lambda: bootstrap.read_passphrase(args.volume),
        identity=args.identity,
        validator=args.validator,
    )
    print(f"unlocked {args.volume} on {instance.id} (verdict {result.verdict})", file=out)
    print(f"channel: {bootstrap.UNVERIFIED}", file=out)
    return EXIT_OK


# ── verify ───────────────────────────────────────────────────────────────


def _cmd_verify(args: argparse.Namespace, out: TextIO) -> int:
    found = _settings(args)
    build = builds.resolve(args.build, root=found.build, image=None)
    result = mf.verify(
        build.path, require_signature=args.require_signature, allowed_signers=args.allowed_signers
    )
    checks = list(result.checks)
    if args.rebuild:
        checks.append(_rebuild(args, found, build))
        result = replace(result, checks=tuple(checks))
    print(json_report("verify", result.to_dict()) if args.json else result.render(), file=out)
    return EXIT_OK if result.passed else EXIT_FAILURE


def _rebuild(args: argparse.Namespace, found: Settings, build: builds.Build) -> mf.Check:
    """Build the recorded profile and targets again, against the recorded lock, in a
    fresh build root, and compare every artifact with the manifest."""
    import tempfile

    loaded = _loaded(args, found)
    with tempfile.TemporaryDirectory(prefix="tundravm-rebuild-") as scratch:
        root = Path(scratch)
        (root / "tundravm.lock").write_bytes(
            (build.path / mf.INPUTS / "tundravm.lock").read_bytes()
        )
        img = replace(loaded.lowered(), lock_file=root / "tundravm.lock", build_dir=root)
        reporter = TextReporter(sys.stderr)
        try:
            again = builds.build(
                img,
                root=root,
                profile=build.profile,
                targets=build.targets,
                reporter=reporter,
                ports=_service_ports(loaded, build.profile),
            )
        finally:
            reporter.close()
        want = {
            k: v
            for k, v in build.manifest["artifacts"].items()
            if "/" not in k and k not in (mf.MEASUREMENTS, mf.SBOM)
        }
        have = again.build.manifest["artifacts"]
        differ = sorted(
            k for k, v in want.items() if (have.get(k) or {}).get("sha256") != v.get("sha256")
        )
    if differ:
        return mf.Check(
            "rebuild", "failed", f"{', '.join(differ)} differ from an independent rebuild"
        )
    return mf.Check("rebuild", "ok", f"an independent rebuild matched {len(want)} artifacts")


# ── convert ──────────────────────────────────────────────────────────────


def _cmd_convert(args: argparse.Namespace, out: TextIO) -> int:
    from . import convert

    if args.source == "systemd":
        if args.allow_scripts:
            raise ValidationError("--allow-scripts applies to --from mkosi.", hint="Drop it.")
        converted = convert.from_systemd(args.path)
    else:
        converted = convert.from_mkosi(args.path, allow_scripts=args.allow_scripts)
    out.write(converted.source)
    for line in convert.render(converted):
        print(line, file=out)
    return EXIT_OK


# ── doctor ───────────────────────────────────────────────────────────────


def _rows(area: str | None, host: Host, needs: ToolchainNeeds | None) -> list[Capability]:
    rows = list(capabilities(host, backend=None, areas=AREAS, needs=needs))
    if area is None:
        return rows
    if area == "build":
        return [row for row in rows if row.area == "build"]
    if area in ("run", "tdx"):
        name = "run:simulated" if area == "run" else "run:tdx"
        return [row for row in rows if row.name == name]
    return [row for row in rows if row.name.endswith(f":{area}")]


def doctor(
    out: TextIO,
    *,
    area: str | None = None,
    runner: ProbeRunner | None = None,
    host: Host | None = None,
    as_json: bool = False,
    needs: ToolchainNeeds | None = None,
) -> int:
    """Print one row per capability (of *area*); exit 1 when a relevant one is blocked.

    *needs* is what the image builds with (:func:`_toolchain_needs`); ``None`` without one.
    """
    facts = host if host is not None else Host()
    if runner is not None:
        facts.runner = runner
    rows = _rows(area, facts, needs)
    if as_json:
        payload = {
            "rows": [{"name": r.name, "verdict": r.verdict, "detail": r.detail} for r in rows]
        }
        print(json_report("doctor", payload), file=out)
    else:
        width = max(16, *(len(row.name) for row in rows))
        for row in rows:
            print(row.render(width), file=out)
    blocked = any(r.verdict == "blocked" for r in rows) if area else failed(rows, areas=None)
    return EXIT_FAILURE if blocked else EXIT_OK


def _cmd_doctor(args: argparse.Namespace, out: TextIO) -> int:
    found = _settings(args)
    needs = None if found.image is None else _toolchain_needs(_loaded(args, found), found)
    return doctor(out, area=args.area, runner=args.runner, as_json=args.json, needs=needs)


def _toolchain_needs(loaded: RecipeFile, found: Settings) -> ToolchainNeeds:
    """The image's Go sources, and its Cargo sources that pin a Rust toolchain.

    A Cargo source pins one when its checkout in the workspace (a local source: its
    directory) says so; one not checked out yet (or whose toolchain file the build would
    refuse) counts as needing ``rustup``.
    """
    img = _lowered(loaded, found)
    locked = _locked(found)
    pins = {} if locked is None else {f.name: f for f in locked.lockfile.fetches if f.name}
    checkouts = found.build / img.image_id / builds.WORK / SOURCES_DIRNAME
    go: list[str] = []
    rustup: list[str] = []
    for spec in img.lock_sources().values():
        if not isinstance(spec, SourceBuild):
            continue
        if isinstance(spec.build, GoBuild):
            go.append(spec.key)
        elif isinstance(spec.build, CargoBuild) and _may_pin(spec, spec.pin_from(pins), checkouts):
            rustup.append(spec.key)
    return ToolchainNeeds(tuple(go), tuple(rustup))


def _may_pin(spec: SourceBuild, pin: str | None, checkouts: Path) -> bool:
    checkout = None if pin is None else checkouts / spec.pin_dir(pin)
    if isinstance(spec.source, LocalSource):
        checkout = spec.source.directory
    if checkout is None or not checkout.is_dir():
        return True
    try:
        return checkout_rust_toolchain(spec, checkout) is not None
    except SourceError:
        return True


__all__ = [
    "COMMANDS",
    "DEPLOY_ACTIONS",
    "ERROR_SCHEMA",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_SDK_ERROR",
    "QUICKSTART",
    "RUN_ACTIONS",
    "build_parser",
    "doctor",
    "error_payload",
    "json_report",
    "main",
    "schema",
]
