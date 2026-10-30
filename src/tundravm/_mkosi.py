"""The mkosi adapter: the only module of tundravm that imports mkosi.

mkosi has no Python API, but its CLI is two functions: ``parse_config(argv)`` and
``run_verb(args, tools, images)``. :func:`invoke` runs them the way ``mkosi.__main__.main()``
does (signal handlers, mkosi's log format, ``resource_path(mkosi.resources)``) inside mkosi's
own ``fork_and_wait``. mkosi 27's ``run_verb`` calls ``acquire_privileges()`` and
``unshare(CLONE_NEWNS)`` in the calling process, so the fork keeps tundravm's process out of
those namespaces; ``die()`` is a ``SystemExit`` in the child, which ``fork_and_wait`` turns
into ``CalledProcessError`` in the parent, and :func:`invoke` into :class:`MkosiError`
carrying the log.

Two places where the plan's sketch and mkosi's code differ:

- ``fork_and_wait`` sends the child's return value through a pipe that the parent reads only
  after ``waitpid()``; a value bigger than the pipe buffer (64 KiB, less than one image's
  ``summary --json``) deadlocks. The child therefore returns nothing and writes what mkosi
  printed on stdout to a file the parent reads.
- mkosi writes progress through the root logger, but the commands it runs (apt, systemd-repart,
  build scripts) write to file descriptors 1 and 2 directly. A logging handler alone would
  miss them, so the child's descriptors 1 and 2 go to a pipe that a thread in the parent
  copies to the log and mirrors line by line; mkosi's own handler (``log_setup()``) writes its
  formatted records to that same descriptor 2. ``terminal=True`` (``vm``, ``shell``) leaves
  the terminal alone and logs mkosi's records to the log file only.

Privilege is per process: when this host cannot give an unprivileged process a user
namespace (:func:`needs_sudo`, decided the way ``run_verb`` acquires one), the backend runs
``sudo -E <python> -m tundravm._mkosi --cwd DIR --log FILE --result FILE -- ARGV``
(:func:`sudo_argv`), which runs :func:`invoke` as root and writes the outcome as JSON
(:func:`read_result`).

Every mkosi symbol used here is pinned by ``tests/unit/test_mkosi_adapter.py``; importing this
module with any mkosi but :data:`PINNED_VERSION` raises :class:`MkosiVersionError`. mkosi
only imports on Linux (``mkosi.sandbox`` binds Linux libc symbols at import), so callers
import this module lazily, from code that runs on Linux.
"""

from __future__ import annotations

import argparse
import contextlib
import faulthandler
import functools
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

import mkosi.resources
from mkosi import run_verb
from mkosi.config import SETTINGS_LOOKUP_BY_NAME, parse_config
from mkosi.kmod import globs_match_module, normalize_module_glob, normalize_module_name
from mkosi.log import log_setup
from mkosi.run import fork_and_wait
from mkosi.sandbox import (
    CAP_SYS_ADMIN,
    CLONE_NEWNS,
    VarlinkError,
    acquire_privileges,
    have_effective_cap,
    unshare,
)
from mkosi.sandbox import __version__ as MKOSI_VERSION
from mkosi.util import resource_path

from tundravm.toolchain import MKOSI_PIN

PINNED_VERSION = MKOSI_PIN.version
"""The mkosi release tundravm is written against; every other version is refused."""


class MkosiVersionError(ImportError):
    """The installed mkosi is not :data:`PINNED_VERSION`."""


if MKOSI_VERSION != PINNED_VERSION:
    raise MkosiVersionError(
        f"tundravm needs mkosi {PINNED_VERSION}, but mkosi {MKOSI_VERSION} is installed. "
        f"Reinstall tundravm (`uv sync`, or `pip install` it again) to get mkosi "
        f"{PINNED_VERSION} from {MKOSI_PIN.repository}@{MKOSI_PIN.tag}."
    )


OutputSink = Callable[[str], None]
"""Receives each line mkosi and the commands it runs print, as it arrives."""

INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
"""What ``mkosi.__main__.main()`` turns into ``KeyboardInterrupt`` so cleanup runs."""

MULTI_THREADED_FORK = r"This process .* is multi-threaded, use of fork\(\) may lead to deadlocks"
"""Python 3.12's warning for the fork below; the copying thread holds no lock while it waits."""

READER_JOIN_TIMEOUT = 10.0
"""Seconds to wait for the copying thread once the child exited (a stray grandchild may keep
the pipe open)."""


@dataclass(frozen=True, slots=True)
class MkosiResult:
    """A verb that succeeded: its *argv*, what mkosi printed on stdout (``stdout=True``) and
    the *log* it wrote."""

    argv: tuple[str, ...]
    stdout: str
    log: Path


