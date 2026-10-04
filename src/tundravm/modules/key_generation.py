"""Key generation module."""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal

from tundravm.build_cache import Build, Cache
from tundravm.check import Diagnostic
from tundravm.errors import ValidationError
from tundravm.modules.base import Module

if TYPE_CHECKING:
    from tundravm.image import Image

KEY_GENERATION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

KEY_GENERATION_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
KEY_GENERATION_DEFAULT_BRANCH = "master"
KEY_GENERATION_DEFAULT_CONFIG_PATH = "/etc/tdx/key-gen.yaml"
ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class KeySpec:
    """Specification for a cryptographic key generated at boot time."""

    name: str
    strategy: Literal["tpm", "random", "pipe"] = "tpm"
    output: str | None = None
    size: int = 64
    pipe_path: str | None = None
    persist_in_tpm: bool | None = None

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

    init_priority: ClassVar[int | None] = 10

    config_path: str = KEY_GENERATION_DEFAULT_CONFIG_PATH
    source_repo: str = KEY_GENERATION_DEFAULT_REPO
    source_branch: str = KEY_GENERATION_DEFAULT_BRANCH
    _keys: list[KeySpec] = field(default_factory=list, init=False, repr=False)

    def key(
        self,
        name: str,
        *,
        strategy: Literal["tpm", "random", "pipe"] = "tpm",
        output: str | None = None,
        size: int = 64,
        pipe_path: str | None = None,
        persist_in_tpm: bool | None = None,
    ) -> KeySpec:
        """Register an additional key definition."""
        spec = KeySpec(
            name=name,
            strategy=strategy,
            output=output,
            size=size,
            pipe_path=pipe_path,
            persist_in_tpm=persist_in_tpm,
        )
        self._append_key(spec)
        return spec

    @property
    def keys(self) -> tuple[KeySpec, ...]:
        """Registered key definitions, in declaration order."""
        return tuple(self._keys)

    def setup(self, image: Image) -> None:
        """Validate keys, declare build packages and the key-gen build hook."""
        self._validate()
        image.build_install(*KEY_GENERATION_BUILD_PACKAGES)

        clone_dir = Build.build_path("key-generation")
        chroot_dir = Build.chroot_path("key-generation")
        cache = Cache.declare(
            self._cache_key(),
            (
                Cache.file(
                    src=Build.build_path("key-generation/build/key-gen"),
                    dest=Build.dest_path("usr/bin/key-gen"),
                    name="key-gen",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {shlex.quote(self.source_branch)} "
            f'{shlex.quote(self.source_repo)} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir} && "
            "mkdir -p ./build && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/key-gen ./cmd/key-gen"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))

    def install(self, image: Image) -> None:
        """Install tpm2-tools when needed and write the aggregate key config."""
        if any(spec.tpm_enabled() for spec in self._keys):
            image.install("tpm2-tools")
        image.file(self.config_path, content=self._render_config())

    def init_script(self, image: Image) -> str:
        return self._render_init_script()

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
        for spec in self._keys:
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

    def _append_key(self, spec: KeySpec) -> None:
        self._validate_name(spec.name, kind="key")
        if any(existing.name == spec.name for existing in self._keys):
            raise ValidationError(f"Duplicate key name {spec.name!r}.")
        self._keys.append(spec)

    def _validate(self) -> None:
        if not self._keys:
            raise ValidationError("KeyGeneration requires at least one key definition.")

        output_paths: set[str] = set()
        for spec in self._keys:
            if spec.output is not None:
                if spec.output in output_paths:
                    raise ValidationError(
                        "Each generated key output path must be unique.",
                        context={"key": spec.name, "path": spec.output},
                    )
                output_paths.add(spec.output)
            if spec.strategy == "pipe" and not spec.pipe_path:
                raise ValidationError(
                    "pipe strategy requires pipe_path.",
                    context={"key": spec.name},
                )
            if spec.strategy != "pipe" and spec.pipe_path is not None:
                raise ValidationError(
                    "pipe_path is only valid with strategy='pipe'.",
                    context={"key": spec.name, "strategy": spec.strategy},
                )

    def _cache_key(self) -> str:
        repo_hash = hashlib.sha256(self.source_repo.encode("utf-8")).hexdigest()[:12]
        return f"key-generation-{repo_hash}-{self.source_branch}"

    def _render_config(self, keys: tuple[KeySpec, ...] | None = None) -> str:
        key_specs = keys or tuple(self._keys)
        lines = ["keys:"]
        for spec in key_specs:
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

    def _render_init_script(self) -> str:
        return f"/usr/bin/key-gen setup {shlex.quote(self.config_path)}\n"

    def _validate_name(self, name: str, *, kind: str) -> None:
        if not name:
            raise ValidationError(f"{kind} names must be non-empty.")
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            raise ValidationError(
                f"Invalid {kind} name {name!r}.",
                hint="Use only letters, numbers, dot, underscore, and dash.",
            )
