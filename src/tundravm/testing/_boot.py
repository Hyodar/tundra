"""Boot a baked image under QEMU and read its serial console: :func:`boot`.

The image is booted the way ``deploy --target qemu`` boots it without TDX: OVMF from
pflash and the UKI through ``-kernel`` (a disk image through ``-drive``, on a
throwaway snapshot). Nothing is added to the image: the console settings the harness
needs reach the kernel through SMBIOS (``io.systemd.stub.kernel-cmdline-extra``, the
way ``mkosi vm`` passes them), which systemd-stub appends to the UKI's own command line
when Secure Boot is off.
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
)
"""Console text that ends the boot at once: a panic, emergency mode, a dead PID 1."""

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


def clean_console(raw: str) -> str:
    """*raw* serial output without ANSI escapes and carriage returns."""
    return _ANSI.sub("", raw)


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
    for line in clean_console(text).splitlines():
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
    for line in clean_console(text).splitlines():
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
    """The *markers* not in *text* and the *units* not started, in order."""
    clean = clean_console(text)
    absent = [marker for marker in markers if marker not in clean]
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
"""A UKI (``.efi``) or disk image path, or an :class:`~tundravm.declarative.lifecycle.Artifact`."""


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


def _image_path(artifact: Bootable) -> Path:
    path = Path(artifact.path if hasattr(artifact, "path") else artifact)
    if path.is_dir():
        found = sorted(path.glob("*.efi"))
        if len(found) != 1:
            raise ValueError(f"{path} holds {len(found)} .efi files: name the image to boot")
        path = found[0]
    if not path.is_file():
        raise FileNotFoundError(f"image to boot not found: {path}")
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
) -> BootResult:
    """Boot *artifact* under QEMU with OVMF and wait for *markers* and *expect_units*.

    The serial console goes to *log* (a new temporary file by default). The boot ends
    when every marker is on the console and every expected unit started or failed (then
    *settle* more seconds, for a unit that fails just after), on a fatal line (panic, emergency
    mode), when QEMU exits, or after *timeout* seconds; QEMU is then stopped. *kvm*
    ``"auto"`` uses KVM when ``/dev/kvm`` is usable, else TCG. *forward* maps host ports
    (``127.0.0.1``) to guest ports; once the markers and units are in, the boot also waits
    until each *wait_http* URL (``http://127.0.0.1:HOST/``) answers with a 2xx, so the
    answers (:meth:`BootResult.http_get`) prove the network, firewall and service. Raises
    :class:`BootUnavailable` when QEMU or OVMF is missing.
    """
    if shutil.which(qemu) is None:
        raise BootUnavailable(f"{qemu} is not installed")
    image = _image_path(artifact)
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