class MkosiError(Exception):
    """mkosi exited non-zero: *argv*, its exit status and everything it logged."""

    def __init__(self, argv: Sequence[str], log_text: str, returncode: int) -> None:
        self.argv = tuple(argv)
        self.log_text = log_text
        self.returncode = returncode
        tail = "\n".join(log_text.rstrip().splitlines()[-20:])
        super().__init__(
            f"mkosi {' '.join(self.argv)} failed (exit {returncode})"
            + (f":\n{tail}" if tail else "")
        )


@dataclass(frozen=True, slots=True)
class MkosiSummary:
    """``mkosi summary --json``: the resolved tools tree (``None`` without ``ToolsTree=``)
    and every image, subimages first and the main image last, as ``Config.to_dict()``."""

    tools: dict[str, Any] | None
    images: tuple[dict[str, Any], ...]

    @property
    def main(self) -> dict[str, Any]:
        return self.images[-1]


@dataclass(frozen=True, slots=True)
class MkosiSetting:
    """A setting mkosi knows: its canonical *name*, INI *section*, ``Config`` field (*dest*),
    the parser that reads its value, its scope and accepted *choices*."""

    name: str
    section: str
    dest: str
    parser: str
    scope: str
    choices: tuple[str, ...] = field(default=())
    tools: bool = False


# ── invoke ───────────────────────────────────────────────────────────────


def invoke(
    argv: Sequence[str],
    *,
    cwd: Path,
    log: Path,
    on_output: OutputSink | None = None,
    stdout: bool = False,
    terminal: bool = False,
) -> MkosiResult:
    """Run ``mkosi ARGV`` in *cwd*, in a child forked by mkosi's ``fork_and_wait``.

    Everything mkosi logs and the commands it runs print goes to *log* and, line by line, to
    *on_output* (default: this process's stderr). *stdout* captures what mkosi prints on
    stdout (``summary --json``, ``cat-config``) as :attr:`MkosiResult.stdout` instead.
    *terminal* hands the child this terminal (``vm``, ``shell``) and logs only mkosi's own
    records. Raises :class:`MkosiError` when mkosi fails and ``KeyboardInterrupt`` when this
    process was interrupted (the child got ``SIGINT`` and cleaned up first).
    """
    args = tuple(argv)
    log.parent.mkdir(parents=True, exist_ok=True)
    sink = on_output if on_output is not None else _stderr_line
    with contextlib.ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tundravm-mkosi-")))
        stdout_file = scratch / "stdout"
        interrupted = stack.enter_context(_forward_signals())
        if terminal:
            log.write_text("", encoding="utf-8")
            capture = None
        else:
            capture = stack.enter_context(_LogPipe(log, sink))
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", MULTI_THREADED_FORK, DeprecationWarning)
                fork_and_wait(
                    _child,
                    args,
                    cwd,
                    None if capture is None else capture.write_fd,
                    stdout_file if stdout else None,
                    log if terminal else None,
                )
        except subprocess.CalledProcessError as exc:
            if capture is not None:
                capture.finish()
            if interrupted():
                raise KeyboardInterrupt from exc
            raise MkosiError(args, _read(log), exc.returncode) from exc
        if capture is not None:
            capture.finish()
        captured = _read(stdout_file) if stdout else ""
    return MkosiResult(argv=args, stdout=captured, log=log)


def summary(
    tree_dir: Path,
    *,
    extra_argv: Sequence[str] = (),
    log: Path | None = None,
) -> MkosiSummary:
    """``mkosi --force summary --json`` of the tree in *tree_dir*, parsed; no privileges needed.

    ``--force`` keeps mkosi from replaying a previous build's history. *extra_argv* (e.g.
    ``--profile=…``) goes before the verb. *log* defaults to a temporary file.
    """
    argv = ["--directory", str(tree_dir), "--force", "--no-pager", *extra_argv, "summary", "--json"]
    with contextlib.ExitStack() as stack:
        if log is None:
            log = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "summary.log"
        result = invoke(argv, cwd=tree_dir, log=log, stdout=True, on_output=_discard)
    return parse_summary(result.stdout)


def parse_summary(text: str) -> MkosiSummary:
    """:class:`MkosiSummary` from ``summary --json`` output (``{"Tools": …, "Images": […]}``)."""
    payload = json.loads(text)
    tools = payload.get("Tools")
    return MkosiSummary(
        tools=dict(tools) if isinstance(tools, dict) else None,
        images=tuple(dict(image) for image in payload.get("Images", ())),
    )


