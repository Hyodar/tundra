"""Bake progress: events, reporters, backend output streaming, and the ``bake`` CLI flags."""

from __future__ import annotations

import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends.base import StreamResult, failure_message, run_streaming
from tundravm.backends.inprocess import InProcessBackend
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.errors import BackendExecutionError, LintError
from tundravm.models import ArtifactRef, BakeRequest, BakeResult, ProfileBuildResult
from tundravm.observability import (
    Event,
    JsonReporter,
    StructuredLogger,
    TextReporter,
    format_duration,
    format_size,
    render_bake_summary,
)


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


# -- JsonReporter -------------------------------------------------------------


def test_json_reporter_writes_one_object_per_line() -> None:
    stream = io.StringIO()
    reporter = JsonReporter(stream)
    reporter.emit(_phase("start"))
    reporter.emit(_output("hello"))
    reporter.emit(Event("done", None, "baked 1 profile in 0.1s", 0.1234, {"status": "ok"}))
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [p["kind"] for p in payloads] == ["phase", "log", "done"]
    assert payloads[1] == {
        "kind": "log",
        "profile": "default",
        "message": "hello",
        "elapsed_s": 0.0,
        "extra": {"source": "backend"},
    }
    assert payloads[2]["elapsed_s"] == 0.123


# -- backend streaming --------------------------------------------------------


def test_inprocess_backend_streams_output_lines(tmp_path: Path) -> None:
    lines: list[str] = []
    request = BakeRequest(
        profile="default",
        build_dir=tmp_path / "build",
        emit_dir=tmp_path / "emit",
        on_output=lines.append,
    )
    InProcessBackend().execute(request)
    assert lines[0].startswith("inprocess: building profile default")
    assert "inprocess: wrote qemu artifact disk.qcow2" in lines
    assert lines[-1] == "inprocess: done (1 artifacts)"


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


def test_local_backend_maps_streamed_failure_to_backend_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> StreamResult:
        return StreamResult(returncode=1, tail=("E: Unable to locate package nope",))

    monkeypatch.setattr(LocalLinuxBackend, "_ensure_local_prerequisites", lambda self: None)
    monkeypatch.setattr("tundravm.backends.local_linux.run_streaming", fake_run)
    request = BakeRequest(profile="default", build_dir=tmp_path / "b", emit_dir=tmp_path / "e")

    with pytest.raises(BackendExecutionError) as excinfo:
        LocalLinuxBackend(privilege="none").execute(request)

    assert excinfo.value.code == "E_BACKEND_EXECUTION"
    assert "E: Unable to locate package nope" in str(excinfo.value)
    assert excinfo.value.context["returncode"] == "1"


# -- Image.bake events --------------------------------------------------------


def _image(tmp_path: Path) -> Image:
    img = Image(build_dir=tmp_path / "build", backend=InProcessBackend())
    img.output_targets("qemu")
    return img


def test_bake_emits_phases_output_artifacts_and_done(tmp_path: Path) -> None:
    capture = Capture()
    result = _image(tmp_path).bake(reporter=capture)

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
    assert done.message.startswith("baked 1 profile in ")
    assert all(b.elapsed_s >= a.elapsed_s for a, b in pairwise(capture.events))
    ok_build = next(
        e
        for e in capture.events
        if (e.extra.get("phase"), e.extra.get("status")) == ("build", "ok")
    )
    assert float(ok_build.extra["duration_s"]) >= 0


def test_bake_forwards_logger_records_only_while_attached(tmp_path: Path) -> None:
    capture = Capture()
    img = _image(tmp_path)
    img.bake(reporter=capture)
    logged = [e for e in capture.events if e.extra.get("source") == "logger"]
    assert [e.extra["operation"] for e in logged] == ["bake_profile_start", "bake_profile_complete"]
    assert img.logger.reporter is None


def test_bake_without_reporter_records_digests_and_durations(tmp_path: Path) -> None:
    result = _image(tmp_path).bake()
    profile = result.profiles["default"]
    assert profile.artifacts["qemu"].digest is not None
    assert len(profile.artifacts["qemu"].digest or "") == 64
    assert profile.duration_s is not None and result.duration_s is not None
    assert result.duration_s >= profile.duration_s
    saved = json.loads((tmp_path / "build" / "bake-result.json").read_text())
    assert (
        saved["profiles"]["default"]["artifact_digests"]["qemu"] == profile.artifacts["qemu"].digest
    )


def test_bake_lint_failure_emits_failed_phase_and_done(tmp_path: Path) -> None:
    capture = Capture()
    img = _image(tmp_path)
    img.file("relative/path", content="x")
    with pytest.raises(LintError):
        img.bake(reporter=capture)
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


