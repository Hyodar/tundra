"""RTMR measurement derivation.

Runs ``measured-boot`` (UKI ``.efi`` or ``.raw``/``.img`` disk image) or
``dstack-mr`` (UKI ``.efi``) from ``PATH`` to predict the RTMR registers of a
baked artifact. Without a tool that measures one, ``derive()`` raises
``MeasurementError`` unless ``allow_placeholder=True``, which returns
SHA-256 stand-ins derived from artifact digests (``source="placeholder"``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from tundravm.backends.base import Requirement
from tundravm.errors import MeasurementError
from tundravm.measure.model import Measurements, MeasurementSource
from tundravm.measure.placeholder import digest_values, placeholder_measurements

ToolRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
"""Runs one tool command line; injectable so tests can fake ``measured-boot``/``dstack-mr``."""

ToolLocator = Callable[[str], str | None]
"""Maps a tool name to its executable path, or ``None`` when it is not installed."""

_Measure = Callable[[str, Path, ToolRunner], dict[str, str]]

TOOL_HINT = "Install measured-boot or dstack-mr and make sure it is on PATH."
SHA384_HEX_LENGTH = 96

_SHA384_HEX = re.compile(rf"[0-9a-f]{{{SHA384_HEX_LENGTH}}}")
_RTMR_KEY = re.compile(r"rtmr(\d+)", re.IGNORECASE)
_MEASURED_BOOT_SUFFIXES = frozenset({".efi", ".raw", ".img"})
_DSTACK_MR_SUFFIXES = frozenset({".efi"})
_OUTPUT_EXCERPT = 200


class _ToolFailed(Exception):
    """The tool exited non-zero for one artifact; another candidate may still work."""


def requirements() -> tuple[Requirement, ...]:
    """The optional RTMR tools, probed by ``tundravm doctor``."""
    return tuple(
        Requirement(
            tool=tool,
            probe=(tool, "--version"),
            hint=f"Real RTMR measurements need it. {TOOL_HINT}",
            optional=True,
        )
        for tool in ("measured-boot", "dstack-mr")
    )


def run_tool(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Default runner: run *argv* with captured text output and no stdin."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def derive(
    profile: str,
    artifact_digests: dict[str, str],
    artifact_paths: tuple[Path, ...] = (),
    *,
    allow_placeholder: bool = False,
    tool_locator: ToolLocator | None = None,
    runner: ToolRunner | None = None,
) -> Measurements:
    """Measure the first artifact a tool accepts; placeholder values only when allowed."""
    locate = tool_locator if tool_locator is not None else shutil.which
    run = runner if runner is not None else run_tool
    candidates = _measurement_candidates(artifact_paths, artifact_digests)
    found: list[str] = []
    failures: list[str] = []
    tools: tuple[tuple[MeasurementSource, frozenset[str], _Measure], ...] = (
        ("measured-boot", _MEASURED_BOOT_SUFFIXES, _measure_with_measured_boot),
        ("dstack-mr", _DSTACK_MR_SUFFIXES, _measure_with_dstack),
    )
    for tool, suffixes, measure in tools:
        tool_path = locate(tool)
        if tool_path is None:
            continue
        found.append(tool)
        for candidate in candidates:
            if candidate.suffix not in suffixes:
                continue
            try:
                values = measure(tool_path, candidate, run)
            except _ToolFailed as failure:
                failures.append(str(failure))
                continue
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{tool} {candidate.name}: {exc}")
                continue
            return Measurements(
                backend="rtmr",
                values=values,
                source=tool,
                tool_version=_tool_version(tool_path, run),
                artifact=str(candidate),
            )

    context = {"profile": profile, "artifacts": ", ".join(c.name for c in candidates)}
    if not found:
        message = "No measurement tool found for rtmr measurements."
        hint = TOOL_HINT
    else:
        message = f"{' and '.join(found)} could not measure any baked artifact."
        hint = (
            "measured-boot reads a UKI (.efi) or raw disk image (.raw/.img); "
            "dstack-mr reads a UKI (.efi). Add such an output target and bake again."
        )
        if failures:
            context["failures"] = "; ".join(failures)
    values = digest_values(
        ("RTMR0", "RTMR1", "RTMR2"),
        salt="",
        profile=profile,
        artifact_digests=artifact_digests,
    )
    return placeholder_measurements(
        "rtmr",
        values,
        allow_placeholder=allow_placeholder,
        message=message,
        hint=hint,
        context=context,
    )


def _measure_with_measured_boot(tool_path: str, artifact: Path, run: ToolRunner) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="tundravm-measure-") as tmp:
        output = Path(tmp) / "measurements.json"
        command = [tool_path, str(artifact), str(output)]
        if artifact.suffix == ".efi":
            command.append("--direct-uki")
        _check_exit("measured-boot", artifact, run(command))
        try:
            text = output.read_text(encoding="utf-8")
        except OSError as exc:
            raise _invalid_output(
                "measured-boot", artifact, "it exited 0 but wrote no output file", str(exc)
            ) from exc
    data = _parse_json_object("measured-boot", artifact, text)
    raw_rtmr = data.get("rtmr")
    if not isinstance(raw_rtmr, dict):
        raise _invalid_output("measured-boot", artifact, 'the output has no "rtmr" object', text)
    values: dict[str, str] = {}
    for index, payload in raw_rtmr.items():
        if not isinstance(payload, dict):
            continue
        expected = payload.get("expected")
        if not isinstance(expected, str):
            continue
        if not str(index).isdigit():
            raise _invalid_output(
                "measured-boot", artifact, f"unexpected RTMR index {index!r}", text
            )
        values[f"RTMR{int(index)}"] = expected
    return _validated("measured-boot", artifact, values)


def _measure_with_dstack(tool_path: str, artifact: Path, run: ToolRunner) -> dict[str, str]:
    result = run([tool_path, str(artifact)])
    _check_exit("dstack-mr", artifact, result)
    data = _parse_json_object("dstack-mr", artifact, result.stdout or "")
    values: dict[str, str] = {}
    for key, value in data.items():
        match = _RTMR_KEY.fullmatch(str(key))
        if match is None:
            continue
        if not isinstance(value, str):
            raise _invalid_output(
                "dstack-mr", artifact, f"{key} is not a string", result.stdout or ""
            )
        values[f"RTMR{int(match.group(1))}"] = value
    return _validated("dstack-mr", artifact, values)


def _check_exit(tool: str, artifact: Path, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    reason = detail[-1] if detail else "no output"
    raise _ToolFailed(f"{tool} {artifact.name}: exit {result.returncode}: {reason}")


def _parse_json_object(tool: str, artifact: Path, text: str) -> dict[str, object]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _invalid_output(tool, artifact, f"the output is not JSON ({exc.msg})", text) from exc
    if not isinstance(data, dict):
        raise _invalid_output(tool, artifact, "the output is not a JSON object", text)
    return data


def _validated(tool: str, artifact: Path, values: Mapping[str, str]) -> dict[str, str]:
    if not values:
        raise _invalid_output(tool, artifact, "the output has no RTMR values", "")
    normalized: dict[str, str] = {}
    for key, value in sorted(values.items()):
        candidate = value.strip().lower()
        if _SHA384_HEX.fullmatch(candidate) is None:
            raise MeasurementError(
                f"{tool} returned an invalid {key} value for {artifact.name}.",
                hint=(
                    f"RTMR values are SHA-384 digests: exactly {SHA384_HEX_LENGTH} hex "
                    f"characters. Check the installed {tool} version."
                ),
                context={
                    "tool": tool,
                    "artifact": str(artifact),
                    "key": key,
                    "value": value[:_OUTPUT_EXCERPT],
                },
            )
        normalized[key] = candidate
    return normalized


def _invalid_output(tool: str, artifact: Path, reason: str, output: str) -> MeasurementError:
    context = {"tool": tool, "artifact": str(artifact)}
    if output.strip():
        context["output"] = output.strip()[:_OUTPUT_EXCERPT]
    return MeasurementError(
        f"{tool} produced unusable output for {artifact.name}: {reason}.",
        hint=f"Run {tool} on the artifact by hand and check the installed version.",
        context=context,
    )


def _tool_version(tool_path: str, run: ToolRunner) -> str | None:
    try:
        result = run([tool_path, "--version"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    if not lines:
        return None
    version = lines[0].strip().removeprefix(Path(tool_path).name).strip()
    return version.removeprefix("version").strip() or None


def _measurement_candidates(
    artifact_paths: tuple[Path, ...],
    artifact_digests: Mapping[str, str],
) -> tuple[Path, ...]:
    seen: set[Path] = set()
    candidates: list[Path] = []

    for path in artifact_paths:
        if path.exists() and path not in seen:
            candidates.append(path)
            seen.add(path)

    for key in artifact_digests:
        candidate = Path(key)
        if candidate.exists() and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    return tuple(candidates)
