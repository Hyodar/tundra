"""Software built from source: what ``tundravm lock`` pins.

:class:`GoBinary` and :class:`CargoBinary` build one program; :class:`Build` builds
anything else with a toolchain or a script and :class:`Install` steps. Each fetches a
:class:`Git` or :class:`Http` source. A built binary is a valid first element of a
``Service`` argv and pulls its build into the image.
"""

from __future__ import annotations

from tundravm.declarative.model import Build, Git, Http, Install
from tundravm.declarative.utils import CargoBinary, GoBinary

__all__ = ["Build", "CargoBinary", "Git", "GoBinary", "Http", "Install"]
