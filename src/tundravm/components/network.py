"""The network baseline every image implies: :class:`Network` (also ``image.network``,
configured with :class:`StaticAddress` and :class:`Egress`) and :class:`Firewall`
(switchable with :class:`Mode`). Passing one in ``components=`` replaces the default."""

from __future__ import annotations

from tundravm._image._values import Egress, Mode, StaticAddress

from ._network import Firewall, Network

__all__ = ["Egress", "Firewall", "Mode", "Network", "StaticAddress"]
