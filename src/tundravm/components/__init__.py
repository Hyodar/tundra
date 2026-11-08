"""Components: the API for writing one. The shipped library lives in the submodules.

A :class:`Component` subclass is a frozen, keyword-only dataclass: its fields are
configuration, ``compose()`` yields what it stands for, its properties export an
:class:`Endpoint`, a :class:`ServiceRef` or a readiness (:class:`ServiceReady`,
:class:`ComponentReady`), and ``check(resolved: Resolved)`` returns :class:`Diagnostic`
findings, a lint rule ``tundravm check`` runs, of the codes the class declares as
:class:`Rule` values in ``rules``.

The shipped components, one submodule each:

- :mod:`tundravm.components.tee`: ``TeeBase``, ``Tdxs`` and its readiness ``EVIDENCE``.
- :mod:`tundravm.components.network`: ``Network``, ``StaticAddress``, ``Egress``,
  ``Firewall``, ``Mode``.
- :mod:`tundravm.components.access`: ``SshAccess``, ``Login``, ``Command``,
  ``CommandParam``, ``ParamType``, ``DevAccess``.
- :mod:`tundravm.components.container`: ``Container``, ``ContainerSsh``, ``Bind``.
- :mod:`tundravm.components.observability`: ``Observability``, ``JournalExport``,
  ``PrometheusRemoteWrite``, ``Proxy``, ``RecordingRule``, ``SigV4``.

The tee and network components are the baseline every image implies; passing one in
``components=`` replaces that default. Application components are the project's own,
written against this module: the example's Nethermind and Lighthouse live in
``examples/attested-node/components/``.
"""

from __future__ import annotations

from tundravm._image._component import (
    Component,
    ComponentReady,
    Endpoint,
    ServiceReady,
    ServiceRef,
)
from tundravm.declarative.model import Diagnostic, Resolved
from tundravm.diagnostics import Rule

__all__ = [
    "Component",
    "ComponentReady",
    "Diagnostic",
    "Endpoint",
    "Resolved",
    "Rule",
    "ServiceReady",
    "ServiceRef",
]
