"""Components: the :class:`Component` base class, what a component exports, and the shipped
library.

A :class:`Component` subclass is a frozen, keyword-only dataclass: its fields are
configuration, ``compose()`` yields what it stands for, its properties export an
:class:`Endpoint`, a :class:`ServiceRef` or a readiness (:class:`ServiceReady`,
:class:`ComponentReady`), and ``check(resolved: Resolved)`` returns :class:`Diagnostic`
findings, a lint rule ``tundravm check`` runs, of the codes the class declares as
:class:`Rule` values in ``rules``.

- Baseline, implied by every image and replaced by passing one in ``components=``:
  :class:`TeeBase`, :class:`Network` (also ``image.network``, configured with
  :class:`StaticAddress` and :class:`Egress`), :class:`Firewall`, :class:`Tdxs` (the
  evidence service; :data:`EVIDENCE` is its readiness).
- Ways in for people: :class:`SshAccess`, :class:`DevAccess` (``image.dev``).
- :class:`Observability` with :class:`JournalExport`, :class:`PrometheusRemoteWrite` and
  :class:`Proxy` (``image.observability``).
- Workloads: :class:`Nethermind`, :class:`Lighthouse`, :class:`TaikoClient` and
  :class:`Raiko`.
"""

from __future__ import annotations

from tundravm._image._component import (
    Component,
    ComponentReady,
    Endpoint,
    ServiceReady,
    ServiceRef,
)
from tundravm._image._values import Egress, StaticAddress
from tundravm.declarative.model import Diagnostic, Resolved
from tundravm.diagnostics import Rule

from ._base import EVIDENCE, DevAccess, Firewall, Network, SshAccess, Tdxs, TeeBase
from ._lighthouse import Lighthouse
from ._nethermind import Nethermind
from ._observability import JournalExport, Observability, PrometheusRemoteWrite, Proxy
from ._taiko import Raiko, TaikoClient

__all__ = [
    "EVIDENCE",
    "Component",
    "ComponentReady",
    "DevAccess",
    "Diagnostic",
    "Egress",
    "Endpoint",
    "Firewall",
    "JournalExport",
    "Lighthouse",
    "Nethermind",
    "Network",
    "Observability",
    "PrometheusRemoteWrite",
    "Proxy",
    "Raiko",
    "Resolved",
    "Rule",
    "ServiceReady",
    "ServiceRef",
    "SshAccess",
    "StaticAddress",
    "TaikoClient",
    "Tdxs",
    "TeeBase",
]
