"""The confidential-VM baseline every image implies: :class:`TeeBase` and :class:`Tdxs`, the
evidence service, whose readiness is :data:`EVIDENCE`. Passing one in ``components=``
replaces the default."""

from __future__ import annotations

from ._tee import EVIDENCE, Tdxs, TeeBase

__all__ = ["EVIDENCE", "Tdxs", "TeeBase"]
