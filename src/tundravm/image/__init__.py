"""The public SDK: an :class:`Image` of :class:`Service` values, :class:`Component`
groups and :class:`Profile` variations.

Everything a recipe author writes lives here; :mod:`tundravm.declarative` is the
internal model the image lowers onto. The package is named after its one entry point:
an image is what you declare, and the slots this gate does not fill yet (volumes,
secrets, attested ingress, the root layout, observability) are further modules beside
:mod:`tundravm.image._values`, not changes to the ones here.
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
from ._component import Component, ComponentReady, Endpoint, ServiceReady, ServiceRef
from ._image import Image, Profile
from ._lower import recipe_of
from ._service import Service
from ._values import (
    TCP,
    UDP,
    Credential,
    Debian,
    DevAccess,
    Egress,
    File,
    HttpProbe,
    Kernel,
    Mkosi,
    Network,
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
    "UDP",
    "Build",
    "Calendar",
    "CargoBinary",
    "Component",
    "ComponentReady",
    "Credential",
    "Debian",
    "DevAccess",
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
    "Network",
    "Notify",
    "Package",
    "Profile",
    "Resolved",
    "Resources",
    "Service",
    "ServiceReady",
    "ServiceRef",
    "Shutdown",
    "StaticAddress",
    "TcpProbe",
    "Tree",
    "User",
    "bake",
    "compile",
    "load",
    "lower",
    "recipe_of",
]
