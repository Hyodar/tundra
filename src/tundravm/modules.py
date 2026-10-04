"""Shipped composition functions: each returns a :class:`~tundravm.declarative.Fragment`.

``from tundravm.modules import tdxs, devtools, efi_stub, backports``; the same
functions are importable from ``tundravm.declarative``.
"""

from tundravm.declarative.modules import (
    BACKPORTS_TREE,
    TUNDRA_TOOLS,
    backports,
    devtools,
    efi_stub,
    tdxs,
)

__all__ = ["BACKPORTS_TREE", "TUNDRA_TOOLS", "backports", "devtools", "efi_stub", "tdxs"]
