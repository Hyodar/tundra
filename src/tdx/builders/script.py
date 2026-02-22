"""Script-based fallback builder."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.builders.base import BuildArtifact, BuildSpec
from tdx.builders.materialize import materialize_artifact


@dataclass(slots=True)
class ScriptBuilder:
    shell: str = "bash"

    def build(self, spec: BuildSpec) -> BuildArtifact:
        flags = list(spec.flags)
        command = (self.shell, str(spec.source), *flags)
        return materialize_artifact(builder_name="script", command=command, spec=spec)
