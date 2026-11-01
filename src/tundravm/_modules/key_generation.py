"""Key generation module."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Literal

from tundravm._modules.base import Module, tools_source
from tundravm._source import GitSource, GoBuild, Install, LocalSource, SourceBuild
from tundravm.errors import ValidationError

KEY_GENERATION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

KEY_GENERATION_DEFAULT_CONFIG_PATH = "/etc/tdx/key-gen.yaml"
ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_entry_name(name: str, *, kind: str) -> None:
    """Reject empty names and names outside ``[A-Za-z0-9_.-]``."""
    if not name:
        raise ValidationError(
            f"{kind} names must be non-empty.",
            hint="Use only letters, numbers, dot, underscore, and dash.",
        )
    if ENTRY_NAME_PATTERN.fullmatch(name) is None:
        raise ValidationError(
            f"Invalid {kind} name {name!r}.",
            hint="Use only letters, numbers, dot, underscore, and dash.",
        )


@dataclass(frozen=True, slots=True)
class KeySpec:
    """A cryptographic key generated at boot time.

    ``strategy='pipe'`` reads the key from ``pipe_path``; ``'random'`` generates
    ``size`` bytes; ``'tpm'`` is ``'random'`` persisted in the TPM. ``output`` is
    where the key is written (what ``DiskSpec(key=...)`` reads).
    """

    name: str
    strategy: Literal["tpm", "random", "pipe"] = field(default="tpm", kw_only=True)
    output: str | None = field(default=None, kw_only=True)
    size: int = field(default=64, kw_only=True)
    pipe_path: str | None = field(default=None, kw_only=True)
    persist_in_tpm: bool | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        validate_entry_name(self.name, kind="key")
        if self.strategy == "pipe" and not self.pipe_path:
            raise ValidationError(
                "pipe strategy requires pipe_path.",
                hint="Unlock the volume with OperatorKey(), BrokerKey(...) or TpmSeal(...).",
                context={"key": self.name},
            )
        if self.strategy != "pipe" and self.pipe_path is not None:
            raise ValidationError(
                "pipe_path is only valid with strategy='pipe'.",
                hint="Drop pipe=, or pass strategy='pipe'.",
                context={"key": self.name, "strategy": self.strategy},
            )

    def tool_strategy(self) -> Literal["random", "pipe"]:
        if self.strategy == "pipe":
            return "pipe"
        return "random"

    def tpm_enabled(self) -> bool:
        if self.persist_in_tpm is not None:
            return self.persist_in_tpm
        return self.strategy == "tpm"


@dataclass(slots=True)
class KeyGeneration(Module):
    """Generate one or more cryptographic keys at boot time.

    The underlying ``key-gen`` binary supports the ``random`` and ``pipe``
    strategies, with TPM persistence controlled separately. ``strategy='tpm'``
    is kept as a compatibility alias for ``random`` with TPM persistence.
    """

    keys: tuple[KeySpec, ...]
    config_path: str = KEY_GENERATION_DEFAULT_CONFIG_PATH
    source: GitSource | LocalSource = field(default_factory=tools_source)

    def __post_init__(self) -> None:
        self.keys = tuple(self.keys)
        if not self.keys:
            raise ValidationError(
                "volume key generation has no volume to make a key for.",
                hint='Keep state on a Volume("data", unlock=OperatorKey()).',
            )
        names: set[str] = set()
        output_paths: set[str] = set()
        for spec in self.keys:
            if spec.name in names:
                raise ValidationError(
                    f"Duplicate key name {spec.name!r}.",
                    hint="Give each Volume its own name.",
                )
            names.add(spec.name)
            if spec.output is not None:
                if spec.output in output_paths:
                    raise ValidationError(
                        "two volume keys are written to one path.",
                        hint="Give each Volume its own name.",
                        context={"key": spec.name, "path": spec.output},
                    )
                output_paths.add(spec.output)

    def source_spec(self) -> SourceBuild:
        """The ``key-gen`` source build from ``source``."""
        return SourceBuild(
            name="key-generation",
            source=self.source,
            build=GoBuild(package="./cmd/key-gen", output="key-gen"),
            install=(Install.artifact("/usr/bin/key-gen"),),
        )

    def init_script(self) -> str:
        """The ``keys`` runtime-init step: ``key-gen setup`` on the config."""
        return f"/usr/bin/key-gen setup {shlex.quote(self.config_path)}\n"

    def render_config(self) -> str:
        """The aggregate ``key-gen`` config: one entry per key."""
        lines = ["keys:"]
        for spec in self.keys:
            lines.extend(
                (
                    f"  {spec.name}:",
                    f'    strategy: "{spec.tool_strategy()}"',
                    f"    tpm: {'true' if spec.tpm_enabled() else 'false'}",
                )
            )
            if spec.tool_strategy() == "random":
                lines.append(f"    size: {spec.size}")
            elif spec.pipe_path:
                lines.append(f'    pipe_path: "{spec.pipe_path}"')
            if spec.output is not None:
                lines.append(f'    output_path: "{spec.output}"')
        return "\n".join(lines) + "\n"
