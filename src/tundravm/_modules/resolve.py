"""Shared utilities for module dependency resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tundravm._image import Image


def resolve_after(after: tuple[str, ...], image: Image) -> tuple[str, ...]:
    """Build an After= list, prepending the init service if available.

    If an active profile has registered runtime-init scripts, the init service
    name is prepended to *after* (unless already present) so that dependent
    services wait for init to complete.
    """
    result = list(after)
    if image.init is not None and image.has_init_scripts():
        init_svc = image.init.service_name
        if init_svc not in result:
            result.insert(0, init_svc)
    return tuple(result)
