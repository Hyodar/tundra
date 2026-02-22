"""Rust builder."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.builders.base import BuildArtifact, BuildSpec
from tdx.builders.materialize import materialize_artifact


@dataclass(slots=True)
class RustBuilder:
    tool: str = "cargo"

    def build(self, spec: BuildSpec) -> BuildArtifact:
        flags = list(spec.flags)
        if spec.reproducible and "--locked" not in flags:
            flags.append("--locked")
        command = (
            self.tool,
            "build",
            *flags,
            "--target",
            spec.target,
            "--manifest-path",
            str(spec.source),
        )
        return materialize_artifact(builder_name="rust", command=command, spec=spec)
