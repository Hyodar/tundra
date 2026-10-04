"""The ``Module`` base class shared by every built-in and user module."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, final

from tundravm.errors import ValidationError
from tundravm.source import GitSource

if TYPE_CHECKING:
    from tundravm.check import Diagnostic
    from tundravm.image import Image

TUNDRA_TOOLS = GitSource("https://github.com/Hyodar/tundra-tools.git", "master")
"""The ``tundra-tools`` repository Tdxs, KeyGeneration, DiskEncryption and
SecretDelivery build from by default; pass ``source=`` to pin another ref."""

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _kebab(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("-", name.lstrip("_")).lower()


class Module:
    """Reusable bundle of image declarations.

    Subclasses override ``configure(image)`` and declare everything the module
    contributes there, in order: build packages and source builds, runtime
    packages, files, users and services, and ``/usr/bin/runtime-init`` fragments
    through ``image.runtime_init(script, priority=N)`` (lower runs first).

    ``apply()`` checks ``requires`` (every listed module class must already be
    applied to the same profile(s), otherwise it raises ``ValidationError``),
    calls ``configure()`` and records the module on the active profiles.

    ``check(image, profile)`` contributes module-specific diagnostics to
    ``Image.check()`` for every profile the module was applied to.

    The base class declares no instance state, so ``@dataclass(slots=True)``
    subclasses work unchanged.
    """

    __slots__ = ()

    name: ClassVar[str] = "module"
    requires: ClassVar[tuple[type[Module], ...]] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__:
            cls.name = _kebab(cls.__name__)

    def configure(self, image: Image) -> None:
        """Declare the module's build and runtime contributions on *image*."""

    def check(self, image: Image, profile: str) -> Iterable[Diagnostic]:
        """Module-specific diagnostics for *profile*, surfaced by ``Image.check()``."""
        return ()

    @final
    def apply(self, image: Image) -> None:
        """Verify ``requires``, run ``configure()``, record the module."""
        for profile in image._active_profiles:
            applied = image.applied_modules(profile, inherited=True)
            for required in self.requires:
                if any(isinstance(module, required) for module in applied):
                    continue
                mine, theirs = type(self).__name__, required.__name__
                raise ValidationError(
                    f"Module {mine} requires {theirs}; apply {theirs} first.",
                    hint=f"img.apply({theirs}(), {mine}())",
                    context={"profile": profile},
                )
        self.configure(image)
        image._record_module(self)


__all__ = ["TUNDRA_TOOLS", "Module"]
