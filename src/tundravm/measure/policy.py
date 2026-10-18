"""Verifier policy files: the TDX registers a quote may hold, handed from ``measure`` to verifiers.

``tundravm measure --export-policy FILE`` writes one (``schema_version`` 2,
``tundravm.policy/2``); ``Tdxs.from_policy``, ``attest``, ``evidence`` and ``verify``
read it. ``registers`` maps ``MRTD``/``RTMR0``..``RTMR3`` to the list of values a
genuine quote may hold: a hex string, or ``{"value": hex, "firmware": ..., "label": ...}``
naming what the value belongs to. A register matches when the quote holds any accepted
value. ``unverifiable`` maps each of ``MRTD``/``RTMR0``..``RTMR2`` the scheme cannot
predict offline to the reason; every one of them is either accepted or unverifiable.
``scheme``, ``tool``/``tool_version``, ``artifact``/``boot_payload`` (``path``,
``sha256``), ``sources`` (where firmware values come from) and, for placeholders, a
``note`` complete it. ``platform_approval`` (optional) maps ``MRTD``/``RTMR0`` to values
the operator approves for the platform when the scheme cannot predict them (qemu,
portable): ``attest`` trusts a quote only when MRTD, RTMR0, RTMR1 and RTMR2 all match.
Schema 1 files (one value per register) are refused.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tundravm.errors import MeasurementError, ValidationError

POLICY_SCHEMA_VERSION = 2
POLICY_SCHEMA = "tundravm.policy/2"
SCHEMES = ("qemu", "gcp", "azure", "portable")
REGISTERS = ("MRTD", "RTMR0", "RTMR1", "RTMR2", "RTMR3")
COVERED = ("MRTD", "RTMR0", "RTMR1", "RTMR2")
"""Registers a policy must either accept values for or declare unverifiable."""
PLATFORM_REGISTERS = ("MRTD", "RTMR0")
"""The TD firmware (TDVF) and its configuration: approved by the scheme (gcp) or the operator
(``platform_approval``); a quote is trusted only when both match an approved value."""
IMAGE_REGISTERS = ("RTMR1", "RTMR2")
"""The image's boot path; a quote is trusted only when both match a measured value."""
APPROVED_LABEL = "operator-approved"
"""The ``label`` an unlabelled ``platform_approval`` value carries once merged."""
PLACEHOLDER_TOOL = "placeholder"
PLACEHOLDER_NOTE = (
    "PLACEHOLDER: these registers are derived from artifact digests, not measured; "
    "no TEE reproduces them. Replace this file with `tundravm measure --export-policy` "
    "output from a real bake before trusting a verifier built from it."
)
REEXPORT_HINT = "Export the policy again: `tundravm measure --scheme SCHEME --export-policy FILE`."

_HEX = re.compile(r"[0-9a-f]+")
_SHA384_HEX = 96
_PLACEHOLDER_HEX = 64
_LABELS = ("firmware", "label")


@dataclass(frozen=True, slots=True)
class Accepted:
    """One accepted register value and what it belongs to (a firmware, a machine type)."""

    value: str
    firmware: str | None = None
    label: str | None = None

    def describe(self) -> str | None:
        """``firmware label`` joined, ``None`` for an unlabelled value."""
        parts = [p for p in (self.firmware, self.label) if p]
        return " ".join(parts) or None


def accepted(registers: Mapping[str, object], name: str) -> tuple[Accepted, ...]:
    """The accepted values of register *name* in a checked policy's ``registers``."""
    entries = registers.get(name)
    if not isinstance(entries, list):
        return ()
    found = []
    for entry in entries:
        if isinstance(entry, str):
            found.append(Accepted(entry))
        elif isinstance(entry, Mapping):
            found.append(
                Accepted(
                    str(entry["value"]),
                    firmware=_text(entry.get("firmware")),
                    label=_text(entry.get("label")),
                )
            )
    return tuple(found)


