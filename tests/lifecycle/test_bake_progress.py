"""Bake progress: events, reporters, backend output streaming, and the ``bake`` CLI flags."""

from __future__ import annotations

import io
import sys
import time
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import pytest

from tundravm._reporting import (
    Event,
    Progress,
    StructuredLogger,
    TextReporter,
    format_duration,
    format_size,
)
from tundravm.backends.base import StreamResult, failure_message, run_streaming
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import File, Package, Recipe, lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.errors import LintError
from tundravm.models import BakeRequest, BakeResult


@dataclass
class Capture:
    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def phases(self) -> list[tuple[str, str]]:
        return [(e.extra["phase"], e.extra["status"]) for e in self.events if e.kind == "phase"]


def _phase(
    status: str, message: str = "build via x", profile: str | None = "default", **extra: str
) -> Event:
    return Event("phase", profile, message, 0.0, {"phase": "build", "status": status, **extra})


def _output(line: str, profile: str = "default") -> Event:
    return Event("log", profile, line, 0.0, {"source": "backend"})


def _feed(reporter: TextReporter, *, lines: int, status: str) -> None:
    reporter.emit(_phase("start"))
    for index in range(1, lines + 1):
        reporter.emit(_output(f"line {index}"))
    reporter.emit(_phase(status, duration_s="1.25"))


# -- TextReporter -------------------------------------------------------------


def test_text_reporter_plain_success_hides_backend_output() -> None:
    stream = io.StringIO()
    _feed(TextReporter(stream, live=False), lines=8, status="ok")
    assert stream.getvalue() == "[default] build via x ...\n[default] build via x ... ok (1.2s)\n"


def test_text_reporter_failure_prints_only_the_tail() -> None:
    stream = io.StringIO()
    _feed(TextReporter(stream, live=False), lines=8, status="fail")
    lines = stream.getvalue().splitlines()
    assert lines[1] == "[default] build via x ... FAILED (1.2s)"
    assert lines[2:] == [f"[default] | line {index}" for index in range(4, 9)]


def test_text_reporter_verbose_echoes_every_line() -> None:
    stream = io.StringIO()
    _feed(TextReporter(stream, verbose=True, live=False), lines=3, status="fail")
    assert stream.getvalue().splitlines() == [
        "[default] build via x ...",
        "[default] | line 1",
        "[default] | line 2",
        "[default] | line 3",
        "[default] build via x ... FAILED (1.2s)",
    ]


def test_text_reporter_quiet_prints_failures_only() -> None:
    stream = io.StringIO()
    reporter = TextReporter(stream, quiet=True)
    _feed(reporter, lines=2, status="ok")
    reporter.emit(Event("artifact", "default", "artifact qemu disk.qcow2 (1 B)"))
    reporter.emit(Event("warning", "default", "w-code: meh", extra={"level": "warning"}))
    assert stream.getvalue() == ""
    _feed(reporter, lines=2, status="fail")
    assert stream.getvalue().splitlines() == [
        "[default] build via x ... FAILED (1.2s)",
        "[default] | line 1",
        "[default] | line 2",
    ]


def test_text_reporter_live_redraws_one_line_in_place() -> None:
    stream = io.StringIO()
    reporter = TextReporter(stream, live=True, tick_s=60)
    _feed(reporter, lines=2, status="ok")
    reporter.close()
    text = stream.getvalue()
    assert "\r\x1b[2K[default] build via x ... 0." in text
    assert "  line 2" in text  # latest backend line shown on the live line
    assert text.endswith("\r\x1b[2K[default] build via x ... ok (1.2s)\n")
    assert "[default] | line" not in text


def test_text_reporter_color_and_global_label() -> None:
    stream = io.StringIO()
    reporter = TextReporter(stream, color=True, live=False)
    reporter.emit(_phase("ok", message="compile", profile=None, duration_s="0.4"))
    assert stream.getvalue() == "\x1b[36m[tundravm]\x1b[0m compile ... \x1b[32mok\x1b[0m (0.4s)\n"


def test_text_reporter_heartbeat_when_not_a_tty() -> None:
    stream = io.StringIO()
    reporter = TextReporter(stream, live=False, tick_s=0.01, heartbeat_s=0.0)
    reporter.emit(_phase("start"))
    deadline = time.monotonic() + 5
    while "still running" not in stream.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    reporter.close()
    assert "[default] build via x ... still running (" in stream.getvalue()


def test_text_reporter_survives_a_closed_stream() -> None:
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError

    reporter = TextReporter(Broken(), live=False)
    _feed(reporter, lines=1, status="ok")
    reporter.close()


# -- backend streaming --------------------------------------------------------


def test_the_linux_backend_streams_the_build_output_only(tmp_path: Path) -> None:
    lines: list[str] = []
    request = BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "emit",
        on_output=lines.append,
    )
    LocalLinuxBackend().execute(request)
    assert lines == ["fake mkosi build"]  # summary --json is captured, not streamed


