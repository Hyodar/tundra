"""PromQL selectors for what an image declares, so a :class:`RecordingRule` names a unit or
a mount the way the image file does: ``f"max({searcher.metrics.active})"``,
``data.metrics.available.over("128Gi")``.

A selector is the PromQL text itself with its labels filled in, so an f-string holds it
without doubled braces. The series come from ``image-metrics`` (``tools/cmd/image-metrics``)
and the node exporter's filesystem collector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tundravm.errors import ValidationError

from .._keywords import Keywords

__all__ = ["Selector", "UnitMetrics", "VolumeMetrics"]

_SIZE = re.compile(r"(0|[1-9][0-9]*)(Ki|Mi|Gi|Ti)B?")
_FACTORS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def _label(value: str) -> str:
    """*value* as a PromQL string: backslash and double quote escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Selector(str):
    """A PromQL selector, as the text a rule's expression holds:
    ``tundravm_unit_active{unit="searcher.service"}``.

    A selector of bytes compares with a size in binary units: ``.over("128Gi")`` is
    ``... > bool 137438953472``, 1 while more than that, else 0.
    """

    in_bytes: bool

    def __new__(cls, text: str, *, in_bytes: bool = False) -> Selector:
        self = super().__new__(cls, text)
        self.in_bytes = in_bytes
        return self

    def over(self, size: str) -> str:
        """``SELECTOR > bool BYTES``, *size* written ``"128Gi"`` or ``"2048MiB"``."""
        if not self.in_bytes:
            raise ValidationError(
                f"{self} is not in bytes, so .over({size!r}) compares nothing it measures.",
                hint=f'Write the comparison: f"{{selector}} > bool 0" for {self}.',
            )
        found = _SIZE.fullmatch(size) if isinstance(size, str) else None
        if found is None:
            raise ValidationError(
                f"Selector.over: {size!r} is not a size in binary units.",
                hint='Write "128Gi" or "2048MiB": Ki, Mi, Gi or Ti, with or without B.',
            )
        return f"{self} > bool {int(found[1]) * _FACTORS[found[2]]}"


@dataclass(frozen=True, slots=True)
class UnitMetrics(Keywords):
    """What ``image-metrics`` writes for one unit, as selectors: a ``Service``'s or a
    ``Container``'s ``.metrics``. A container's processes are all in its unit's cgroup."""

    unit: str

    def _select(self, series: str, *, in_bytes: bool = False) -> Selector:
        return Selector(f"{series}{{unit={_label(self.unit)}}}", in_bytes=in_bytes)

    @property
    def active(self) -> Selector:
        """``tundravm_unit_active``: 1 while the unit is active, else 0."""
        return self._select("tundravm_unit_active")

    @property
    def cpu_seconds(self) -> Selector:
        """``tundravm_unit_cpu_seconds_total``: the CPU time systemd accounts to the unit's
        cgroup, a counter (``rate(...[5m])`` is its CPUs in use)."""
        return self._select("tundravm_unit_cpu_seconds_total")

    @property
    def memory_bytes(self) -> Selector:
        """``tundravm_unit_memory_bytes``: the memory the unit's cgroup holds now."""
        return self._select("tundravm_unit_memory_bytes", in_bytes=True)

    @property
    def oom_kills(self) -> Selector:
        """``tundravm_unit_oom_kills_total``: the processes the OOM killer took in the
        unit's cgroup and below it (``memory.events``), a counter."""
        return self._select("tundravm_unit_oom_kills_total")


@dataclass(frozen=True, slots=True)
class VolumeMetrics(Keywords):
    """What the node exporter reports for one mount, as selectors: a ``Volume``'s
    ``.metrics``."""

    mount: str

    def _select(self, series: str) -> Selector:
        return Selector(f"{series}{{mountpoint={_label(self.mount)}}}", in_bytes=True)

    @property
    def available(self) -> Selector:
        """``node_filesystem_avail_bytes``: what the volume's filesystem has free."""
        return self._select("node_filesystem_avail_bytes")

    @property
    def size(self) -> Selector:
        """``node_filesystem_size_bytes``: the volume's filesystem's size."""
        return self._select("node_filesystem_size_bytes")

    @property
    def used_ratio(self) -> Selector:
        """The share of the filesystem in use, from 0 to 1, in parentheses."""
        return Selector(f"(1 - {self.available} / {self.size})")
