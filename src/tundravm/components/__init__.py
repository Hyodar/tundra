"""The shipped component library: the baseline every image implies, and the workloads
the Surge prover composes.

Every class here is a :class:`~tundravm.image.Component`: configuration fields, a
``compose()``, typed exports and a ``check()`` lint rule.

- Baseline, implied by every image and replaced by passing one in ``components=``:
  :class:`TeeBase`, :class:`Network` (also ``image.network``), :class:`Firewall`,
  :class:`Tdxs` (the evidence service; :data:`EVIDENCE` is its readiness).
- Ways in for people: :class:`SshAccess`, :class:`DevAccess` (``image.dev``).
- :class:`Observability` with :class:`JournalExport` and :class:`PrometheusRemoteWrite`
  (``image.observability``).
- Workloads: :class:`Nethermind` (:mod:`tundravm.components.nethermind`),
  :class:`TaikoClient` and :class:`Raiko` (:mod:`tundravm.components.taiko`).
"""

from __future__ import annotations

from ._base import EVIDENCE, DevAccess, Firewall, Network, SshAccess, Tdxs, TeeBase
from ._observability import JournalExport, Observability, PrometheusRemoteWrite
from .nethermind import Nethermind
from .taiko import Raiko, TaikoClient

__all__ = [
    "EVIDENCE",
    "DevAccess",
    "Firewall",
    "JournalExport",
    "Nethermind",
    "Network",
    "Observability",
    "PrometheusRemoteWrite",
    "Raiko",
    "SshAccess",
    "TaikoClient",
    "Tdxs",
    "TeeBase",
]
