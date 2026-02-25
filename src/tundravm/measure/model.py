"""Measurement model, export, and verification helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import cbor2

MismatchReason = Literal["missing_actual", "unexpected_actual", "value_mismatch"]


@dataclass(frozen=True, slots=True)
class MeasurementMismatch:
    key: str
    reason: MismatchReason
    expected: str | None
    actual: str | None
    hint: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    mismatches: tuple[MeasurementMismatch, ...] = ()


@dataclass(frozen=True, slots=True)
class Measurements:
    backend: Literal["rtmr", "azure", "gcp"]
    values: dict[str, str] = field(default_factory=dict)
    schema_version: int = 1

    def to_json(self, path: str | Path | None = None) -> str:
        payload = self._payload()
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if path is not None:
            Path(path).write_text(encoded, encoding="utf-8")
        return encoded

    def to_cbor(self, path: str | Path | None = None) -> bytes:
        payload = self._payload()
        encoded = cbor2.dumps(payload, canonical=True)
        if path is not None:
            Path(path).write_bytes(encoded)
        return encoded

    def verify(self, expected: dict[str, str]) -> VerificationResult:
        mismatches: list[MeasurementMismatch] = []
        for key, expected_value in sorted(expected.items()):
            if key not in self.values:
                mismatches.append(
                    MeasurementMismatch(
                        key=key,
                        reason="missing_actual",
                        expected=expected_value,
                        actual=None,
                        hint="Measured values are missing this key.",
                    ),
                )
                continue
            actual_value = self.values[key]
            if actual_value != expected_value:
                mismatches.append(
                    MeasurementMismatch(
                        key=key,
                        reason="value_mismatch",
                        expected=expected_value,
                        actual=actual_value,
                        hint=(
                            "Rebuild image and verify measurement backend/inputs match "
                            "expected digest set."
                        ),
                    ),
                )

        for key, actual_value in sorted(self.values.items()):
            if key in expected:
                continue
            mismatches.append(
                MeasurementMismatch(
                    key=key,
                    reason="unexpected_actual",
                    expected=None,
                    actual=actual_value,
                    hint="Expected set does not include this measured key.",
                ),
            )

        return VerificationResult(ok=not mismatches, mismatches=tuple(mismatches))

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "backend": self.backend,
            "values": dict(sorted(self.values.items())),
        }
