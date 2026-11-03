"""Cloud platform integration files lowering adds for the ``azure`` and ``gcp`` targets."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from . import azure, gcp

if TYPE_CHECKING:
    from tundravm.declarative.model import Service, Task

__all__ = ["azure", "gcp", "metadata_units"]


def metadata_units(targets: Iterable[str]) -> tuple[Service | Task, ...]:
    """The glue units of *targets* that read their cloud's metadata servers, each naming it
    in its ``egress``: what the firewall lets reach them, beside the image's own services."""
    found: list[Service | Task] = []
    wanted = set(targets)
    if "azure" in wanted:
        found.append(azure.provisioning_task(False))
    if "gcp" in wanted:
        found += gcp.metadata_units()
    return tuple(found)
