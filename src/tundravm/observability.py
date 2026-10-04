"""Structured logging, progress events, and reporters for long-running operations.

``Image.bake`` stamps every step as an :class:`Event` and hands it to a
:class:`Reporter`: :class:`TextReporter` for people, :class:`JsonReporter` for
machines (one JSON object per line), :class:`NullReporter` to discard.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TextIO

if TYPE_CHECKING:
    from .models import BakeResult

EventKind = Literal["phase", "log", "artifact", "warning", "done"]
PhaseStatus = Literal["start", "ok", "fail"]

TAIL_LINES = 5
"""Backend output lines a non-verbose :class:`TextReporter` keeps for failures."""

GLOBAL_LABEL = "tundravm"
"""Label :class:`TextReporter` prints for events that span several profiles."""


@dataclass(frozen=True, slots=True)
class Event:
    """One progress event.

    ``profile`` is the variant the event concerns (``variant`` in :meth:`to_dict`),
    or ``None`` for a step that spans several. ``elapsed_s`` counts seconds since
    the operation started.
    Phase events carry ``extra["phase"]`` and ``extra["status"]`` (``start``,
    ``ok`` or ``fail``); finished phases add ``extra["duration_s"]``. Backend
    output lines are ``log`` events with ``extra["source"] == "backend"``;
    forwarded :class:`StructuredLogger` records have ``source == "logger"``;
    a backend's informational notices (e.g. adding a tools tree) have
    ``source == "notice"`` and are shown unless quiet.
    """

    kind: EventKind
    profile: str | None
    message: str
    elapsed_s: float = 0.0
    extra: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "variant": self.profile,
            "message": self.message,
            "elapsed_s": round(self.elapsed_s, 3),
            "extra": dict(self.extra),
        }


class Reporter(Protocol):
    def emit(self, event: Event) -> None:
        """Handle one event; called from the thread running the operation."""


class NullReporter:
    """Discard every event."""

    def emit(self, event: Event) -> None:
        del event

    def close(self) -> None:
        pass


class JsonReporter:
    """Write each event as one JSON object per line."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._broken = False

    def emit(self, event: Event) -> None:
        if self._broken:
            return
        try:
            self._stream.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
            self._stream.flush()
        except BrokenPipeError:
            self._broken = True

    def close(self) -> None:
        if not self._broken:
            try:
                self._stream.flush()
            except BrokenPipeError:
                self._broken = True


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAR_LINE = "\r\x1b[2K"
_STYLES = {"label": "36", "ok": "32", "fail": "1;31", "warn": "33", "dim": "2"}


@dataclass(slots=True)
class _Running:
    profile: str | None
    message: str
    started: float
    beat: float
    last_line: str = ""


