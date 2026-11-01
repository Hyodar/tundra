"""A stand-in for :mod:`tundravm._mkosi` at the process boundary.

:class:`FakeMkosi` replaces ``invoke`` and ``needs_sudo`` on the adapter module, so a real
backend runs its whole command construction, summary handling and artifact collection while
nothing forks and nothing needs privileges: each call is recorded, ``summary --json``
returns a canned payload and ``build`` writes small stand-ins into the
``--output-directory`` it was given: a PE UKI (:func:`fake_uki`) the measurement replays,
GPT disks (:func:`fake_disk`) and the GCP tarball around one, so a fake build measures,
reports and diffs as a real one does.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
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
    log. *uki_size* pads the ``payload.efi`` a build writes to that many bytes (sparse), and
    a failing build writes it before it fails, as mkosi's payload subimage is built before
    the disk around it. *sudo* is what ``needs_sudo()`` answers.
    """

    summary: dict[str, Any] | None = None
    outputs: Sequence[str] = DEFAULT_OUTPUTS
    fail: int | None = None
    fail_verb: str = "build"
    log: str = ""
    uki_size: int | None = None
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
            if call.verb == "build" and self.uki_size is not None:
                self._build(call, only=DEFAULT_OUTPUTS[:1])
            raise MkosiError(argv, log.read_text(encoding="utf-8"), self.fail)
        printed = ""
        if call.verb == "summary":
            printed = json.dumps(self.summary if self.summary is not None else _summary(call))
        elif call.verb == "build":
            self._build(call)
            self._cache(call)
        return MkosiResult(argv=tuple(argv), stdout=printed, log=log)

    def needs_sudo(self) -> bool:
        return self.sudo

    def _build(self, call: FakeCall, only: Sequence[str] | None = None) -> None:
        output = call.option("--output-directory")
        if output is None:
            return
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        wrappers = [
            WRAPPER_OUTPUTS[t] for t in call.options("--dependency") if t in WRAPPER_OUTPUTS
        ]
        for name in only or (*self.outputs, *wrappers):
            (directory / name).write_bytes(_output(name, call.options("--profile")))
            if name.endswith(".efi") and self.uki_size is not None:
                os.truncate(directory / name, self.uki_size)

    def _cache(self, call: FakeCall) -> None:
        """What a build with ``--incremental=yes`` leaves in its ``--cache-directory``: per
        image of the summary a ``<key>.cache`` tree and a ``<key>.manifest``, written on the
        first build under a key (``--cache-key``, its ``&d``, ``&r``, ``&a`` and ``&I``
        expanded) and left alone after, as mkosi reuses what the manifest records."""
        cache = call.option("--cache-directory")
        if call.option("--incremental") != "yes" or cache is None:
            return
        key = call.option("--cache-key") or "&d~&r~&a~&I"
        summary = self.summary if self.summary is not None else _summary(call)
        for image in summary.get("Images", ()):
            name = (
                key.replace("&d", str(image.get("Distribution") or ""))
                .replace("&r", str(image.get("Release") or ""))
                .replace("&a", str(image.get("Architecture") or ""))
                .replace("&I", str(image.get("Image") or "main"))
            )
            manifest = Path(cache) / f"{name}.manifest"
            if manifest.is_file():
                continue
            (Path(cache) / f"{name}.cache").mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"packages": image.get("Packages", [])}, indent=4))

    def install(self, setattr: Callable[[object, str, object], None]) -> FakeMkosi:
        """Put this fake in place of the adapter's ``invoke`` and ``needs_sudo`` using
        *setattr* (pytest's ``monkeypatch.setattr``, which undoes it after the test)."""
        from tundravm import _mkosi

        setattr(_mkosi, "invoke", self.invoke)
        setattr(_mkosi, "needs_sudo", self.needs_sudo)
        return self


_ALIGN = 0x200
ESP_TYPE = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
"""The ESP's partition type GUID, as a GPT entry stores it."""


def _aligned(size: int) -> int:
    return (size + _ALIGN - 1) // _ALIGN * _ALIGN


def fake_pe(sections: Sequence[tuple[str, bytes]]) -> bytes:
    """A minimal valid PE32+ image holding *sections* (name, data), in file order."""
    count = len(sections)
    size_of_headers = _aligned(0x40 + 24 + 240 + 40 * count)
    offset = size_of_headers
    table = b""
    body = b""
    for name, data in sections:
        raw = _aligned(len(data))
        table += struct.pack(
            "<8sIIIIIIHHI", name.encode(), len(data), offset, raw, offset, 0, 0, 0, 0, 0x40000040
        )
        body += data.ljust(raw, b"\0")
        offset += raw
    dos = b"MZ".ljust(0x3C, b"\0") + struct.pack("<I", 0x40)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664, count, 0, 0, 0, 240, 0x22)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<I", optional, 60, size_of_headers)
    struct.pack_into("<I", optional, 108, 16)
    head = (dos + coff + bytes(optional) + table).ljust(size_of_headers, b"\0")
    return head + body


def fake_uki(cmdline: bytes = b"console=ttyS0", stub: int = 257) -> bytes:
    """A UKI: stub text, ``.sdmagic``, ``.osrel``, ``.cmdline``, ``.initrd``, ``.linux`` (a PE)."""
    linux = fake_pe([(".text", b"kernel")])
    return fake_pe(
        [
            (".text", b"stub"),
            (".sdmagic", f"#### LoaderInfo: systemd-stub {stub} ####".encode()),
            (".osrel", b"ID=test\n"),
            (".cmdline", cmdline),
            (".initrd", b"initrd"),
            (".linux", linux),
        ]
    )


def fake_disk(disk_guid: bytes = b"\x12" * 16) -> bytes:
    """The first 34 sectors of a GPT disk with one ESP entry."""
    head = bytearray(34 * 512)
    header = bytearray(92)
    header[:8] = b"EFI PART"
    header[56:72] = disk_guid
    struct.pack_into("<II", header, 80, 128, 128)
    head[512 : 512 + 92] = header
    head[1024 : 1024 + 16] = ESP_TYPE
    struct.pack_into("<QQ", head, 1024 + 32, 2048, 4095)
    return bytes(head)


def _output(name: str, profiles: Sequence[str]) -> bytes:
    """What a fake build of *profiles* writes as *name*: a UKI whose command line names them,
    a disk with a line naming them after its partition table, the GCP tarball around one,
    and that line alone for anything else."""
    line = f"fake mkosi build: {' '.join(profiles)} output={name}\n".encode()
    if name.endswith(".efi"):
        return fake_uki(f"console=ttyS0 fake.profiles={','.join(profiles)}".encode())
    if name.endswith((".raw", ".vhd")):
        return fake_disk() + line
    if name.endswith(".tar.gz"):
        disk = fake_disk() + line
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
            member = tarfile.TarInfo("disk.raw")
            member.size = len(disk)
            archive.addfile(member, io.BytesIO(disk))
        return gzip.compress(buffer.getvalue(), mtime=0)
    return line


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
            "Architecture": "x86-64",
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