def match_registers(
    registers: Mapping[str, object], actual: Mapping[str, str | None]
) -> dict[str, tuple[str, Accepted | None]]:
    """Per register of :data:`REGISTERS`: ``(verdict, matched value)``; the verdict is
    ``match``, ``mismatch`` or ``unchecked`` (the policy accepts no value for it).

    Values labelled with a ``firmware`` pair up: MRTD and RTMR0 matching values of two
    different firmwares is a mismatch of RTMR0.
    """
    found: dict[str, tuple[str, Accepted | None]] = {}
    for name in REGISTERS:
        values = accepted(registers, name)
        if not values:
            found[name] = ("unchecked", None)
            continue
        hit = next((a for a in values if a.value == actual.get(name)), None)
        found[name] = ("match" if hit else "mismatch", hit)
    mrtd, rtmr0 = found["MRTD"][1], found["RTMR0"][1]
    if mrtd and rtmr0 and mrtd.firmware and rtmr0.firmware and mrtd.firmware != rtmr0.firmware:
        found["RTMR0"] = ("mismatch", None)
    return found


def single_values(registers: Mapping[str, object]) -> dict[str, str]:
    """Registers with exactly one accepted value, as ``{name: value}``."""
    return {
        name: values[0].value for name in registers if len(values := accepted(registers, name)) == 1
    }


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def policy_payload(
    *,
    scheme: str,
    tool: str,
    registers: Mapping[str, Sequence[str | Mapping[str, str]]],
    artifact_path: str,
    artifact_sha256: str,
    unverifiable: Mapping[str, str] | None = None,
    boot_payload: Mapping[str, str] | None = None,
    extra: Mapping[str, object] | None = None,
    allow_placeholder: bool = False,
    platform_approval: Mapping[str, Sequence[str | Mapping[str, str]]] | None = None,
) -> dict[str, object]:
    """The normalised policy for one measured artifact; raises on placeholders or gaps.

    *tool* is ``Measurements.tool`` (``"tundravm 0.9"``: name, then version).
    *extra* adds scheme fields (``sources``, ``firmware_table``, ``vtpm_pcrs``).
    *platform_approval* maps ``MRTD``/``RTMR0`` to the values the operator approves for
    the platform (taken from a quote of a host they trust): kept apart from the measured
    ``registers`` as ``platform_approval``.
    """
    name, _, version = tool.partition(" ")
    placeholder = name == PLACEHOLDER_TOOL
    if placeholder and not allow_placeholder:
        raise MeasurementError(
            "Refusing to export placeholder measurements as a verifier policy.",
            hint="Measure a real bake, or pass --allow-placeholder for a test policy "
            "(marked as a placeholder in its note).",
        )
    payload: dict[str, object] = {
        **dict(extra or {}),
        "schema_version": POLICY_SCHEMA_VERSION,
        "schema": POLICY_SCHEMA,
        "scheme": scheme,
        "tool": name,
        "tool_version": version or None,
        "artifact": {"path": artifact_path, "sha256": artifact_sha256},
        "registers": {k: list(v) for k, v in registers.items()},
        "unverifiable": dict(unverifiable or {}),
    }
    if platform_approval:
        payload["platform_approval"] = {k: list(v) for k, v in platform_approval.items()}
    if boot_payload is not None:
        payload["boot_payload"] = {"path": boot_payload["path"], "sha256": boot_payload["sha256"]}
    if placeholder:
        payload["note"] = PLACEHOLDER_NOTE
    return _checked(payload, where="the measurements")


