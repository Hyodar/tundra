"""Boot a built image under QEMU and read its serial console: :func:`boot`.

The image is booted the way ``run --mode simulated`` boots it: OVMF from
pflash and either the UKI through ``-kernel`` or the disk (``qemu.raw``) through ``-drive``
on a throwaway snapshot, which OVMF boots from the UKI on its ESP
(``EFI/BOOT/BOOTX64.EFI``). Nothing is added to the image: the console settings the harness
needs reach the kernel through SMBIOS (``io.systemd.stub.kernel-cmdline-extra``, the
way ``mkosi vm`` passes them), which systemd-stub appends to the UKI's own command line
when Secure Boot is off; :func:`expected_cmdline` is the line the kernel should then print.

The serial console is one byte stream that the kernel, systemd and the journal share. The
kernel writes its lines straight to the UART, past the tty that userspace's whole-line
writes go through, so a kernel line can land inside a journal line: ``... starts no servi``,
then a whole ``[    2.197534] virtio_blk ...`` line, then ``ces``. A marker
(:func:`on_console`) is therefore looked for in the transcript without its ANSI escapes and
carriage returns, and in that text de-interleaved (:func:`deinterleave`): every run of whole
kernel lines (a ``[ seconds.micros]`` stamp up to its newline) that starts in the middle of
another line is cut out, which puts the interrupted line back together, while the kernel
line itself is whole in the transcript. A marker still has to appear in full in one of them.
What reads the console line by line (:func:`scan_console`, a unit's lines in a summary)
reads :func:`console_lines`: the mended lines, each followed by the kernel lines cut out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from tundravm.deploy.qemu import OVMF_CODE_PATHS, OVMF_VARS_PATHS
from tundravm.measure import pe

QEMU = "qemu-system-x86_64"
BOOT_TARGET = "minimal.target"
"""The target every tundravm image boots into (``default.target`` links to it)."""
DEFAULT_MARKERS = (f"Reached target {BOOT_TARGET}",)
CONSOLE_CMDLINE = (
    "console=ttyS0",
    "systemd.show_status=yes",
    "systemd.status_unit_format=name",
    "systemd.log_color=no",
    "systemd.journald.forward_to_console=1",
    "panic=-1",
)
"""Appended to the image's command line: kernel and systemd status lines on the serial
console, unit names in them, the journal too (a failed unit's own output; the command line
overrides ``ForwardToConsole=no`` in ``journald.conf.d``, see ``journald.conf(5)``), and a
panic that ends the VM (QEMU runs ``-no-reboot``)."""

FATAL = (
    "Kernel panic - not syncing",
    "You are in emergency mode",
    "You are in rescue mode",
    "Reached target emergency.target",
    "Reached target rescue.target",
    "Failed to execute /init",
    "No working init found",
    "Freezing execution.",
    "UEFI Interactive Shell",
)
"""Console text that ends the boot at once: a panic, emergency mode, a dead PID 1, or OVMF
falling through to its shell (no boot option loaded: a disk whose ESP it cannot read)."""

_ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]|\r")
_UNIT = r"(\S+?\.(?:service|socket|target|mount|timer|path|swap|automount|slice|scope|device))"
_END = r"(?=[\s.]|$)"
_STARTED = re.compile(rf"\b(?:Started|Finished|Reached target|Listening on|Mounted) {_UNIT}{_END}")
_FAILED = re.compile(
    rf"\b(?:Failed to start|Failed to mount|Failed to listen on|Dependency failed for) "
    rf"{_UNIT}{_END}|(?<!\S){_UNIT}: Failed with result "
)
_FINISHED = re.compile(rf"\bFinished {_UNIT}{_END}")
_STOPPED = re.compile(rf"\bStopped {_UNIT}{_END}|(?<!\S){_UNIT}: Deactivated successfully")
_SOURCE = re.compile(r"^\[\s*[0-9.]+\]\s+([^\s\[:]+)(?:\[[0-9]+\])?:\s")
"""The identifier of a journal line forwarded to the console (``nft[155]: ...``)."""
_KERNEL_CMDLINE = re.compile(r"^\[\s*[0-9.]+\]\s+Kernel command line: (.*?)\s*$", re.MULTILINE)
"""The command line the kernel booted with, as it prints it (the text of ``/proc/cmdline``)."""
_INTERRUPTION = re.compile(r"(?<=[^\n])(?:\[ *[0-9]+\.[0-9]+\] [^\n]*\n)+")
"""Whole kernel lines (``[    2.197534] ...`` up to the newline) written into another line."""
_CUT = "\ue000"
"""Where :func:`console_lines` cut kernel lines out (a private-use character)."""


def clean_console(raw: str) -> str:
    """*raw* serial output without ANSI escapes and carriage returns."""
    return _ANSI.sub("", raw)


def deinterleave(text: str) -> str:
    """Console *text*, cleaned, with each run of kernel lines that interrupts another line
    cut out, which makes the interrupted line whole again."""
    return _INTERRUPTION.sub("", clean_console(text))


def console_lines(text: str) -> list[str]:
    """Console *text*, cleaned, line by line as the writers wrote them: each line kernel
    lines interrupted made whole again (:func:`deinterleave`), then those kernel lines."""
    runs: list[str] = []

    def cut(match: re.Match[str]) -> str:
        runs.append(match[0])
        return _CUT

    found: list[str] = []
    pending = iter(runs)
    for line in _INTERRUPTION.sub(cut, clean_console(text).replace(_CUT, "")).splitlines():
        found.append(line.replace(_CUT, ""))
        for _ in range(line.count(_CUT)):
            found += next(pending).splitlines()
    return found


def on_console(text: str, marker: str) -> bool:
    """Whether *marker* is on console *text*: in it cleaned or in it de-interleaved."""
    return marker in clean_console(text) or marker in deinterleave(text)


def kernel_cmdline(text: str) -> str | None:
    """The kernel's ``Kernel command line:`` in console *text* (the last one, should the
    console hold two boots); ``None`` when the kernel printed none."""
    found = _KERNEL_CMDLINE.findall(clean_console(text))
    return found[-1] if found else None


def uki_cmdline(uki: str | os.PathLike[str]) -> str:
    """The command line *uki* carries in its ``.cmdline`` section (``""`` without one), as
    systemd-stub reads it: up to the first NUL, surrounding whitespace dropped."""
    section = pe.parse(Path(uki).read_bytes()).section(".cmdline") or b""
    return section.split(b"\0", 1)[0].decode("utf-8").strip()


def expected_cmdline(uki: str | os.PathLike[str], cmdline: Sequence[str] = CONSOLE_CMDLINE) -> str:
    """The command line a boot of *uki* with *cmdline* through SMBIOS gives the kernel:
    systemd-stub appends the SMBIOS words to the UKI's own line, after one space."""
    return " ".join(part for part in (uki_cmdline(uki), " ".join(cmdline)) if part)