def test_run_streaming_merges_stderr_and_keeps_a_tail() -> None:
    script = (
        "import sys\n"
        "for i in range(30):\n"
        "    print(f'out {i}', flush=True)\n"
        "print('to stderr', file=sys.stderr, flush=True)\n"
        "sys.exit(3)\n"
    )
    seen: list[str] = []
    result = run_streaming([sys.executable, "-c", script], on_output=seen.append)
    assert result.returncode == 3
    assert seen[0] == "out 0"
    assert seen[-1] == "to stderr"
    assert len(result.tail) == 20
    assert result.tail[-1] == "to stderr"


def test_failure_message_includes_tail_only_when_not_streamed() -> None:
    result = StreamResult(returncode=2, tail=("a", "b"))
    assert failure_message("boom.", result, streamed=True) == "boom. (exit 2)"
    assert failure_message("boom.", result, streamed=False) == (
        "boom. (exit 2)\nLast output:\n  | a\n  | b"
    )


# -- bake events ----------------------------------------------------------------


def _recipe(*items: File) -> Recipe:
    return Recipe("bake", items=(Package("linux-image-amd64"), *items))


def _bake(tmp_path: Path, recipe: Recipe, reporter: Capture | None) -> BakeResult:
    """Bake *recipe* lowered against the fake mkosi, with *reporter*, as ``build`` does."""
    result, _ = bake_image(
        lower(recipe),
        ("default",),
        locked=None,
        backend=LocalLinuxBackend(),
        build=tmp_path / "build",
        reporter=reporter,
    )
    return result


def test_bake_emits_phases_output_artifacts_and_done(tmp_path: Path) -> None:
    capture = Capture()
    result = _bake(tmp_path, _recipe(), capture)

    assert capture.phases() == [
        ("lint", "start"),
        ("lint", "ok"),
        ("compile", "start"),
        ("compile", "ok"),
        ("prepare", "start"),
        ("prepare", "ok"),
        ("build", "start"),
        ("build", "ok"),
    ]
    kinds = [e.kind for e in capture.events]
    build_start = next(i for i, e in enumerate(capture.events) if e.extra.get("phase") == "build")
    backend_lines = [e for e in capture.events if e.extra.get("source") == "backend"]
    assert backend_lines and all(capture.events.index(e) > build_start for e in backend_lines)
    assert all(e.profile == "default" for e in backend_lines)

    artifacts = [e for e in capture.events if e.kind == "artifact"]
    assert [e.extra["role"] for e in artifacts] == ["image", "report"]
    image_event = artifacts[0]
    ref = result.profiles["default"].artifacts["qemu"]
    assert image_event.extra["sha256"] == ref.digest
    assert int(image_event.extra["size_bytes"]) == Path(ref.path).stat().st_size

    assert kinds[-1] == "done"
    done = capture.events[-1]
    assert done.extra["status"] == "ok"
    assert done.message.startswith("built 1 profile in ")
    assert all(b.elapsed_s >= a.elapsed_s for a, b in pairwise(capture.events))
    ok_build = next(
        e
        for e in capture.events
        if (e.extra.get("phase"), e.extra.get("status")) == ("build", "ok")
    )
    assert float(ok_build.extra["duration_s"]) >= 0


def test_bake_lint_failure_emits_failed_phase_and_done(tmp_path: Path) -> None:
    capture = Capture()
    with pytest.raises(LintError):
        _bake(tmp_path, _recipe(File("/etc/../../escape", "x")), capture)
    assert capture.phases() == [("lint", "start"), ("lint", "fail")]
    warnings = [e for e in capture.events if e.kind == "warning"]
    assert any(e.extra["level"] == "error" for e in warnings)
    assert capture.events[-1].kind == "done"
    assert capture.events[-1].extra == {
        "status": "fail",
        "duration_s": capture.events[-1].extra["duration_s"],
        "error": "E_LINT",
    }


def test_structured_logger_forwards_warnings() -> None:
    capture = Capture()
    logger = StructuredLogger()
    with logger.attached(capture, started_at=time.monotonic()):
        logger.log(
            operation="op",
            profile="p",
            phase=None,
            module="m",
            builder=None,
            message="careful",
            level="warning",
        )
    logger.log(operation="op", profile="p", phase=None, module=None, builder=None, message="x")
    assert len(logger.records) == 2
    assert [e.kind for e in capture.events] == ["warning"]
    assert capture.events[0].extra == {
        "operation": "op",
        "module": "m",
        "level": "warning",
        "source": "logger",
    }


# -- summary helpers ----------------------------------------------------------


def test_format_helpers() -> None:
    assert format_duration(0.04) == "0.0s"
    assert format_duration(12.34) == "12.3s"
    assert format_duration(245) == "4m05s"
    assert format_duration(3725) == "1h02m"
    assert format_size(93) == "93 B"
    assert format_size(1536) == "1.5 KiB"
    assert format_size(3 * 1024**3) == "3.0 GiB"


def test_backend_notices_show_unless_quiet() -> None:
    def run(*, quiet: bool = False) -> str:
        stream = io.StringIO()
        progress = Progress(TextReporter(stream, live=False, quiet=quiet))
        progress.notice("default")("info", "adding a tools tree")
        progress.notice("default")("warning", "could not chown")
        return stream.getvalue()

    assert run().splitlines() == [
        "[default] note adding a tools tree",
        "[default] warning could not chown",
    ]
    assert run(quiet=True) == ""
