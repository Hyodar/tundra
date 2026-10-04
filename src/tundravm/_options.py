"""mkosi build-tree options of the lowered :class:`~tundravm._image.Image` (internal)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Literal

EmitMode = Literal["per_directory", "native_profiles"]


@dataclass(frozen=True, slots=True)
class MkosiOptions:
    """Knobs for the emitted mkosi tree; none of them enter the recipe digest.

    ``declarative.lower`` builds them from the recipe's ``Setting`` items.
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
    """``SandboxTrees=`` entries."""
    package_cache_directory: str | None = None
    """``PackageCacheDirectory=``."""
    init_script: str | None = None
    """Written to ``mkosi.skeleton/init`` (mode 0755)."""
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
    settings: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """``(section, key, values)`` written verbatim to ``mkosi.conf``, one line per value."""
    bootable: bool = True
    """``False`` writes ``Bootable=no`` and a plain disk image instead of a UKI."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sandbox_trees", tuple(self.sandbox_trees))
        object.__setattr__(self, "settings", tuple(self.settings))
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
