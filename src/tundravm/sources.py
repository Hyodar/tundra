"""Software built from source: what ``tundravm lock`` pins.

:class:`GoBinary` and :class:`CargoBinary` build one program; :class:`Build` builds
anything else with a toolchain (``using=``: :class:`Go`, :class:`Cargo` or
:class:`Dotnet`) or a script and :class:`Install` steps. Each builds a :class:`Local`
directory of the project (your own program, beside the image file), a :class:`Git`
repository or an :class:`Http` download. A built binary is a valid first element of a
``Service`` argv and pulls its build into the image. Each toolchain is the one the
source pins (``go.mod``, ``rust-toolchain.toml``, ``global.json``), fetched on the host
for the offline build.
"""

from __future__ import annotations

from tundravm.declarative.model import Build, Cargo, Dotnet, Git, Go, Http, Install, Local
from tundravm.declarative.utils import CargoBinary, GoBinary

__all__ = [
    "Build",
    "Cargo",
    "CargoBinary",
    "Dotnet",
    "Git",
    "Go",
    "GoBinary",
    "Http",
    "Install",
    "Local",
]
