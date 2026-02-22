"""Shared artifact materialization helpers for language builders."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tdx.builders.base import BuildArtifact, BuildSpec


def materialize_artifact(
    *,
    builder_name: str,
    command: tuple[str, ...],
    spec: BuildSpec,
) -> BuildArtifact:
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{spec.name}-{spec.target}.bin"
    output_path = spec.output_dir / output_name
    metadata_path = spec.output_dir / f"{output_name}.json"

    output_payload = (
        f"builder={builder_name}\n"
        f"source={spec.source}\n"
        f"target={spec.target}\n"
        f"reproducible={spec.reproducible}\n"
        f"command={' '.join(command)}\n"
    )
    output_path.write_text(output_payload, encoding="utf-8")

    metadata = {
        "builder": builder_name,
        "source": str(spec.source),
        "target": spec.target,
        "reproducible": spec.reproducible,
        "flags": list(spec.flags),
        "env": dict(sorted(spec.env.items())),
        "command": list(command),
        "output_path": str(output_path),
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    installed_path: Path | None = None
    if spec.install_to is not None:
        installed_path = spec.install_to
        installed_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_path, installed_path)

    return BuildArtifact(
        builder=builder_name,
        target=spec.target,
        output_path=output_path,
        installed_path=installed_path,
        metadata_path=metadata_path,
    )
