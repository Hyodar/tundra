"""Software built from source: what ``tundravm lock`` pins.

:class:`GoBinary` and :class:`CargoBinary` build one program; :class:`Build` builds
anything else with a toolchain or a script and :class:`Install` steps. Each builds a
:class:`Local` directory of the project (your own program, beside the image file), a
:class:`Git` repository or an :class:`Http` download. A built binary is a valid first
element of a ``Service`` argv and pulls its build into the image.
"""

from __future__ import annotations

from tundravm.declarative.model import Build, Git, Http, Install, Local
from tundravm.declarative.utils import CargoBinary, GoBinary

__all__ = ["Build", "CargoBinary", "Git", "GoBinary", "Http", "Install", "Local"]