@dataclass(frozen=True, slots=True)
class ConsoleScan:
    """What a console transcript says: units started and failed, fatal lines.

    *failed* holds every unit that failed to start or failed later (``Failed with result``),
    *exited* the units whose last state is stopped after ``Started`` (a ``Finished`` oneshot
    that deactivates is done, not exited).
    """

    started: tuple[str, ...]
    failed: tuple[str, ...]
    fatal: tuple[str, ...]
    exited: tuple[str, ...] = ()


def scan_console(text: str) -> ConsoleScan:
    """Read systemd's status lines and the kernel's fatal messages out of *text*."""
    started: dict[str, None] = {}
    failed: dict[str, None] = {}
    state: dict[str, str] = {}
    fatal: list[str] = []
    for line in console_lines(text):
        for match in _STARTED.finditer(line):
            started.setdefault(match[1])
            if state.get(match[1]) != "done":
                state[match[1]] = "running"
        for match in _FINISHED.finditer(line):
            state[match[1]] = "done"
        for match in _STOPPED.finditer(line):
            unit = match[1] or match[2]
            if state.get(unit) == "running":
                state[unit] = "exited"
        for match in _FAILED.finditer(line):
            unit = match[1] or match[2]
            failed.setdefault(unit)
            state[unit] = "failed"
        if any(marker in line for marker in FATAL):
            fatal.append(line.strip())
    exited = tuple(unit for unit, last in state.items() if last == "exited")
    return ConsoleScan(tuple(started), tuple(failed), tuple(fatal), exited)


