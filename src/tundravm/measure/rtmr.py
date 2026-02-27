"""RTMR measurement derivation.

Uses the `measured-boot` tool when available to predict RTMR register
values from UKI or disk-image artifacts. Falls back to deterministic
SHA-256 derivation from artifact digests when the tool is not available.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def derive(
    profile: str,
    artifact_digests: dict[str, str],
    artifact_paths: tuple[Path, ...] = (),
) -> dict[str, str]:
    """Derive RTMR values from artifact digests."""
    measured_boot = shutil.which("measured-boot")
    if measured_boot is not None:
        for candidate in _measurement_candidates(artifact_paths, artifact_digests):
            values = _measure_with_tool(measured_boot, candidate)
            if values:
                return values

    dstack_mr = shutil.which("dstack-mr")
    if dstack_mr is not None:
        for candidate in _measurement_candidates(artifact_paths, artifact_digests):
            if candidate.suffix == ".efi":
                values = _measure_with_dstack(dstack_mr, candidate)
                if values:
                    return values

    return _derive_deterministic(profile, artifact_digests)


def _measure_with_tool(tool_path: str, artifact: Path) -> dict[str, str]:
    if artifact.suffix not in {".efi", ".raw", ".img"}:
        return {}

    command = [tool_path, str(artifact)]

    with tempfile.NamedTemporaryFile(suffix=".json") as output_file:
        command.append(output_file.name)
        if artifact.suffix == ".efi":
            command.append("--direct-uki")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return {}
        try:
            data = json.loads(Path(output_file.name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return _extract_measured_boot_rtmrs(data)


def _extract_measured_boot_rtmrs(data: dict[str, object]) -> dict[str, str]:
    raw_rtmr = data.get("rtmr")
    if not isinstance(raw_rtmr, dict):
        return {}

    values: dict[str, str] = {}
    for index, payload in raw_rtmr.items():
        if not isinstance(payload, dict):
            continue
        expected = payload.get("expected")
        if isinstance(expected, str):
            values[f"RTMR{index}"] = expected
    return values


def _measure_with_dstack(tool_path: str, artifact: Path) -> dict[str, str]:
    result = subprocess.run(
        [tool_path, str(artifact)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
        return {k: v for k, v in data.items() if k.startswith("RTMR")}
    except (json.JSONDecodeError, KeyError):
        return {}


def _derive_deterministic(profile: str, artifact_digests: dict[str, str]) -> dict[str, str]:
    digest_payload = "|".join(f"{key}:{value}" for key, value in sorted(artifact_digests.items()))
    return {
        "RTMR0": _sha256(digest_payload),
        "RTMR1": _sha256(f"profile:{profile}"),
        "RTMR2": _sha256(f"targets:{','.join(sorted(artifact_digests))}"),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _measurement_candidates(
    artifact_paths: tuple[Path, ...],
    artifact_digests: dict[str, str],
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
