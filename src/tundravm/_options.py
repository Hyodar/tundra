"""mkosi build-tree options grouped off :class:`~tundravm._image.Image`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Literal

EmitMode = Literal["per_directory", "native_profiles"]


@dataclass(frozen=True, slots=True)
class MkosiOptions:
    """Knobs for the emitted mkosi tree; none of them enter the recipe digest.

    Pass ``Image(mkosi=MkosiOptions(seed=...))`` or adjust an existing image
    with ``img.set_mkosi(replace(img.mkosi, seed=...))``.
    """

    with_network: bool = True
    """``WithNetwork=`` for build scripts."""
    clean_package_metadata: bool = True
    """``CleanPackageMetadata=``."""
    manifest_format: str = "json"
    """``ManifestFormat=``."""
    compress_output: str | None = None
    """``CompressOutput=``; ``None`` leaves mkosi's default."""
    output_directory: str | None = None
    """``OutputDirectory=``; ``None`` leaves mkosi's default."""
    seed: str | None = None
    """``Seed=`` for partition UUIDs; ``None`` uses the SDK's fixed seed."""
    sandbox_trees: tuple[str, ...] = ()
    """``SandboxTrees=`` entries (``backports()`` appends its sources file)."""
    package_cache_directory: str | None = None
    """``PackageCacheDirectory=``."""
    init_script: str | None = None
    """Written to ``mkosi.skeleton/init`` (mode 0755), e.g. ``Image.DEFAULT_TDX_INIT``."""
    environment: Mapping[str, str] = field(default_factory=dict)
    """``Environment=`` key/value pairs."""
    environment_passthrough: tuple[str, ...] | None = None
    """Host variables passed through ``Environment=``."""
    emit_mode: EmitMode = "per_directory"
    """One mkosi tree per profile, or one tree with ``mkosi.profiles/``."""
    generate_version_script: bool = False
    """Emit ``mkosi.version``."""
    generate_cloud_postoutput: bool = True
    """Emit the Azure/GCP disk conversion postoutput scripts."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sandbox_trees", tuple(self.sandbox_trees))
        object.__setattr__(self, "environment", dict(self.environment))
        if self.environment_passthrough is not None:
            object.__setattr__(self, "environment_passthrough", tuple(self.environment_passthrough))

    def non_defaults(self) -> dict[str, object]:
        """The options that differ from ``MkosiOptions()``, in declaration order."""
        default = MkosiOptions()
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) != getattr(default, f.name)
        }


__all__ = ["EmitMode", "MkosiOptions"]
