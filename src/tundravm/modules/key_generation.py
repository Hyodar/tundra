"""Key generation module."""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tundravm.build_cache import Build, Cache
from tundravm.errors import ValidationError

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
class KeyGeneration:
    """Generate one or more cryptographic keys at boot time.

    The underlying ``tundra-tools`` binary supports the ``random`` and ``pipe``
    strategies, with TPM persistence controlled separately. ``strategy='tpm'``
    is kept as a compatibility alias for ``random`` with TPM persistence.
    """

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

    def apply(self, image: Image) -> None:
        """Add build hook, aggregate config, and init script to *image*."""
        self._validate()
        image.build_install(*KEY_GENERATION_BUILD_PACKAGES)
        image.install("tpm2-tools")

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
        image.file(self.config_path, content=self._render_config())
        image.add_init_script(self._render_init_script(), priority=10)

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
                    f'    tpm: {"true" if spec.tpm_enabled() else "false"}',
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
        lines = [f"/usr/bin/key-gen setup {shlex.quote(self.config_path)}"]
        primary = next((spec for spec in self._keys if spec.output is not None), None)
        if primary is not None and primary.output is not None:
            output_path = shlex.quote(primary.output)
            lines.extend(
                (
                    f"if [ -f {output_path} ]; then",
                    f'    export DISK_ENCRYPTION_KEY="$(tr -d \'\\n\' < {output_path})"',
                    "else",
                    f'    echo "missing generated key output: {primary.output}" >&2',
                    "    exit 1",
                    "fi",
                )
            )
        return "\n".join(lines) + "\n"

    def _validate_name(self, name: str, *, kind: str) -> None:
        if not name:
            raise ValidationError(f"{kind} names must be non-empty.")
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            raise ValidationError(
                f"Invalid {kind} name {name!r}.",
                hint="Use only letters, numbers, dot, underscore, and dash.",
            )
