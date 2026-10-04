"""The ``Module`` base class shared by every built-in and user module."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, final

from tundravm.errors import ValidationError

if TYPE_CHECKING:
    from tundravm.check import Diagnostic
    from tundravm.image import Image

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _kebab(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("-", name.lstrip("_")).lower()


class Module:
    """Reusable bundle of image declarations.

    Subclasses override any of the hooks below; ``apply()`` runs them in order
    for the active profiles and records the module on each of them:

    1. ``requires``: every listed module class must already be applied to the
       same profile(s), otherwise ``apply()`` raises ``ValidationError``.
    2. ``setup(image)``: build-time declarations (build packages, build
       sources, build hooks).
    3. ``install(image)``: runtime declarations (packages, files, users,
       services).
    4. ``init_script(image)``: a bash fragment for ``/usr/bin/runtime-init``,
       registered at ``init_priority`` (lower runs first). Ignored when
       ``init_priority`` is ``None``.

    ``check(image, profile)`` contributes module-specific diagnostics to
    ``Image.check()`` for every profile the module was applied to.

    The base class declares no instance state, so ``@dataclass(slots=True)``
    subclasses work unchanged.
    """

    __slots__ = ()

    name: ClassVar[str] = "module"
    requires: ClassVar[tuple[type[Module], ...]] = ()
    init_priority: ClassVar[int | None] = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__:
            cls.name = _kebab(cls.__name__)

    def setup(self, image: Image) -> None:
        """Build-time declarations: build packages, build sources, build hooks."""

    def install(self, image: Image) -> None:
        """Runtime declarations: packages, files, users, services."""

    def init_script(self, image: Image) -> str | None:
        """Bash fragment for ``/usr/bin/runtime-init``; requires ``init_priority``."""
        return None

    def check(self, image: Image, profile: str) -> Iterable[Diagnostic]:
        """Module-specific diagnostics for *profile*, surfaced by ``Image.check()``."""
        return ()

    @final
    def apply(self, image: Image) -> None:
        """Verify ``requires``, run setup/install/init_script, record the module."""
        for profile in image._active_profiles:
            applied = image.applied_modules(profile)
            for required in self.requires:
                if any(isinstance(module, required) for module in applied):
                    continue
                mine, theirs = type(self).__name__, required.__name__
                raise ValidationError(
                    f"Module {mine} requires {theirs}; apply {theirs} first.",
                    hint=f"img.apply({theirs}(), {mine}())",
                    context={"profile": profile},
                )
        self.setup(image)
        self.install(image)
        if self.init_priority is not None:
            script = self.init_script(image)
            if script:
                image.add_init_script(script, priority=self.init_priority)
        image._record_module(self)


__all__ = ["Module"]
