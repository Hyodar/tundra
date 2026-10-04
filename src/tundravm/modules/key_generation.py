"""Key generation module."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tundravm.check import Diagnostic
from tundravm.errors import ValidationError
from tundravm.modules.base import TUNDRA_TOOLS, Module
from tundravm.source import GitSource, GoBuild, Install, SourceBuild

if TYPE_CHECKING:
    from tundravm.image import Image

KEY_GENERATION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

KEY_GENERATION_DEFAULT_CONFIG_PATH = "/etc/tdx/key-gen.yaml"
KEY_GENERATION_INIT_PRIORITY = 10
ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_entry_name(name: str, *, kind: str) -> None:
    """Reject empty names and names outside ``[A-Za-z0-9_.-]``."""
    if not name:
        raise ValidationError(f"{kind} names must be non-empty.")
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
                context={"key": self.name},
            )
        if self.strategy != "pipe" and self.pipe_path is not None:
            raise ValidationError(
                "pipe_path is only valid with strategy='pipe'.",
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

    The underlying ``tundra-tools`` binary supports the ``random`` and ``pipe``
    strategies, with TPM persistence controlled separately. ``strategy='tpm'``
    is kept as a compatibility alias for ``random`` with TPM persistence.
    """

    keys: tuple[KeySpec, ...]
    config_path: str = KEY_GENERATION_DEFAULT_CONFIG_PATH
    source: GitSource = TUNDRA_TOOLS

    def __post_init__(self) -> None:
        self.keys = tuple(self.keys)
        if not self.keys:
            raise ValidationError("KeyGeneration requires at least one key definition.")
        names: set[str] = set()
        output_paths: set[str] = set()
        for spec in self.keys:
            if spec.name in names:
                raise ValidationError(f"Duplicate key name {spec.name!r}.")
            names.add(spec.name)
            if spec.output is not None:
                if spec.output in output_paths:
                    raise ValidationError(
                        "Each generated key output path must be unique.",
                        context={"key": spec.name, "path": spec.output},
                    )
                output_paths.add(spec.output)

    def configure(self, image: Image) -> None:
        """Build key-gen, write the aggregate key config, run it at boot (priority 10)."""
        image.build_packages(*KEY_GENERATION_BUILD_PACKAGES)
        image.build_from(self.source_spec())
        if any(spec.tpm_enabled() for spec in self.keys):
            image.install("tpm2-tools")
        image.file(self.config_path, content=self._render_config())
        image.runtime_init(
            f"/usr/bin/key-gen setup {shlex.quote(self.config_path)}\n",
            priority=KEY_GENERATION_INIT_PRIORITY,
        )

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
        for spec in self.keys:
            if spec.pipe_path is None or spec.pipe_path.startswith("/run/"):
                continue
            yield Diagnostic(
                level="info",
                code="key-pipe-outside-run",
                message=f"key {spec.name!r} reads its pipe from {spec.pipe_path}, outside /run",
                hint=(
                    "Place pipes under /run (tmpfs): a FIFO on persistent storage "
                    "survives reboots and can be replaced by a regular file."
                ),
                profile=profile,
                subject=spec.name,
            )

    def source_spec(self) -> SourceBuild:
        """The ``key-gen`` source build from ``source``.

        ``mark_unpinned=False`` keeps the unpinned hook byte-identical to the
        hand-written one this module emitted before source builds existed.
        """
        return SourceBuild(
            name="key-generation",
            source=self.source,
            build=GoBuild(package="./cmd/key-gen", output="key-gen"),
            install=(Install.artifact("/usr/bin/key-gen"),),
            mark_unpinned=False,
        )

    def _render_config(self) -> str:
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