def unit_lines(text: str, unit: str, limit: int = 20) -> list[str]:
    """The last *limit* console lines about *unit*: those naming it and the forwarded journal
    lines of a process named like it (``nft[155]:`` for ``nftables.service``)."""
    stem = unit.rsplit(".", 1)[0]
    found = []
    for line in console_lines(text):
        source = _SOURCE.match(line)
        name = source[1] if source else ""
        related = len(name) >= 3 and name not in ("systemd", "kernel")
        related = related and (stem.startswith(name) or name.startswith(stem))
        if related or re.search(rf"(?<![\w@.-]){re.escape(unit)}(?![\w@-])", line):
            found.append(line.strip())
    return found[-limit:]


def unit_name(name: str) -> str:
    """*name* with ``.service`` added when it has no unit suffix."""
    return name if re.fullmatch(_UNIT, name) else f"{name}.service"


def missing_from(
    text: str, scan: ConsoleScan, markers: Sequence[str], units: Sequence[str]
) -> tuple[str, ...]:
    """The *markers* not on console *text* (:func:`on_console`) and the *units* not started,
    in order."""
    absent = [marker for marker in markers if not on_console(text, marker)]
    absent += [unit for unit in map(unit_name, units) if unit not in scan.started]
    return tuple(absent)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What one ``wait_http`` URL answered while the VM ran: ``status`` ``None`` when it never
    answered with a 2xx before the boot ended (``body`` then holds the last error)."""

    url: str
    status: int | None
    body: str

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _fetch(url: str, timeout: float = 3.0) -> HttpResponse:
    """GET *url* once; an error or a non-2xx answer is an ``HttpResponse`` with no status."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:  # noqa: S310 - test harness
            return HttpResponse(url, reply.status, reply.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(url, None, f"HTTP {exc.code}")
    except (OSError, ValueError) as exc:
        return HttpResponse(url, None, str(exc))


@dataclass(frozen=True, slots=True)
class BootResult:
    """One boot: the console, what it reached, what failed, how long it took.

    ``missing`` lists the markers not seen and the expected units not started, ``exited``
    the expected units that started and then stopped; ``returncode`` is QEMU's own exit
    status, ``None`` when the harness stopped it.
    """

    console: str
    log: Path
    duration: float
    command: tuple[str, ...]
    started: tuple[str, ...]
    failed: tuple[str, ...]
    fatal: tuple[str, ...]
    missing: tuple[str, ...]
    timed_out: bool
    returncode: int | None
    stderr: str = ""
    exited: tuple[str, ...] = ()
    http: tuple[HttpResponse, ...] = ()
    """The ``wait_http`` URLs' answers, in order; one that never answered is in ``missing``."""

    @property
    def kernel_cmdline(self) -> str | None:
        """The command line the kernel printed (:func:`kernel_cmdline`)."""
        return kernel_cmdline(self.console)

    def http_get(self, url: str) -> str:
        """The body *url* (a ``wait_http`` URL) answered with; ``AssertionError`` when it
        never answered with a 2xx."""
        reply = next((r for r in self.http if r.url == url), None)
        if reply is None or not reply.ok:
            why = "was not requested" if reply is None else f"did not answer: {reply.body}"
            raise AssertionError(f"{url} {why}\n{self.summary()}")
        return reply.body

    @property
    def ok(self) -> bool:
        """Every marker and expected unit seen and still running, no unit failed, nothing
        fatal, in time, and QEMU did not exit with an error."""
        qemu_failed = self.returncode not in (None, 0)
        problems = (self.missing, self.failed, self.exited, self.fatal)
        return not (any(problems) or self.timed_out or qemu_failed)

    def summary(self, tail: int = 60) -> str:
        """A failure report: what went wrong, QEMU's stderr, the console's last lines."""
        lines = [f"boot {'ok' if self.ok else 'failed'} after {self.duration:.1f}s ({self.log})"]
        lines += [f"  missing: {', '.join(self.missing)}"] if self.missing else []
        lines += [f"  failed units: {', '.join(self.failed)}"] if self.failed else []
        lines += [f"  exited units: {', '.join(self.exited)}"] if self.exited else []
        lines += [f"  fatal: {line}" for line in self.fatal]
        lines += ["  timed out"] if self.timed_out else []
        if self.returncode is not None:
            lines.append(f"  qemu exited {self.returncode} before the boot finished")
        for unit in (*self.failed, *self.exited):
            lines += [f"--- {unit} ---", *unit_lines(self.console, unit)]
        if self.stderr.strip():
            lines += ["--- qemu stderr ---", self.stderr.strip()]
        lines += [f"--- console (last {tail} lines) ---", *self.console.splitlines()[-tail:]]
        return "\n".join(lines)

    def assert_booted(self) -> None:
        """Raise ``AssertionError`` with :meth:`summary` unless the boot is :attr:`ok`."""
        if not self.ok:
            raise AssertionError(self.summary())


class BootUnavailable(RuntimeError):
    """QEMU or OVMF is missing on this host: the boot cannot run here."""


class _HasPath(Protocol):
    @property
    def path(self) -> Path: ...


Bootable = str | os.PathLike[str] | _HasPath
"""A UKI (``.efi``) or disk image path, the directory holding one, or an
:class:`~tundravm.declarative.lifecycle.Artifact` (``path`` the disk, ``boot_payload`` its UKI)."""

BootPath = Literal["auto", "kernel", "disk"]
"""How :func:`boot` starts *artifact*: ``"kernel"`` passes the UKI to ``-kernel``, ``"disk"``
attaches the disk image and lets OVMF boot its ESP, ``"auto"`` goes by the path's suffix
(an artifact's ``path``, a directory's single ``.efi``)."""


def find_ovmf(firmware: str | os.PathLike[str] | None = None) -> tuple[Path, Path | None]:
    """The OVMF code image and its variable store (``None``: boot it with ``-bios``).

    *firmware* names the code image; its store is the sibling with ``CODE`` read as
    ``VARS``, when there is one. ``None`` searches the paths the QEMU deploy adapter does.
    """
    if firmware is not None:
        code = Path(firmware)
        if not code.is_file():
            raise FileNotFoundError(f"OVMF firmware not found: {code}")
        store = code.with_name(code.name.replace("CODE", "VARS"))
        return code, store if store != code and store.is_file() else None
    for code_path, vars_path in zip(OVMF_CODE_PATHS, OVMF_VARS_PATHS, strict=True):
        if Path(code_path).is_file() and Path(vars_path).is_file():
            return Path(code_path), Path(vars_path)
    raise BootUnavailable(f"OVMF firmware not found (searched {', '.join(OVMF_CODE_PATHS)})")


def boot_unavailable(
    qemu: str = QEMU, firmware: str | os.PathLike[str] | None = None
) -> str | None:
    """Why :func:`boot` cannot run on this host (no QEMU, no OVMF); ``None`` when it can."""
    if shutil.which(qemu) is None:
        return f"{qemu} is not installed"
    try:
        find_ovmf(firmware)
    except (BootUnavailable, FileNotFoundError) as exc:
        return str(exc)
    return None


_DISK_SUFFIXES = (".raw", ".qcow2", ".vhd", ".img")


def _image_path(artifact: Bootable, via: BootPath = "auto") -> Path:
    """The file :func:`boot` starts: *artifact*'s UKI for ``"kernel"`` (an artifact's
    ``boot_payload``), its disk for ``"disk"``, by suffix for ``"auto"``."""
    payload = getattr(artifact, "boot_payload", None)
    if via == "kernel" and payload is not None:
        path = Path(payload)
    else:
        path = Path(artifact.path if hasattr(artifact, "path") else artifact)
    if path.is_dir():
        if via == "disk":
            found = sorted(p for p in path.iterdir() if p.suffix.lower() in _DISK_SUFFIXES)
            kind = "disk image"
        else:
            found = sorted(path.glob("*.efi"))
            kind = ".efi"
        if len(found) != 1:
            raise ValueError(f"{path} holds {len(found)} {kind} files: name the image to boot")
        path = found[0]
    if not path.is_file():
        raise FileNotFoundError(f"image to boot not found: {path}")
    is_uki = path.suffix.lower() == ".efi"
    if (via == "kernel" and not is_uki) or (via == "disk" and is_uki):
        want = "a UKI (.efi)" if via == "kernel" else "a disk image"
        raise ValueError(f"boot via {via!r} needs {want}, not {path}")
    return path


def _use_kvm(kvm: bool | Literal["auto"]) -> bool:
    if kvm == "auto":
        return os.access("/dev/kvm", os.R_OK | os.W_OK)
    return bool(kvm)


def qemu_command(
    image: Path,
    log: Path,
    *,
    code: Path,
    store: Path | None,
    memory: str = "2G",
    cpus: int = 2,
    kvm: bool = False,
    cmdline: Sequence[str] = CONSOLE_CMDLINE,
    network: bool = True,
    qemu: str = QEMU,
    extra_args: Sequence[str] = (),
    forward: Mapping[int, int] | None = None,
) -> list[str]:
    """The QEMU command :func:`boot` runs: serial console to *log*, no display, no reboot;
    *forward* maps host ports (on 127.0.0.1) to guest TCP ports."""
    cmd = [qemu, "-machine", f"q35,accel={'kvm' if kvm else 'tcg'}"]
    cmd += ["-cpu", "host" if kvm else "max", "-m", memory, "-smp", str(cpus)]
    cmd += ["-no-reboot", "-display", "none", "-monitor", "none", "-serial", f"file:{log}"]
    if store is None:
        cmd += ["-bios", str(code)]
    else:
        cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}"]
        cmd += ["-drive", f"if=pflash,format=raw,file={store}"]
    if image.suffix == ".efi":
        cmd += ["-kernel", str(image)]
    else:
        disk = {".qcow2": "qcow2", ".vhd": "vpc"}.get(image.suffix.lower(), "raw")
        cmd += ["-drive", f"file={image},format={disk},if=virtio,snapshot=on"]
    if cmdline:
        extra = " ".join(cmdline).replace(",", ",,")
        cmd += ["-smbios", f"type=11,value=io.systemd.stub.kernel-cmdline-extra={extra}"]
    hostfwd = "".join(
        f",hostfwd=tcp:127.0.0.1:{host}-:{guest}" for host, guest in (forward or {}).items()
    )
    cmd += ["-nic", f"user,model=virtio-net-pci{hostfwd}" if network else "none"]
    return [*cmd, *extra_args]


