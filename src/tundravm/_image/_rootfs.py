"""The root-filesystem layout of the measured payload (plan section 6.6)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from tundravm.errors import ValidationError

__all__ = ["RootFS"]

_SIZE = re.compile(r"[1-9][0-9]*[KMG]|[1-9][0-9]?%")


@dataclass(frozen=True, slots=True)
class RootFS:
    """Where the OS lives: ``RootFS.initramfs()`` (the default) or ``RootFS.verity()``.

    ``initramfs`` puts the whole OS in the UKI's ``.initrd``: small services, one file to
    measure. ``verity`` makes a read-only erofs root with a dm-verity hash partition
    beside the ESP and the root hash on the UKI's command line, for payloads too large
    for an initrd (a Rust prover, a .NET client); the root is overlaid with a volatile
    writable layer at boot. The layout is chosen explicitly, recorded in the manifest and
    never switched by a size threshold.

    ``RootFS.initramfs(max_size="4G")`` caps the root tmpfs (the kernel's default is half
    the memory): written past the cap, the root fills instead of the VM running out of
    memory. It is recorded in the manifest too.
    """

    layout: Literal["initramfs", "verity"] = "initramfs"
    filesystem: Literal["erofs"] | None = None
    max_size: str | None = None

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
        if self.max_size is None:
            return
        if self.layout == "verity":
            raise ValidationError(
                "RootFS.verity() takes no max_size: its writable layer is systemd's volatile "
                "overlay, not a root tmpfs.",
                hint="Drop max_size=, or use RootFS.initramfs(max_size=...).",
            )
        if not isinstance(self.max_size, str) or not _SIZE.fullmatch(self.max_size):
            raise ValidationError(
                f"RootFS.initramfs(max_size={self.max_size!r}) is not a tmpfs size.",
                hint="Pass a size such as '4G', '512M' or '50%' (of the memory).",
            )

    @classmethod
    def initramfs(cls, *, max_size: str | None = None) -> RootFS:
        """The OS in the UKI's initrd, its tmpfs capped at *max_size* (``"4G"``, ``"50%"``)."""
        return cls("initramfs", max_size=max_size)

    @classmethod
    def verity(cls, *, filesystem: Literal["erofs"] = "erofs") -> RootFS:
        """A read-only *filesystem* root verified by dm-verity."""
        return cls("verity", filesystem)
