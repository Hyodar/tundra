"""Ports: what a service exposes and how traffic reaches it.

``Service(expose=[TCP(8080)])`` opens a port in the firewall, ``UDP(53)`` the same for
datagrams, and nothing else opens one. ``Image(ingress=[AttestedTLS(443, upstream=...)])``
is the confidential way in: TLS relayed to a service on the loopback; :class:`TLS` is
ordinary HTTPS with a certificate delivered as a secret. ``Egress(ports=[TCP(443)])``
names outbound ports with the same values.
"""

from __future__ import annotations

from tundravm._image._ingress import TLS, AttestedTLS
from tundravm._image._values import TCP, UDP

__all__ = ["TCP", "TLS", "UDP", "AttestedTLS"]
