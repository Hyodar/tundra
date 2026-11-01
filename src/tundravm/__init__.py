"""tundravm: confidential VM images declared as one :class:`Image`.

An :class:`Image` names what it runs (:class:`Service` values, components, :class:`Profile`
variations), what it contains (:class:`Package`, :class:`File`, :class:`Tree`,
:class:`User`, :class:`Group`) and its foundation (:class:`Debian`, :class:`Kernel`,
:class:`RootFS`, the :class:`Mkosi` escape), and nothing about how it is built. Everything
a confidential image needs and no one wants to repeat is implied: Debian trixie from the
locked snapshot, a measured UKI, NTS time, a volatile journal, a firewall that opens only
what a service exposed, one hardened system user per service, and the evidence service the
attestation reads. ``Image``'s docstring lists each implied item with its one escape hatch.

``load`` reads an image file, ``lower`` hands it to the compiler, ``compile`` writes the
mkosi tree, ``build`` builds the artifacts and ``doctor`` checks the host. Every other public
name has one home: :mod:`tundravm.ports`, :mod:`tundravm.systemd`,
:mod:`tundravm.components`, :mod:`tundravm.storage`, :mod:`tundravm.secrets`,
:mod:`tundravm.sources`, :mod:`tundravm.errors` and :mod:`tundravm.testing`.
:mod:`tundravm.declarative` is the internal model those lower onto; it is not the API.
"""

__version__ = "0.1.0"

from ._image import (
    Debian,
    File,
    Group,
    Image,
    Kernel,
    Mkosi,
    Package,
    Profile,
    RootFS,
    Service,
    Tree,
    User,
    build,
    compile,
    load,
    lower,
)
from .declarative.lifecycle import doctor

__all__ = [
    "Debian",
    "File",
    "Group",
    "Image",
    "Kernel",
    "Mkosi",
    "Package",
    "Profile",
    "RootFS",
    "Service",
    "Tree",
    "User",
    "__version__",
    "build",
    "compile",
    "doctor",
    "load",
    "lower",
]
