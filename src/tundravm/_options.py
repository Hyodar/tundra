"""mkosi build-tree options of a lowered recipe (``declarative._lowered.Lowered``; internal)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Literal

RootLayout = Literal["initramfs", "verity"]
"""Where the payload's root filesystem lives: the UKI's ``.initrd`` (``initramfs``), or an
erofs root and its dm-verity hash partition beside the ESP (``verity``)."""


@dataclass(frozen=True, slots=True)
class MkosiOptions:
    """Knobs for the emitted mkosi tree; none of them enter the recipe digest.

    ``declarative.lower`` builds them from ``Recipe.mkosi``, the platform and the
    recipe's ``Setting`` items.
    """

    with_network: bool = False
    """``WithNetwork=`` for build scripts: ``no`` unless ``Mkosi(build_network=True)``."""
    clean_package_metadata: bool = True
    """``CleanPackageMetadata=``."""
    kernel_modules: tuple[str, ...] | Literal["all"] = ()
    """``Debian(kernel_modules=...)``: names added to the curated module set
    (``KernelModules=``), or ``"all"``."""
    kernel_cmdline: tuple[str, ...] = ()
    """``Debian(kernel_cmdline=...)``: parameters after the compiler's own on
    ``KernelCommandLine=``."""
    manifest_format: str = "json"
    """``ManifestFormat=``."""
    compress_output: str | None = None
    """``CompressOutput=``; ``None`` leaves mkosi's default."""
    output_directory: str | None = None
    """``OutputDirectory=``; ``None`` writes ``mkosi.output``, which ``%O`` needs set."""
    seed: str | None = None
    """``Seed=`` for partition UUIDs; ``None`` uses the SDK's fixed seed."""
    sandbox_trees: tuple[str, ...] = ()
    """``SandboxTrees=`` entries."""
    sandbox_files: tuple[tuple[str, str], ...] = ()
    """``(path, content)`` written under ``mkosi.sandbox/``, which mkosi copies into its
    package-manager sandbox (not the image)."""
    package_cache_directory: str | None = None
    """``PackageCacheDirectory=``."""
    environment: Mapping[str, str] = field(default_factory=dict)
    """``Environment=`` key/value pairs of the payload's scripts (never host pass-through)."""
    generate_version_script: bool = False
    """Emit ``mkosi.version``."""
    settings: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """``(section, key, values)`` written verbatim, one line per value: to the root
    ``mkosi.conf`` for a key mkosi reads in the main image only, else to the payload's."""
    bootable: bool = True
    """``False`` writes ``Bootable=no`` and a plain disk payload instead of a UKI."""
    root: RootLayout = "initramfs"
    """The payload's root filesystem layout (``Mkosi(root=...)``)."""
    root_size: str | None = None
    """``Mkosi(root_size=)``: the initramfs root's tmpfs cap, written to ``/etc/fstab``."""
    uki_max_size: str | None = None
    """``Mkosi(uki_max_size=)``: the initramfs UKI's ceiling, which sizes the ESP."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sandbox_trees", tuple(self.sandbox_trees))
        object.__setattr__(self, "sandbox_files", tuple(self.sandbox_files))
        object.__setattr__(self, "settings", tuple(self.settings))
        object.__setattr__(self, "environment", dict(self.environment))

    def non_defaults(self) -> dict[str, object]:
        """The options that differ from ``MkosiOptions()``, in declaration order."""
        default = MkosiOptions()
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) != getattr(default, f.name)
        }


__all__ = ["MkosiOptions", "RootLayout"]
