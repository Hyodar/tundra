"""The root-filesystem layout of the measured payload (plan section 6.6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tundravm.errors import ValidationError

__all__ = ["RootFS"]


@dataclass(frozen=True, slots=True)
class RootFS:
    """Where the OS lives: ``RootFS.initramfs()`` (the default) or ``RootFS.verity()``.

    ``initramfs`` puts the whole OS in the UKI's ``.initrd``: small services, one file to
    measure. ``verity`` makes a read-only erofs root with a dm-verity hash partition
    beside the ESP and the root hash on the UKI's command line, for payloads too large
    for an initrd (a Rust prover, a .NET client); the root is overlaid with a volatile
    writable layer at boot. The layout is chosen explicitly, recorded in the manifest and
    never switched by a size threshold.
    """

    layout: Literal["initramfs", "verity"] = "initramfs"
    filesystem: Literal["erofs"] | None = None

    def __post_init__(self) -> None:
        if self.layout not in ("initramfs", "verity"):
            raise ValidationError(
                f"RootFS: layout {self.layout!r} is not initramfs or verity.",
                hint="Use RootFS.initramfs() or RootFS.verity().",
            )
        if self.layout == "initramfs" and self.filesystem is not None:
            raise ValidationError(
                "RootFS.initramfs() takes no filesystem.",
                hint="Use RootFS.initramfs(), or RootFS.verity(filesystem='erofs').",
            )
        if self.layout == "verity" and self.filesystem != "erofs":
            raise ValidationError(
                f"RootFS.verity(filesystem={self.filesystem!r}) is not offered.",
                hint="The verity root is erofs, which the initrd mounts: "
                "RootFS.verity(filesystem='erofs').",
            )

    @classmethod
    def initramfs(cls) -> RootFS:
        """The OS in the UKI's initrd."""
        return cls("initramfs")

    @classmethod
    def verity(cls, *, filesystem: Literal["erofs"] = "erofs") -> RootFS:
        """A read-only *filesystem* root verified by dm-verity."""
        return cls("verity", filesystem)
