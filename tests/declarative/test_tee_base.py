"""``TeeBase``: the confidential-VM baseline, one typed declaration per piece."""

from __future__ import annotations

import pytest

from tundravm.declarative import File, Firewall
from tundravm.declarative.utils import Network, TeeBase
from tundravm.errors import ValidationError


def _items(base: TeeBase) -> list[object]:
    return list(base.compose())


def test_defaults_bring_up_the_network_behind_a_firewall() -> None:
    items = _items(TeeBase())
    assert [type(item).__name__ for item in items[-2:]] == ["Network", "Firewall"]
    assert items[-2] == Network() and items[-1] == Firewall()


def test_each_piece_opts_out() -> None:
    off = TeeBase(
        time=False,
        journal=False,
        sysctl=False,
        dns=False,
        mask=False,
        network=False,
        firewall=False,
    )
    assert _items(off) == []
    hypervisor_free = _items(TeeBase(hypervisor_clocks=(), journal=False, sysctl=False))
    chrony = next(i for i in hypervisor_free if isinstance(i, File) and "chrony" in i.path)
    assert "169.254" not in str(chrony.content)
    with pytest.raises(ValidationError, match="NTS server"):
        _items(TeeBase(nts_servers=()))