def test_render_bake_summary_aligns_columns(tmp_path: Path) -> None:
    disk = tmp_path / "disk.qcow2"
    disk.write_bytes(b"x" * 2048)
    result = BakeResult(
        profiles={
            "default": ProfileBuildResult(
                profile="default",
                artifacts={"qemu": ArtifactRef(target="qemu", path=disk, digest="ab" * 32)},
                duration_s=75.0,
            ),
            "empty": ProfileBuildResult(profile="empty"),
        }
    )
    lines = render_bake_summary(result).splitlines()
    assert lines[0].split() == ["profile", "target", "artifact", "size", "sha256", "time"]
    assert lines[1].split() == ["default", "qemu", str(disk), "2.0", "KiB", "abababababab", "1m15s"]
    assert lines[2].split() == ["empty", "-", "(no", "artifacts)", "-", "-", "-"]
    assert lines[0].index("target") == lines[1].index("qemu") == lines[2].index("-")


# -- CLI ----------------------------------------------------------------------

RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl")
img.output_targets("qemu")
"""

FAILING_RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.errors import BackendExecutionError


class FlakyBackend(InProcessBackend):
    def execute(self, request):
        for index in range(1, 9):
            request.on_output(f"mkosi line {index}")
        raise BackendExecutionError("mkosi build failed. (exit 1)", context={"backend": "flaky"})


img = Image(build_dir=BUILD_DIR, backend=FlakyBackend(name="flaky"))
img.output_targets("qemu")
"""


def _write_recipe(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "recipe.py"
    path.write_text(f"BUILD_DIR = {str(tmp_path / 'build')!r}\n" + body, encoding="utf-8")
    return path


def _run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(list(argv), stdout=out), out.getvalue()


def test_cli_bake_default_prints_progress_and_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = _write_recipe(tmp_path, RECIPE)
    code, out = _run("bake", str(recipe))
    err = capsys.readouterr().err
    assert code == EXIT_OK
    assert re.search(r"^\[default\] build via inprocess \.\.\. ok \(\d+\.\ds\)$", err, re.M)
    assert re.search(r"^\[default\] artifact qemu \S+disk\.qcow2 \(\d+ B\)$", err, re.M)
    assert "[default] | " not in err
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("profile"))
    assert lines[header].split() == ["profile", "target", "artifact", "size", "sha256", "time"]
    assert re.match(
        r"default\s+qemu\s+\S+disk\.qcow2\s+\d+ B\s+[0-9a-f]{12}\s+\d+\.\ds$", lines[header + 1]
    )
    assert lines[-1] == f"next: tundravm deploy {recipe} --target qemu"


def test_cli_bake_verbose_echoes_backend_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = _write_recipe(tmp_path, RECIPE)
    code, _ = _run("bake", str(recipe), "-v")
    err = capsys.readouterr().err
    assert code == EXIT_OK
    assert "[default] | inprocess: wrote qemu artifact disk.qcow2" in err
    assert "[default] Starting profile bake via inprocess backend." in err


def test_cli_bake_quiet_prints_only_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = _write_recipe(tmp_path, RECIPE)
    code, out = _run("bake", str(recipe), "-q")
    assert code == EXIT_OK
    assert capsys.readouterr().err == ""
    assert out.splitlines()[0].startswith("profile  target")
    assert out.splitlines()[-1].startswith("next: tundravm deploy")


def test_cli_bake_json_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recipe = _write_recipe(tmp_path, RECIPE)
    code, out = _run("bake", str(recipe), "--json-logs", "--lock")
    assert code == EXIT_OK
    assert capsys.readouterr().err == ""
    events = [json.loads(line) for line in out.splitlines()]
    assert events[0]["message"].startswith("locked ")
    assert events[-1]["kind"] == "done"
    assert events[-1]["extra"]["status"] == "ok"
    assert any(e["extra"].get("source") == "backend" for e in events)
    assert "next:" not in out


def test_cli_bake_flags_are_mutually_exclusive(tmp_path: Path) -> None:
    recipe = _write_recipe(tmp_path, RECIPE)
    with pytest.raises(SystemExit):
        _run("bake", str(recipe), "-v", "-q")


def test_cli_bake_failure_shows_output_tail_and_error_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = _write_recipe(tmp_path, FAILING_RECIPE)
    code, out = _run("bake", str(recipe))
    err = capsys.readouterr().err
    assert code == EXIT_SDK_ERROR
    assert out == ""
    assert re.search(r"^\[default\] build via flaky \.\.\. FAILED \(\d+\.\ds\)$", err, re.M)
    for index in range(4, 9):
        assert f"[default] | mkosi line {index}\n" in err
    assert "mkosi line 3" not in err
    assert re.search(r"^\[default\] failed after \d+\.\ds$", err, re.M)
    assert "error [E_BACKEND_EXECUTION]: mkosi build failed. (exit 1)" in err
    assert err.index("mkosi line 8") < err.index("error [E_BACKEND_EXECUTION]")


def test_cli_bake_failure_in_quiet_mode_still_shows_tail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = _write_recipe(tmp_path, FAILING_RECIPE)
    code, _ = _run("bake", str(recipe), "-q")
    err = capsys.readouterr().err
    assert code == EXIT_SDK_ERROR
    assert err.splitlines()[0].startswith("[default] build via flaky ... FAILED")
    assert "[default] | mkosi line 8" in err
