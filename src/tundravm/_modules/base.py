"""The ``Module`` base class of the runtime tools lowering configures."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from tundravm._source import GitSource

if TYPE_CHECKING:
    from tundravm._image import Image
    from tundravm.check import Diagnostic

TUNDRA_TOOLS = GitSource("https://github.com/Hyodar/tundra-tools.git", "master")
"""The ``tundra-tools`` repository Tdxs, KeyGeneration, DiskEncryption and
SecretDelivery build from by default; pass ``source=`` to pin another ref."""


class Module:
    """A runtime tool's configuration, recorded on each profile it configures.

    Lowering (``tundravm.declarative.state``) writes what the module renders
    (its config files, source build and ``/usr/bin/runtime-init`` step) and
    records it per profile; ``check(image, profile)`` contributes the
    module's diagnostics to ``check()`` for each of those profiles.

    The base class declares no instance state, so ``@dataclass(slots=True)``
    subclasses work unchanged.
    """

    __slots__ = ()

    def check(self, image: Image, profile: str) -> Iterable[Diagnostic]:
        """Module-specific diagnostics for *profile*, surfaced by ``check()``."""
        return ()


__all__ = ["TUNDRA_TOOLS", "Module"]
