"""Structured logging and observability helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StructuredLogger:
    records: list[dict[str, Any]] = field(default_factory=list)

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

    def records_for_profile(self, profile: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record.get("profile") == profile]

    def to_json_lines(self, path: str | Path) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record, sort_keys=True) for record in self.records]
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path
