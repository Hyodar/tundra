"""Go builder."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.builders.base import BuildArtifact, BuildSpec
from tdx.builders.materialize import materialize_artifact


@dataclass(slots=True)
class GoBuilder:
    tool: str = "go"

    def build(self, spec: BuildSpec) -> BuildArtifact:
        flags = list(spec.flags)
        if spec.reproducible and "-trimpath" not in flags:
            flags.append("-trimpath")
        command = (self.tool, "build", *flags, str(spec.source))
        return materialize_artifact(builder_name="go", command=command, spec=spec)
