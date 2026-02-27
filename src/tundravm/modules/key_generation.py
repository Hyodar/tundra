"""Key generation module."""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from textwrap import dedent
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
KEY_GENERATION_TPM_NV_INDEX = "0x1500016"
KEY_GENERATION_TPM_TCTI = "device:/dev/tpmrm0"
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

    strategy: Literal["tpm", "random", "pipe"] = "tpm"
    output: str | None = "/persistent/key"
    key_name: str = "key_persistent"
    size: int = 64
    pipe_path: str | None = None
    persist_in_tpm: bool | None = None
    config_path: str = KEY_GENERATION_DEFAULT_CONFIG_PATH
    source_repo: str = KEY_GENERATION_DEFAULT_REPO
    source_branch: str = KEY_GENERATION_DEFAULT_BRANCH
    _keys: list[KeySpec] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._append_key(
            KeySpec(
                name=self.key_name,
                strategy=self.strategy,
                output=self.output,
                size=self.size,
                pipe_path=self.pipe_path,
                persist_in_tpm=self.persist_in_tpm,
            )
        )

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
        """Add build hook, config files, and init script to *image*."""
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
        for spec in self._keys:
            image.file(self._entry_config_path(spec.name), content=self._render_config((spec,)))
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
            if spec.tpm_enabled() and spec.output is None:
                raise ValidationError(
                    "TPM-backed keys require output paths in the module layer.",
                    hint=(
                        "This module captures each key from the shared TPM NV index "
                        "immediately after generation."
                    ),
                    context={"key": spec.name},
                )
            if spec.output is not None and not spec.tpm_enabled():
                raise ValidationError(
                    "Non-TPM keys cannot be materialized to output paths by this module.",
                    hint="Use persist_in_tpm=True for keys that need file outputs.",
                    context={"key": spec.name},
                )
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

    def _entry_config_path(self, name: str) -> str:
        config_dir = (
            f"{self.config_path[:-5]}.d"
            if self.config_path.endswith(".yaml")
            else f"{self.config_path}.d"
        )
        return f"{config_dir}/{name}.yaml"

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
        return "\n".join(lines) + "\n"

    def _render_init_script(self) -> str:
        lines: list[str] = []
        for spec in self._keys:
            config_path = shlex.quote(self._entry_config_path(spec.name))
            lines.append(f"/usr/bin/key-gen setup {config_path}")
            if spec.output is None:
                lines.append("")
                continue

            output_path = shlex.quote(spec.output)
            output_dir = shlex.quote(str(PurePosixPath(spec.output).parent))
            variable_name = _shell_var_name(spec.name)
            lines.extend(
                (
                    f"install -d -m 0700 {output_dir}",
                    f'export {variable_name}="$(',
                    f"    tpm2_nvread -C o -T {KEY_GENERATION_TPM_TCTI} \\",
                    f"        {KEY_GENERATION_TPM_NV_INDEX} | tr -d '\\n'",
                    ')"',
                    f"printf '%s\\n' \"${{{variable_name}}}\" > {output_path}",
                    f"chmod 0600 {output_path}",
                )
            )
            if spec.name == self.key_name:
                lines.append(f'export DISK_ENCRYPTION_KEY="${{{variable_name}}}"')
            lines.append("")
        return dedent("\n".join(lines)).rstrip() + "\n"

    def _validate_name(self, name: str, *, kind: str) -> None:
        if not name:
            raise ValidationError(f"{kind} names must be non-empty.")
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            raise ValidationError(
                f"Invalid {kind} name {name!r}.",
                hint="Use only letters, numbers, dot, underscore, and dash.",
            )


def _shell_var_name(name: str) -> str:
    return "KEY_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
