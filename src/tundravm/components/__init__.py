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
  :class:`StaticAddress` and :class:`Egress`), :class:`Firewall` (switchable with
  :class:`Mode`), :class:`Tdxs` (the
  evidence service; :data:`EVIDENCE` is its readiness).
- Ways in for people: :class:`SshAccess` (root, and each non-root :class:`Login` with its
  :class:`Sudo` rules), :class:`DevAccess` (``image.dev``).
- :class:`Observability` with :class:`JournalExport`, :class:`PrometheusRemoteWrite` and
  :class:`Proxy` (``image.observability``).

Application components are the project's own, written against this module: the
example's Nethermind and Lighthouse live in ``examples/attested-node/components/``.
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

from ._base import (
    EVIDENCE,
    DevAccess,
    Firewall,
    Login,
    Mode,
    Network,
    SshAccess,
    Sudo,
    Tdxs,
    TeeBase,
)
from ._observability import JournalExport, Observability, PrometheusRemoteWrite, Proxy

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
    "Login",
    "Mode",
    "Network",
    "Observability",
    "PrometheusRemoteWrite",
    "Proxy",
    "Resolved",
    "Rule",
    "ServiceReady",
    "ServiceRef",
    "SshAccess",
    "StaticAddress",
    "Sudo",
    "Tdxs",
    "TeeBase",
]