def _child(
    argv: tuple[str, ...],
    cwd: Path,
    capture_fd: int | None,
    stdout_file: Path | None,
    log: Path | None,
) -> None:
    """The forked child: ``mkosi.__main__.main()`` without its exit, output redirected."""
    for signum in INTERRUPT_SIGNALS:
        signal.signal(signum, _onsignal)
    if capture_fd is not None:
        os.dup2(capture_fd, 1)
        os.dup2(capture_fd, 2)
        os.close(capture_fd)
    if stdout_file is not None:
        fd = os.open(stdout_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(fd, 1)
        os.close(fd)
    if capture_fd is not None or stdout_file is not None:
        # sys.stdout/sys.stderr may be objects of the embedding program (pytest's capture,
        # a GUI): mkosi's prints and log records must reach the descriptors redirected above.
        sys.stdout = open(1, "w", encoding="utf-8", errors="replace", closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
        # mkosi hands ``sys.stdin`` to every script it runs (``stdin=sys.stdin``); pytest's
        # capture object has no ``fileno()``, and a redirected build has no terminal anyway.
        sys.stdin = open(os.devnull, encoding="utf-8")
    os.chdir(cwd)
    logging.getLogger().handlers.clear()  # the embedding program's handlers are not mkosi's
    log_setup()
    if log is not None:
        handler = logging.FileHandler(log, encoding="utf-8")
        handler.setFormatter(logging.Formatter("‣ %(message)s"))
        logging.getLogger().addHandler(handler)
    try:
        with resource_path(mkosi.resources) as resources:
            args, tools, images = parse_config(argv, resources=resources)
            if args.debug:
                faulthandler.enable()
            run_verb(args, tools, images, resources=resources)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()


_INTERRUPTED = False


def _onsignal(signum: int, frame: FrameType | None) -> None:
    """``mkosi.__main__.onsignal``: the first signal becomes ``KeyboardInterrupt``."""
    global _INTERRUPTED
    if _INTERRUPTED:
        return
    _INTERRUPTED = True
    raise KeyboardInterrupt()


@contextlib.contextmanager
def _forward_signals() -> Iterator[Callable[[], bool]]:
    """While mkosi runs, make ``SIGTERM``/``SIGHUP`` interrupt this process like ``SIGINT``,
    so ``fork_and_wait`` stops the child and waits for its cleanup. Yields whether one came."""
    seen: list[int] = []

    def handler(signum: int, frame: FrameType | None) -> None:
        seen.append(signum)
        raise KeyboardInterrupt()

    if threading.current_thread() is not threading.main_thread():
        yield lambda: False
        return
    previous = {signum: signal.signal(signum, handler) for signum in INTERRUPT_SIGNALS}
    try:
        yield lambda: bool(seen)
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


class _LogPipe:
    """A pipe the child writes to; a thread copies each line to the log and the sink."""

    def __init__(self, log: Path, sink: OutputSink) -> None:
        self.log = log
        self.sink = sink
        self.read_fd, self.write_fd = os.pipe()
        self.thread = threading.Thread(target=self._copy, name="mkosi-log", daemon=True)

    def __enter__(self) -> _LogPipe:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.finish()

    def finish(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1
        self.thread.join(READER_JOIN_TIMEOUT)

    def _copy(self) -> None:
        with (
            open(self.read_fd, "rb", buffering=0, closefd=True) as source,
            open(self.log, "w", encoding="utf-8") as target,
        ):
            pending = b""
            while chunk := source.read(65536):
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    self._emit(raw, target)
            if pending:
                self._emit(pending, target)

    def _emit(self, raw: bytes, target: Any) -> None:
        line = raw.decode(errors="replace").rstrip("\r")
        target.write(line + "\n")
        target.flush()
        with contextlib.suppress(Exception):
            self.sink(line)


def _stderr_line(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _discard(line: str) -> None:
    del line


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ── privilege ────────────────────────────────────────────────────────────


@functools.cache
def needs_sudo() -> bool:
    """Whether mkosi must run under ``sudo`` here: not root, and no user namespace to be had.

    Decided as ``run_verb`` acquires privileges: a process with ``CAP_SYS_ADMIN`` needs
    nothing; else a forked probe asks systemd-nsresourced for a user namespace, falls back to
    ``unshare(CLONE_NEWUSER)`` with a uid map (``acquire_privileges()``), then unshares a
    mount namespace. A kernel, container or LSM policy that forbids any step (Ubuntu's
    ``kernel.apparmor_restrict_unprivileged_userns=1`` fails the uid map) means sudo.
    """
    if have_effective_cap(CAP_SYS_ADMIN):
        return False
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return not fork_and_wait(_probe_userns)
    except subprocess.CalledProcessError:
        return True


def _probe_userns() -> bool:
    try:
        try:
            if os.getenv("MKOSI_FORCE_USERNS_FALLBACK"):
                raise ConnectionRefusedError("forced fallback")
            acquire_privileges(foreign=True)
        except (FileNotFoundError, VarlinkError, ConnectionRefusedError):
            acquire_privileges()
        unshare(CLONE_NEWNS)
    except Exception:
        return False
    return True


def sudo_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    log: Path,
    result: Path,
    stdout: bool = False,
    python: str | None = None,
) -> list[str]:
    """The command that runs :func:`invoke` as root: ``sudo -E <python> -m tundravm._mkosi``.

    ``-E`` keeps the environment (proxies, ``SOURCE_DATE_EPOCH``); *python* is this
    interpreter, whose environment has tundravm and the pinned mkosi.
    """
    command = [
        "sudo",
        "-E",
        python or sys.executable,
        "-m",
        "tundravm._mkosi",
        "--cwd",
        str(cwd),
        "--log",
        str(log),
        "--result",
        str(result),
    ]
    if stdout:
        command.append("--stdout")
    return [*command, "--", *argv]


@dataclass(frozen=True, slots=True)
class SudoOutcome:
    """What ``python -m tundravm._mkosi`` wrote to ``--result``."""

    returncode: int
    stdout: str


def read_result(path: Path) -> SudoOutcome | None:
    """The outcome a ``python -m tundravm._mkosi`` run wrote to *path*; ``None`` if it wrote
    none (sudo refused, or the interpreter failed before running mkosi)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("returncode"), int):
        return None
    return SudoOutcome(int(payload["returncode"]), str(payload.get("stdout", "")))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m tundravm._mkosi --cwd DIR --log FILE --result FILE-or-FD [--stdout] -- ARGV``.

    Runs :func:`invoke` and writes ``{"returncode", "stdout"}`` to ``--result`` (a path, or
    an inherited file descriptor number; sudo closes inherited descriptors unless its
    ``closefrom_override`` allows ``-C``, so the backend passes a path). Files it creates
    are handed back to ``SUDO_UID``/``SUDO_GID``. Exits with mkosi's status.
    """
    parser = argparse.ArgumentParser(prog="python -m tundravm._mkosi")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("mkosi", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    mkosi_argv = list(options.mkosi)
    if mkosi_argv[:1] == ["--"]:
        mkosi_argv = mkosi_argv[1:]
    try:
        result = invoke(mkosi_argv, cwd=options.cwd, log=options.log, stdout=options.stdout)
        outcome: dict[str, object] = {"returncode": 0, "stdout": result.stdout}
    except MkosiError as exc:
        outcome = {"returncode": exc.returncode or 1, "stdout": ""}
    except KeyboardInterrupt:
        outcome = {"returncode": 130, "stdout": ""}
    _write_result(options.result, outcome)
    _give_back(options.log)
    return int(str(outcome["returncode"]))


def _write_result(target: str, outcome: dict[str, object]) -> None:
    text = json.dumps(outcome)
    if target.isdigit():
        with open(int(target), "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    path = Path(target)
    path.write_text(text, encoding="utf-8")
    _give_back(path)


def _give_back(path: Path) -> None:
    """chown *path* to the user ``sudo`` ran for, if it did."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.getuid() != 0 or not uid or not gid:
        return
    with contextlib.suppress(OSError):
        os.chown(path, int(uid), int(gid))


# ── read-only helpers ────────────────────────────────────────────────────


def setting(name: str) -> MkosiSetting | None:
    """The mkosi setting called *name* (or one of its compatibility names); ``None`` if mkosi
    has none (what lint says about a ``Mkosi(settings=)`` key)."""
    found = SETTINGS_LOOKUP_BY_NAME.get(name)
    if found is None:
        return None
    return MkosiSetting(
        name=found.name,
        section=found.section,
        dest=found.dest,
        parser=getattr(found.parse, "__name__", repr(found.parse)),
        scope=str(found.scope),
        choices=tuple(found.choices or ()),
        tools=found.tools,
    )


def kernel_module_globs_match(module: str, globs: Sequence[str]) -> bool:
    """Whether mkosi's ``KernelModules=`` *globs* keep *module*, a path relative to
    ``/usr/lib/modules/<kver>/`` (``kernel/drivers/virtio/virtio_net.ko.zst``).

    The same normalisation (``_`` → ``-``) and last-match-wins rules mkosi applies;
    ``re:`` patterns are not globs and are left to mkosi.
    """
    normalized = [normalize_module_glob(glob) for glob in globs if not glob.startswith("re:")]
    return globs_match_module(normalize_module_name(module), normalized)


if __name__ == "__main__":
    sys.exit(main())
