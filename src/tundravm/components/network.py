"""The network baseline every image implies: :class:`Network` (also ``image.network``,
configured with :class:`StaticAddress` and :class:`Egress`, whose peers a :class:`Host`
pins) and :class:`Firewall` (switchable with :class:`Mode`). Passing one in
``components=`` replaces the default."""

from __future__ import annotations

from tundravm._image._values import Egress, Host, Mode, StaticAddress

from ._network import Firewall, Network

__all__ = ["Egress", "Firewall", "Host", "Mode", "Network", "StaticAddress"]
