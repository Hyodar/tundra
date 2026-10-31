"""Persistent storage: encrypted volumes, the directories services keep on them, and how
each volume unlocks.

``Volume("data", mount="/persistent", unlock=OperatorKey())`` is a LUKS2 volume an operator
unlocks after attesting the instance (``tundravm secrets unlock INSTANCE data``);
:class:`TpmSeal` and :class:`BrokerKey` are the unattended alternatives.
``volume.directory("app")`` is a :class:`VolumeDirectory` a service binds as its state.
"""

from __future__ import annotations

from tundravm._image._secrets import BrokerKey
from tundravm._image._storage import OperatorKey, TpmSeal, Volume, VolumeDirectory

__all__ = ["BrokerKey", "OperatorKey", "TpmSeal", "Volume", "VolumeDirectory"]