class TextReporter:
    """Human progress lines such as ``[azure] compile ... ok (0.4s)``.

    With ``live`` (default: *stream* is a TTY) the running phase is one line
    redrawn in place with a ticking timer and the latest backend line; without
    it, phases print a start and an end line plus a ``still running`` line every
    ``heartbeat_s``. Backend output is echoed as ``[profile] | line`` when
    ``verbose``; otherwise only the last *tail_lines* lines are kept and printed
    if the phase fails. ``quiet`` prints failures and error diagnostics only.
    A closed stream (``BrokenPipeError``) silences the reporter, not the build.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        verbose: bool = False,
        color: bool = False,
        quiet: bool = False,
        live: bool | None = None,
        tail_lines: int = TAIL_LINES,
        tick_s: float = 0.5,
        heartbeat_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream
        self._verbose = verbose
        self._color = color
        self._quiet = quiet
        self._live = (_isatty(stream) if live is None else live) and not quiet
        self._tail_lines = tail_lines
        self._tails: dict[str | None, deque[str]] = {}
        self._tick_s = tick_s
        self._heartbeat_s = heartbeat_s
        self._clock = clock
        self._lock = threading.RLock()
        self._running: _Running | None = None
        self._stop: threading.Event | None = None
        self._broken = False

    def emit(self, event: Event) -> None:
        with self._lock:
            if self._broken:
                return
            try:
                self._dispatch(event)
            except BrokenPipeError:
                self._broken = True

    def close(self) -> None:
        """Stop the timer thread and finish any in-place line."""
        with self._lock:
            if self._stop is not None:
                self._stop.set()
                self._stop = None
            if self._broken:
                return
            try:
                self._finish_running()
                self._stream.flush()
            except BrokenPipeError:
                self._broken = True

    def _dispatch(self, event: Event) -> None:
        if event.kind == "phase":
            self._phase(event)
        elif event.kind == "log":
            self._log(event)
        elif event.kind == "warning":
            self._warning(event)
        elif event.kind == "artifact":
            if not self._quiet:
                self._line(event.profile, event.message)
        elif event.kind == "done":
            self.close()
            if not self._quiet:
                failed = event.extra.get("status") == "fail"
                self._line(
                    event.profile, self._paint("fail", event.message) if failed else event.message
                )

    def _phase(self, event: Event) -> None:
        status = event.extra.get("status")
        if status == "start":
            self._tails.pop(event.profile, None)
            if self._quiet:
                return
            now = self._clock()
            self._finish_running()
            if not self._live:
                self._line(event.profile, f"{event.message} ...")
            self._running = _Running(event.profile, event.message, now, now)
            self._redraw()
            self._ensure_ticker()
            return
        self._finish_running()
        duration = event.extra.get("duration_s")
        suffix = f" ({format_duration(float(duration))})" if duration else ""
        if status == "fail":
            failed = self._paint("fail", "FAILED")
            self._line(event.profile, f"{event.message} ... {failed}{suffix}")
            tail = self._tails.pop(event.profile, None)
            if tail and not self._verbose:
                for line in tail:
                    self._line(event.profile, f"{self._paint('dim', '|')} {line}")
        elif not self._quiet:
            self._line(event.profile, f"{event.message} ... {self._paint('ok', 'ok')}{suffix}")

    def _log(self, event: Event) -> None:
        if event.extra.get("source") == "notice":
            if not self._quiet:
                self._line(event.profile, f"{self._paint('dim', 'note')} {event.message}")
            return
        if event.extra.get("source") != "backend":
            if self._verbose:
                self._line(event.profile, self._paint("dim", event.message))
            return
        tail = self._tails.setdefault(event.profile, deque(maxlen=self._tail_lines))
        tail.append(event.message)
        if self._verbose:
            self._line(event.profile, f"{self._paint('dim', '|')} {event.message}")
        elif self._running is not None:
            self._running.last_line = event.message
            self._redraw()

    def _warning(self, event: Event) -> None:
        is_error = event.extra.get("level") == "error"
        if self._quiet and not is_error:
            return
        label = self._paint("fail", "error") if is_error else self._paint("warn", "warning")
        self._line(event.profile, f"{label} {event.message}")

    def _line(self, profile: str | None, text: str) -> None:
        live = self._live and self._running is not None
        if live:
            self._write(_CLEAR_LINE)
        self._write(f"{self._label(profile)} {text}\n")
        if live:
            self._redraw()
        self._stream.flush()

    def _finish_running(self) -> None:
        if self._running is not None and self._live:
            self._write(_CLEAR_LINE)
        self._running = None

    def _redraw(self) -> None:
        running = self._running
        if running is None or not self._live:
            return
        label = f"[{running.profile or GLOBAL_LABEL}]"
        head = f" {running.message} ... {format_duration(self._clock() - running.started)}"
        width = shutil.get_terminal_size((100, 24)).columns - 1
        last = _ANSI.sub("", running.last_line).replace("\t", " ").strip()
        room = width - len(label) - len(head) - 2
        tail = f"  {self._paint('dim', last[:room])}" if last and room > 8 else ""
        self._write(f"{_CLEAR_LINE}{self._paint('label', label)}{head}{tail}")
        self._stream.flush()

    def _heartbeat(self) -> None:
        running = self._running
        if running is None or self._live:
            return
        now = self._clock()
        if now - running.beat < self._heartbeat_s:
            return
        running.beat = now
        took = format_duration(now - running.started)
        self._line(running.profile, f"{running.message} ... still running ({took})")

    def _ensure_ticker(self) -> None:
        if self._stop is not None:
            return
        self._stop = threading.Event()
        threading.Thread(
            target=self._tick, args=(self._stop,), name="tundravm-progress", daemon=True
        ).start()

    def _tick(self, stop: threading.Event) -> None:
        while not stop.wait(self._tick_s):
            with self._lock:
                if stop.is_set() or self._broken:
                    return
                try:
                    self._redraw()
                    self._heartbeat()
                except BrokenPipeError:
                    self._broken = True

    def _label(self, profile: str | None) -> str:
        return self._paint("label", f"[{profile or GLOBAL_LABEL}]")

    def _paint(self, style: str, text: str) -> str:
        if not self._color or not text:
            return text
        return f"\x1b[{_STYLES[style]}m{text}\x1b[0m"

    def _write(self, text: str) -> None:
        self._stream.write(text)


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


@dataclass(slots=True)
class Phase:
    """Timing handle yielded by :meth:`Progress.phase`; ``duration_s`` is set on exit."""

    name: str
    duration_s: float = 0.0


class Progress:
    """A reporter plus the clock that stamps ``elapsed_s`` on its events."""

    def __init__(
        self, reporter: Reporter | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.reporter: Reporter = reporter if reporter is not None else NullReporter()
        self._clock = clock
        self.started = clock()

    def elapsed(self) -> float:
        return self._clock() - self.started

    def emit(self, kind: EventKind, profile: str | None, message: str, **extra: str) -> None:
        self.reporter.emit(Event(kind, profile, message, self.elapsed(), extra))

    def output(self, profile: str) -> Callable[[str], None]:
        """Callback that forwards one backend output line as a ``log`` event."""

        def forward(line: str) -> None:
            self.emit("log", profile, line, source="backend")

        return forward

    def notice(self, profile: str) -> Callable[[str, str], None]:
        """Callback for a backend's ``(level, message)`` notices.

        ``info`` becomes a ``log`` event with ``source == "notice"``; anything else a
        ``warning`` event.
        """

        def forward(level: str, message: str) -> None:
            if level == "info":
                self.emit("log", profile, message, source="notice", level=level)
            else:
                self.emit("warning", profile, message, level="warning", source="backend")

        return forward

    @contextmanager
    def phase(self, name: str, message: str, *, profile: str | None) -> Iterator[Phase]:
        """Emit ``start``, then ``ok`` or ``fail`` (re-raising) with the duration."""
        handle = Phase(name)
        begun = self._clock()
        self.emit("phase", profile, message, phase=name, status="start")
        try:
            yield handle
        except BaseException:
            handle.duration_s = self._clock() - begun
            self.emit("phase", profile, message, **_ended(name, "fail", handle.duration_s))
            raise
        handle.duration_s = self._clock() - begun
        self.emit("phase", profile, message, **_ended(name, "ok", handle.duration_s))

    @contextmanager
    def guard(self, *, profile: str | None) -> Iterator[None]:
        """Emit a failing ``done`` event if the body raises."""
        try:
            yield
        except BaseException as exc:
            took = self.elapsed()
            self.emit(
                "done",
                profile,
                f"failed after {format_duration(took)}",
                status="fail",
                duration_s=f"{took:.3f}",
                error=str(getattr(exc, "code", type(exc).__name__)),
            )
            raise


def _ended(name: str, status: PhaseStatus, duration: float) -> dict[str, str]:
    return {"phase": name, "status": status, "duration_s": f"{duration:.3f}"}


@dataclass(slots=True)
class StructuredLogger:
    records: list[dict[str, Any]] = field(default_factory=list)
    reporter: Reporter | None = None
    started_at: float | None = None

    def log(
        self,
        *,
        operation: str,
        profile: str | None,
        phase: str | None,
        module: str | None,
        builder: str | None,
        message: str,
        level: str = "info",
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "level": level,
            "operation": operation,
            "profile": profile,
            "phase": phase,
            "module": module,
            "builder": builder,
            "message": message,
        }
        if extra is not None:
            record["extra"] = extra
        self.records.append(record)
        if self.reporter is not None:
            self._forward(record)

    @contextmanager
    def attached(self, reporter: Reporter, *, started_at: float) -> Iterator[None]:
        """Forward records to *reporter* for the duration of the block."""
        previous = (self.reporter, self.started_at)
        self.reporter, self.started_at = reporter, started_at
        try:
            yield
        finally:
            self.reporter, self.started_at = previous

    def _forward(self, record: Mapping[str, Any]) -> None:
        assert self.reporter is not None
        elapsed = 0.0 if self.started_at is None else time.monotonic() - self.started_at
        level = str(record["level"])
        kind: EventKind = "warning" if level in ("warning", "error") else "log"
        keys = ("operation", "phase", "module", "builder", "level")
        extra = {key: str(record[key]) for key in keys if record[key] is not None}
        extra["source"] = "logger"
        self.reporter.emit(Event(kind, record["profile"], str(record["message"]), elapsed, extra))

    def records_for_profile(self, profile: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record.get("profile") == profile]


def format_duration(seconds: float) -> str:
    """``0.4s``, ``12.3s``, ``4m05s``, ``1h02m``."""
    if seconds < 60:
        return f"{max(seconds, 0.0):.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_size(size: int) -> str:
    """Binary units: ``93 B``, ``1.5 KiB``, ``2.0 GiB``."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def render_bake_summary(result: BakeResult) -> str:
    """Aligned table: variant, target, artifact, size, sha256 prefix, duration."""
    rows = [("variant", "target", "artifact", "size", "sha256", "time")]
    for name in sorted(result.profiles):
        profile = result.profiles[name]
        took = "-" if profile.duration_s is None else format_duration(profile.duration_s)
        if not profile.artifacts:
            rows.append((name, "-", "(no artifacts)", "-", "-", took))
        for target in sorted(profile.artifacts):
            ref = profile.artifacts[target]
            path = Path(ref.path)
            size = format_size(path.stat().st_size) if path.exists() else "-"
            digest = ref.digest[:12] if ref.digest else "-"
            rows.append((name, target, display_path(path), size, digest, took))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def display_path(path: Path) -> str:
    """*path* relative to the working directory when it lies inside it, else as given."""
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except (OSError, ValueError):
        return os.fspath(path)


__all__ = [
    "GLOBAL_LABEL",
    "TAIL_LINES",
    "Event",
    "EventKind",
    "JsonReporter",
    "NullReporter",
    "Phase",
    "Progress",
    "Reporter",
    "StructuredLogger",
    "TextReporter",
    "display_path",
    "format_duration",
    "format_size",
    "render_bake_summary",
]
