"""Dotnet builder."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.builders.base import BuildArtifact, BuildSpec
from tdx.builders.materialize import materialize_artifact


@dataclass(slots=True)
class DotNetBuilder:
    tool: str = "dotnet"

    def build(self, spec: BuildSpec) -> BuildArtifact:
        flags = list(spec.flags)
        if spec.reproducible and "/p:ContinuousIntegrationBuild=true" not in flags:
            flags.append("/p:ContinuousIntegrationBuild=true")
        command = (
            self.tool,
            "publish",
            str(spec.source),
            *flags,
            "--runtime",
            _runtime_for_target(spec.target),
        )
        return materialize_artifact(builder_name="dotnet", command=command, spec=spec)


def _runtime_for_target(target: str) -> str:
    mapping = {
        "x86_64": "linux-x64",
        "aarch64": "linux-arm64",
    }
    return mapping[target]