def _read(log: Path) -> str:
    try:
        return log.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def boot(
    artifact: Bootable,
    *,
    timeout: float = 300.0,
    memory: str = "2G",
    cpus: int = 2,
    firmware: str | os.PathLike[str] | None = None,
    kvm: bool | Literal["auto"] = "auto",
    markers: Sequence[str] = DEFAULT_MARKERS,
    expect_units: Sequence[str] = (),
    cmdline: Sequence[str] = CONSOLE_CMDLINE,
    settle: float = 3.0,
    log: str | os.PathLike[str] | None = None,
    network: bool = True,
    qemu: str = QEMU,
    extra_args: Sequence[str] = (),
    forward: Mapping[int, int] | None = None,
    wait_http: Sequence[str] = (),
    via: BootPath = "auto",
) -> BootResult:
    """Boot *artifact* under QEMU with OVMF and wait for *markers* and *expect_units*.

    The serial console goes to *log* (a new temporary file by default). The boot ends
    when every marker is on the console and every expected unit started or failed (then
    *settle* more seconds, for a unit that fails just after), on a fatal line (panic, emergency
    mode), when QEMU exits, or after *timeout* seconds; QEMU is then stopped. *kvm*
    ``"auto"`` uses KVM when ``/dev/kvm`` is usable, else TCG. *forward* maps host ports
    (``127.0.0.1``) to guest ports; once the markers and units are in, the boot also waits
    until each *wait_http* URL (``http://127.0.0.1:HOST/``) answers with a 2xx, so the
    answers (:meth:`BootResult.http_get`) prove the network, firewall and service. *via*
    picks the boot path (:data:`BootPath`): ``"disk"`` boots a build's ``qemu.raw``
    through its ESP with no ``-kernel``. Raises :class:`BootUnavailable` when QEMU or OVMF
    is missing.
    """
    if shutil.which(qemu) is None:
        raise BootUnavailable(f"{qemu} is not installed")
    image = _image_path(artifact, via)
    code, store = find_ovmf(firmware)
    log_path = (
        Path(log)
        if log is not None
        else Path(tempfile.mkdtemp(prefix="tundravm-boot-")) / "console.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    if store is not None:
        store = Path(shutil.copyfile(store, log_path.with_name(f"{log_path.stem}.ovmf-vars.fd")))
    cmd = qemu_command(
        image,
        log_path,
        code=code,
        store=store,
        memory=memory,
        cpus=cpus,
        kvm=_use_kvm(kvm),
        cmdline=cmdline,
        network=network,
        qemu=qemu,
        extra_args=extra_args,
        forward=forward,
    )
    stderr_path = log_path.with_name(f"{log_path.stem}.qemu-stderr.log")
    start = time.monotonic()
    with stderr_path.open("wb") as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT)
    returncode: int | None = None
    timed_out = False
    done_at: float | None = None
    replies: dict[str, HttpResponse] = {url: HttpResponse(url, None, "") for url in wait_http}
    polled = 0.0
    try:
        while True:
            text = _read(log_path)
            scan = scan_console(text)
            now = time.monotonic()
            if scan.fatal:
                break
            # An expected unit that failed will not start: stop waiting for it
            waiting = set(missing_from(text, scan, markers, expect_units)) - set(scan.failed)
            pending = [url for url, reply in replies.items() if not reply.ok]
            if not waiting and pending and now - polled >= 1.0:
                polled = now
                replies.update((url, _fetch(url)) for url in pending)
                pending = [url for url, reply in replies.items() if not reply.ok]
            if not waiting and not pending:
                done_at = now if done_at is None else done_at
                if now - done_at >= settle:
                    break
            returncode = proc.poll()
            if returncode is not None:
                break
            if now - start >= timeout:
                timed_out = done_at is None
                break
            time.sleep(_POLL)
    finally:
        _stop(proc)
    duration = time.monotonic() - start
    text = clean_console(_read(log_path))
    scan = scan_console(text)
    return BootResult(
        console=text,
        log=log_path,
        duration=duration,
        command=tuple(cmd),
        started=scan.started,
        failed=scan.failed,
        fatal=scan.fatal,
        missing=(
            *missing_from(text, scan, markers, expect_units),
            *(url for url, reply in replies.items() if not reply.ok),
        ),
        timed_out=timed_out,
        returncode=returncode,
        stderr=_read(stderr_path),
        exited=tuple(unit for unit in map(unit_name, expect_units) if unit in scan.exited),
        http=tuple(replies.values()),
    )


_POLL = 0.25
"""Seconds between reads of the console log."""
