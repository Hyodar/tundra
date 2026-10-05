"""Verifier policy files: RTMR measurements handed from ``measure`` to ``Tdxs``.

``tundravm measure --export-policy FILE`` writes one; ``Tdxs.from_policy``
reads it into the ``expected_measurements`` of a ``tdxs`` validator. The file
is JSON with ``schema_version``, ``scheme``, ``tool``, ``tool_version``,
``artifact`` (``path``, ``sha256``), ``registers`` (``RTMR0``..``RTMR2``
required, ``RTMR3`` and ``MRTD`` optional) and, for placeholder values, a
``note`` saying so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from tundravm.errors import MeasurementError, ValidationError

POLICY_SCHEMA_VERSION = 1
REQUIRED_REGISTERS = ("RTMR0", "RTMR1", "RTMR2")
OPTIONAL_REGISTERS = ("RTMR3", "MRTD")
PLACEHOLDER_TOOL = "placeholder"
PLACEHOLDER_NOTE = (
    "PLACEHOLDER: these registers are derived from artifact digests, not measured; "
    "no TEE reproduces them. Replace this file with `tundravm measure --export-policy` "
    "output from a real bake before trusting a verifier built from it."
)

_HEX = re.compile(r"[0-9a-f]+")
_SHA384_HEX = 96
_PLACEHOLDER_HEX = 64


def policy_payload(
    *,
    scheme: str,
    tool: str,
    values: Mapping[str, str],
    artifact_path: str,
    artifact_sha256: str,
    allow_placeholder: bool = False,
) -> dict[str, object]:
    """The normalised policy for one measured artifact; raises on placeholders or gaps.

    *tool* is ``Measurements.tool`` (``"measured-boot 1.2"``: name, then version).
    """
    if scheme != "rtmr":
        raise ValidationError(
            f"A verifier policy needs rtmr measurements, not {scheme}.",
            hint="Run `tundravm measure ... --scheme rtmr --export-policy FILE`.",
        )
    name, _, version = tool.partition(" ")
    placeholder = name == PLACEHOLDER_TOOL
    if placeholder and not allow_placeholder:
        raise MeasurementError(
            "Refusing to export placeholder measurements as a verifier policy.",
            hint="Measure with measured-boot or dstack-mr, or pass --allow-placeholder "
            "for a test policy (marked as a placeholder in its note).",
        )
    registers = _registers(values, placeholder=placeholder, where="the measurements")
    payload: dict[str, object] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "scheme": scheme,
        "tool": name,
        "tool_version": version or None,
        "artifact": {"path": artifact_path, "sha256": artifact_sha256},
        "registers": registers,
    }
    if placeholder:
        payload["note"] = PLACEHOLDER_NOTE
    return payload


def write_policy(payload: Mapping[str, object], path: Path) -> None:
    """Write *payload* as sorted, indented JSON with a trailing newline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_policy(source: str | Path | Mapping[str, object]) -> dict[str, object]:
    """A policy file (or its parsed dict), checked: schema version, scheme and registers."""
    where = "the policy"
    if isinstance(source, Mapping):
        data: object = dict(source)
    else:
        where = str(source)
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValidationError(
                f"Cannot read policy {source}: {exc.strerror or exc}.",
                hint="Write one with `tundravm measure MANIFEST --export-policy FILE`.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Policy {source} is not JSON ({exc.msg}).",
                hint="Write one with `tundravm measure MANIFEST --export-policy FILE`.",
            ) from exc
    if not isinstance(data, dict):
        raise ValidationError(
            f"{where} is not a JSON object.", hint="Pass a file `--export-policy` wrote."
        )
    version = data.get("schema_version")
    if version != POLICY_SCHEMA_VERSION:
        raise ValidationError(
            f"{where} has schema_version {version!r}; this tundravm reads {POLICY_SCHEMA_VERSION}.",
            hint="Export the policy again with this tundravm version.",
        )
    if data.get("scheme") != "rtmr":
        raise ValidationError(
            f"{where} holds {data.get('scheme')!r} measurements, not rtmr.",
            hint="Export the policy with --scheme rtmr.",
        )
    raw = data.get("registers")
    if not isinstance(raw, dict):
        raise ValidationError(
            f"{where} has no registers object.", hint="Pass a file `--export-policy` wrote."
        )
    placeholder = data.get("tool") == PLACEHOLDER_TOOL
    data["registers"] = _registers(raw, placeholder=placeholder, where=where)
    return data


def is_placeholder(policy: Mapping[str, object]) -> bool:
    return policy.get("tool") == PLACEHOLDER_TOOL


def _registers(values: Mapping[str, object], *, placeholder: bool, where: str) -> dict[str, str]:
    """*values* with upper-case names and lower-case hex; required registers must be there."""
    found: dict[str, str] = {}
    for key, value in values.items():
        name = str(key).upper()
        if name not in (*REQUIRED_REGISTERS, *OPTIONAL_REGISTERS):
            continue
        found[name] = _hex(name, value, placeholder=placeholder, where=where)
    missing = [name for name in REQUIRED_REGISTERS if name not in found]
    if missing:
        raise ValidationError(
            f"{where} lacks register(s) {', '.join(missing)}.",
            hint=f"A verifier policy needs {', '.join(REQUIRED_REGISTERS)}; measure an "
            "artifact the tool can read (a UKI .efi or a raw disk image).",
        )
    return dict(sorted(found.items()))


def _hex(name: str, value: object, *, placeholder: bool, where: str) -> str:
    text = str(value).strip().lower() if isinstance(value, str) else ""
    lengths = (_SHA384_HEX, _PLACEHOLDER_HEX) if placeholder else (_SHA384_HEX,)
    if not _HEX.fullmatch(text) or len(text) not in lengths:
        raise ValidationError(
            f"{where}: {name} is not a SHA-384 digest.",
            hint=f"Registers are {_SHA384_HEX} hex characters.",
        )
    return text


def validator_measurements(
    registers: Mapping[str, str], *, mrtd: str | None = None
) -> tuple[tuple[str, str], ...]:
    """*registers* as the tdxs validator's ``expected_measurements`` keys (``rtmr0``, ``mrtd``).

    The validator compares only the registers it is given, so MRTD is checked only
    when the policy holds it or *mrtd* supplies it (RTMR tools do not report MRTD).
    """
    keyed = {name.lower(): value for name, value in registers.items()}
    if mrtd is not None:
        keyed["mrtd"] = _hex("MRTD", mrtd, placeholder=False, where="mrtd=")
    return tuple(sorted(keyed.items()))


__all__ = [
    "OPTIONAL_REGISTERS",
    "PLACEHOLDER_NOTE",
    "POLICY_SCHEMA_VERSION",
    "REQUIRED_REGISTERS",
    "is_placeholder",
    "policy_payload",
    "read_policy",
    "validator_measurements",
    "write_policy",
]
