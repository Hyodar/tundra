"""The values ``Service`` keywords take, each rendered as systemd unit directives.

The sandbox (:class:`Hardening`), cgroup limits (:class:`Resources`), how it stops
(:class:`Shutdown`), its readiness (:class:`Notify`, :class:`TcpProbe`, :class:`HttpProbe`),
a schedule (:class:`Every`, :class:`Calendar`), socket activation (:class:`Listen`) and the
escape for anything else systemd knows (:class:`DropIn`).
"""

from __future__ import annotations

from tundravm._image._values import HttpProbe, Notify, Resources, Shutdown, TcpProbe
from tundravm.declarative.model import Calendar, DropIn, Every, Listen
from tundravm.declarative.systemd import Hardening

__all__ = [
    "Calendar",
    "DropIn",
    "Every",
    "Hardening",
    "HttpProbe",
    "Listen",
    "Notify",
    "Resources",
    "Shutdown",
    "TcpProbe",
]
