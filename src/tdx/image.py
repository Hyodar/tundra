"""Core image object for SDK recipe declarations."""

from dataclasses import dataclass, field
from pathlib import Path

from .models import Arch, RecipeState


@dataclass(slots=True)
class Image:
    """Represents an image recipe root."""

    build_dir: Path = field(default_factory=lambda: Path("build"))
    base: str = "debian/bookworm"
    arch: Arch = "x86_64"
    default_profile: str = "default"
    _state: RecipeState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._state = RecipeState.initialize(
            base=self.base,
            arch=self.arch,
            default_profile=self.default_profile,
        )

    @property
    def state(self) -> RecipeState:
        return self._state
