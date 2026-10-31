"""The public SDK: an :class:`Image` of :class:`Service` values, :class:`Component`
groups and :class:`Profile` variations.

Everything an image author writes lives here; :mod:`tundravm.declarative` is the
internal model the image lowers onto. The package is named after its one entry point:
an image is what you declare. The runtime contracts are values here too
(:class:`Volume`, :class:`Secret`, :class:`AttestedTLS`, :class:`TLS`, :class:`RootFS`);
the components an image holds by default, and the shipped workloads, are in
:mod:`tundravm.components`.
"""

from __future__ import annotations

from tundravm.declarative.model import (
    Build,
    Calendar,
    Diagnostic,
    DropIn,
    Every,
    Git,
    Group,
    Http,
    Install,
    Listen,
    Resolved,
    User,
)
from tundravm.declarative.systemd import Hardening
from tundravm.declarative.utils import CargoBinary, GoBinary

from ._api import bake, compile, load, lower
from ._component import (
    BootReady,
    Component,
    ComponentReady,
    Endpoint,
    ServiceReady,
    ServiceRef,
)
from ._image import Image, Profile
from ._ingress import TLS, AttestedTLS
from ._lower import recipe_of
from ._rootfs import RootFS
from ._secrets import BrokerKey, Secret
from ._service import Service
from ._storage import OperatorKey, TpmSeal, Volume, VolumeDirectory
from ._values import (
    TCP,
    UDP,
    Credential,
    Debian,
    Egress,
    File,
    HttpProbe,
    Kernel,
    Mkosi,
    Notify,
    Package,
    Resources,
    Shutdown,
    StaticAddress,
    TcpProbe,
    Tree,
)

__all__ = [
    "TCP",
    "TLS",
    "UDP",
    "AttestedTLS",
    "BootReady",
    "BrokerKey",
    "Build",
    "Calendar",
    "CargoBinary",
    "Component",
    "ComponentReady",
    "Credential",
    "Debian",
    "Diagnostic",
    "DropIn",
    "Egress",
    "Endpoint",
    "Every",
    "File",
    "Git",
    "GoBinary",
    "Group",
    "Hardening",
    "Http",
    "HttpProbe",
    "Image",
    "Install",
    "Kernel",
    "Listen",
    "Mkosi",
    "Notify",
    "OperatorKey",
    "Package",
    "Profile",
    "Resolved",
    "Resources",
    "RootFS",
    "Secret",
    "Service",
    "ServiceReady",
    "ServiceRef",
    "Shutdown",
    "StaticAddress",
    "TcpProbe",
    "TpmSeal",
    "Tree",
    "User",
    "Volume",
    "VolumeDirectory",
    "bake",
    "compile",
    "load",
    "lower",
    "recipe_of",
]