def write_policy(payload: Mapping[str, object], path: Path) -> None:
    """Write *payload* as sorted, indented JSON with a trailing newline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_policy(source: str | Path | Mapping[str, object]) -> dict[str, object]:
    """A policy file (or its parsed dict), checked: schema, scheme, registers, unverifiable."""
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
                hint="Write one with `tundravm measure --export-policy FILE`.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Policy {source} is not JSON ({exc.msg}).",
                hint="Write one with `tundravm measure --export-policy FILE`.",
            ) from exc
    if not isinstance(data, dict):
        raise ValidationError(
            f"{where} is not a JSON object.", hint="Pass a file `--export-policy` wrote."
        )
    return _checked(data, where=where)


def is_placeholder(policy: Mapping[str, object]) -> bool:
    return policy.get("tool") == PLACEHOLDER_TOOL


def unverifiable(policy: Mapping[str, object]) -> dict[str, str]:
    """The registers a checked policy cannot predict, with the reason."""
    raw = policy.get("unverifiable")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def _checked(data: dict[str, object], *, where: str) -> dict[str, object]:
    version = data.get("schema_version")
    if version != POLICY_SCHEMA_VERSION:
        old = " (one value per register, no longer read)" if version == 1 else ""
        raise ValidationError(
            f"{where} has schema_version {version!r}{old}; this tundravm reads "
            f"{POLICY_SCHEMA_VERSION} ({POLICY_SCHEMA}).",
            hint=REEXPORT_HINT,
        )
    if data.get("scheme") not in SCHEMES:
        raise ValidationError(
            f"{where} holds {data.get('scheme')!r} measurements.",
            hint=f"Schemes: {', '.join(SCHEMES)}. {REEXPORT_HINT}",
        )
    raw = data.get("registers")
    gaps = data.get("unverifiable", {})
    if not isinstance(raw, dict) or not isinstance(gaps, dict):
        raise ValidationError(f"{where} has no registers/unverifiable objects.", hint=REEXPORT_HINT)
    placeholder = data.get("tool") == PLACEHOLDER_TOOL
    registers = {
        str(k).upper(): _entries(str(k).upper(), v, placeholder=placeholder, where=where)
        for k, v in raw.items()
        if str(k).upper() in REGISTERS
    }
    reasons = {str(k).upper(): str(v) for k, v in gaps.items() if str(k).upper() in REGISTERS}
    missing = [n for n in COVERED if n not in registers and n not in reasons]
    both = [n for n in reasons if n in registers]
    if missing or both:
        raise ValidationError(
            f"{where}: register(s) {', '.join(missing or both)} are "
            + ("neither accepted nor unverifiable." if missing else "accepted and unverifiable."),
            hint=f"Each of {', '.join(COVERED)} needs accepted values or an unverifiable "
            f"reason. {REEXPORT_HINT}",
        )
    data["registers"] = dict(sorted(registers.items()))
    data["unverifiable"] = dict(sorted(reasons.items()))
    if "platform_approval" in data:
        data["platform_approval"] = _approval(data["platform_approval"], where=where)
    return data


def _approval(raw: object, *, where: str) -> dict[str, list[str | dict[str, str]]]:
    """A ``platform_approval`` checked: ``MRTD``/``RTMR0`` to non-empty lists of values."""
    hint = (
        "platform_approval maps MRTD and/or RTMR0 to the values you approve, e.g. "
        "`tundravm measure --export-policy FILE --approve-mrtd HEX --approve-rtmr0 HEX`."
    )
    if not isinstance(raw, dict) or not raw:
        raise ValidationError(f"{where}: platform_approval is not a non-empty object.", hint=hint)
    unknown = sorted(str(k) for k in raw if str(k).upper() not in PLATFORM_REGISTERS)
    if unknown:
        raise ValidationError(
            f"{where}: platform_approval names {', '.join(unknown)}; it approves only "
            f"{' and '.join(PLATFORM_REGISTERS)}.",
            hint=hint,
        )
    return {
        str(k).upper(): _entries(str(k).upper(), v, placeholder=False, where=where)
        for k, v in sorted(raw.items())
    }


def approved_registers(policy: Mapping[str, object]) -> dict[str, list[str | dict[str, str]]]:
    """A checked policy's ``registers`` with its ``platform_approval`` values added: what a
    quote is matched against. An unlabelled approved value is labelled
    :data:`APPROVED_LABEL`."""
    raw = policy.get("registers")
    merged: dict[str, list[str | dict[str, str]]] = (
        {str(k): list(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}
    )
    approval = policy.get("platform_approval")
    if isinstance(approval, Mapping):
        for name, entries in approval.items():
            extra = [
                {"value": e, "label": APPROVED_LABEL}
                if isinstance(e, str)
                else {**e, "label": e.get("label") or APPROVED_LABEL}
                for e in entries
            ]
            merged[str(name)] = [*merged.get(str(name), []), *extra]
    return merged


def _entries(
    name: str, values: object, *, placeholder: bool, where: str
) -> list[str | dict[str, str]]:
    if not isinstance(values, list) or not values:
        raise ValidationError(
            f"{where}: {name} is not a non-empty list of accepted values.", hint=REEXPORT_HINT
        )
    found: list[str | dict[str, str]] = []
    for entry in values:
        if isinstance(entry, Mapping):
            labels = {k: str(entry[k]) for k in _LABELS if entry.get(k)}
            value = _hex(name, entry.get("value"), placeholder=placeholder, where=where)
            found.append({"value": value, **labels})
        else:
            found.append(_hex(name, entry, placeholder=placeholder, where=where))
    return found


def _hex(name: str, value: object, *, placeholder: bool, where: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    lengths = (_SHA384_HEX, _PLACEHOLDER_HEX) if placeholder else (_SHA384_HEX,)
    if not _HEX.fullmatch(text) or len(text) not in lengths:
        raise ValidationError(
            f"{where}: {name} is not a SHA-384 digest.",
            hint=f"Registers are {_SHA384_HEX} hex characters.",
        )
    return text


def validator_measurements(
    registers: Mapping[str, object],
    *,
    mrtd: str | None = None,
    firmware: str | None = None,
    label: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """*registers* as the tdxs validator's ``expected_measurements`` keys (``rtmr0``,
    ``mrtd``), one value per register: the tdxs validator compares each register with a
    single expected value.

    A register with several accepted values (a gcp policy lists one MRTD per firmware and
    one RTMR0 per firmware and machine type) is narrowed to the values labelled
    *firmware* and *label* (unlabelled values always stay); if more than one remains,
    ``MeasurementError`` names the choices. MRTD is checked only when the policy accepts
    values for it or *mrtd* supplies one.
    """
    keyed: dict[str, str] = {}
    for name in REGISTERS:
        values = tuple(
            a
            for a in accepted(registers, name)
            if (firmware is None or a.firmware in (None, firmware))
            and (label is None or a.label in (None, label))
        )
        if not values:
            continue
        distinct = sorted({a.value for a in values})
        if len(distinct) > 1:
            choices = sorted({a.describe() or a.value[:12] for a in values})
            raise MeasurementError(
                f"The tdxs validator checks one value per register, and {name} has "
                f"{len(distinct)} accepted values ({', '.join(choices[:6])}"
                f"{', ...' if len(choices) > 6 else ''}).",
                hint="Pick the deployment's firmware and machine type: "
                "Tdxs.from_policy(policy, firmware='...', label='...'), "
                "or verify the quote with `tundravm attest`, which accepts any listed value.",
                context={"register": name},
            )
        keyed[name.lower()] = distinct[0]
    if mrtd is not None:
        keyed["mrtd"] = _hex("MRTD", mrtd, placeholder=False, where="mrtd=")
    return tuple(sorted(keyed.items()))


__all__ = [
    "APPROVED_LABEL",
    "COVERED",
    "IMAGE_REGISTERS",
    "PLACEHOLDER_NOTE",
    "PLATFORM_REGISTERS",
    "POLICY_SCHEMA",
    "POLICY_SCHEMA_VERSION",
    "REGISTERS",
    "SCHEMES",
    "Accepted",
    "accepted",
    "approved_registers",
    "is_placeholder",
    "match_registers",
    "policy_payload",
    "read_policy",
    "single_values",
    "unverifiable",
    "validator_measurements",
    "write_policy",
]
