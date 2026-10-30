"""A stand-in for :mod:`tundravm._mkosi` at the process boundary.

:class:`FakeMkosi` replaces ``invoke`` and ``needs_sudo`` on the adapter module, so a real
backend runs its whole command construction, summary handling and artifact collection while
nothing forks and nothing needs privileges: each call is recorded, ``summary --json``
returns a canned payload and ``build`` writes placeholder outputs into the
``--output-directory`` it was given.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tundravm._mkosi import MkosiResult

DEFAULT_OUTPUTS = ("payload.efi", "qemu.raw")
"""What a fake ``build`` writes for the tree's default selection: the payload's UKI and the
QEMU disk; each ``--dependency=azure``/``gcp`` adds ``azure.vhd``/``gcp.tar.gz``."""
WRAPPER_OUTPUTS = {"azure": "azure.vhd", "gcp": "gcp.tar.gz"}
"""What each cloud wrapper subimage a ``--dependency=`` selects leaves in the output."""

DEFAULT_TOOLS = {"Distribution": "debian", "Release": "trixie", "Snapshot": "20251020T000000Z"}
"""The tools tree a fake ``summary --json`` resolves."""


@dataclass(frozen=True, slots=True)
class FakeCall:
    """One recorded ``invoke``: mkosi's *argv*, the directory it ran in and its log."""

    argv: tuple[str, ...]
    cwd: Path
    log: Path
    stdout: bool

    @property
    def verb(self) -> str:
        """The mkosi verb (``build``, ``summary``…): the last non-option before ``--json``."""
        words = [arg for arg in self.argv if not arg.startswith("-")]
        return words[-1] if words else ""

    def options(self, name: str) -> list[str]:
        """Every ``--name=value`` value in :attr:`argv`, in order."""
        return [arg.split("=", 1)[1] for arg in self.argv if arg.startswith(f"{name}=")]

    def option(self, name: str) -> str | None:
        """The value of the last ``--name=value`` in :attr:`argv`."""
        found = None
        for arg in self.argv:
            if arg.startswith(f"{name}="):
                found = arg.split("=", 1)[1]
        return found


@dataclass(slots=True)
class FakeMkosi:
    """Records mkosi verbs instead of running them.

    *summary* is the ``summary --json`` payload (``{"Tools": …, "Images": […]}``; by default
    :data:`DEFAULT_TOOLS` and the images the call selects). *outputs* are the file names a
    ``build`` writes besides the selected wrappers' (:data:`WRAPPER_OUTPUTS`). *fail* makes
    the verb named *fail_verb* raise ``MkosiError`` with that exit status and *log* as its
    log. *sudo* is what ``needs_sudo()`` answers.
    """

    summary: dict[str, Any] | None = None
    outputs: Sequence[str] = DEFAULT_OUTPUTS
    fail: int | None = None
    fail_verb: str = "build"
    log: str = ""
    sudo: bool = False
    calls: list[FakeCall] = field(default_factory=list)

    def verbs(self) -> list[str]:
        return [call.verb for call in self.calls]

    def invoke(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        log: Path,
        on_output: Callable[[str], None] | None = None,
        stdout: bool = False,
        terminal: bool = False,
    ) -> MkosiResult:
        from tundravm._mkosi import MkosiError, MkosiResult

        call = FakeCall(tuple(argv), Path(cwd), Path(log), stdout)
        self.calls.append(call)
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"fake mkosi {call.verb}", *self.log.splitlines()]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if on_output is not None:
            for line in lines:
                on_output(line)
        if self.fail is not None and call.verb == self.fail_verb:
            raise MkosiError(argv, log.read_text(encoding="utf-8"), self.fail)
        printed = ""
        if call.verb == "summary":
            printed = json.dumps(self.summary if self.summary is not None else _summary(call))
        elif call.verb == "build":
            self._build(call)
        return MkosiResult(argv=tuple(argv), stdout=printed, log=log)

    def needs_sudo(self) -> bool:
        return self.sudo

    def _build(self, call: FakeCall) -> None:
        output = call.option("--output-directory")
        if output is None:
            return
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        wrappers = [
            WRAPPER_OUTPUTS[t] for t in call.options("--dependency") if t in WRAPPER_OUTPUTS
        ]
        for name in (*self.outputs, *wrappers):
            (directory / name).write_text(
                f"fake mkosi build: {' '.join(call.options('--profile'))} output={name}\n",
                encoding="utf-8",
            )

    def install(self, setattr: Callable[[object, str, object], None]) -> FakeMkosi:
        """Put this fake in place of the adapter's ``invoke`` and ``needs_sudo`` using
        *setattr* (pytest's ``monkeypatch.setattr``, which undoes it after the test)."""
        from tundravm import _mkosi

        setattr(_mkosi, "invoke", self.invoke)
        setattr(_mkosi, "needs_sudo", self.needs_sudo)
        return self


def _summary(call: FakeCall) -> dict[str, Any]:
    """The images the tree resolves for *call*'s selection: the payload, the wrappers its
    ``--dependency=`` options name, then the main image (the QEMU disk)."""
    profiles = call.options("--profile")
    output = call.option("--output-directory")

    def image(name: str, fmt: str, output_name: str, deps: list[str]) -> dict[str, Any]:
        return {
            "Image": name,
            "Format": fmt,
            "Output": output_name,
            "Dependencies": deps,
            "Profiles": profiles,
            "Distribution": "debian",
            "Release": "trixie",
            "OutputDirectory": output,
        }

    wrappers = [t for t in call.options("--dependency") if t in WRAPPER_OUTPUTS]
    return {
        "Tools": dict(DEFAULT_TOOLS),
        "Images": [
            image("payload", "uki", "payload", []),
            *(image(t, "disk", t, ["payload"]) for t in wrappers),
            image("main", "disk", "qemu", ["payload", *wrappers]),
        ],
    }


def _sudo_works() -> bool:
    """Whether ``sudo`` runs without a password (``-n`` refuses some password-less setups)."""
    if shutil.which("sudo") is None:
        return False
    try:
        done = subprocess.run(
            ["sudo", "true"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def build_unavailable() -> str | None:
    """Why this host cannot run a real build with the Linux backend without a password
    prompt, or ``None`` when it can (x86-64 Linux, the pinned mkosi, and user namespaces or
    non-interactive sudo). Integration tests skip on the reason."""
    if not sys.platform.startswith("linux") or platform.machine() != "x86_64":
        return f"the Linux backend builds on x86-64 Linux, not {sys.platform}/{platform.machine()}"
    try:
        from tundravm import _mkosi
    except ImportError as exc:
        return str(exc)
    if not _mkosi.needs_sudo() or _sudo_works():
        return None
    return "needs unprivileged user namespaces or non-interactive sudo"
