"""C/C++ builder."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.builders.base import BuildArtifact, BuildSpec
from tdx.builders.materialize import materialize_artifact


@dataclass(slots=True)
class CBuilder:
    tool: str = "cc"

    def build(self, spec: BuildSpec) -> BuildArtifact:
        flags = list(spec.flags)
        if spec.reproducible and "-ffile-prefix-map=.=." not in flags:
            flags.append("-ffile-prefix-map=.=.")
        command = (self.tool, str(spec.source), "-o", f"{spec.name}.bin", *flags)
        return materialize_artifact(builder_name="c", command=command, spec=spec)
