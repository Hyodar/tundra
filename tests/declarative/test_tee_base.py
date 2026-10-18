"""``TeeBase``: the confidential-VM baseline, one typed declaration per piece."""

from __future__ import annotations

import pytest

from tundravm.declarative import File, Package, Unit
from tundravm.declarative.utils import TEE_MASKED_UNITS, TEE_SYSCTL, TeeBase
from tundravm.errors import ValidationError


def _items(base: TeeBase) -> list[object]:
    return list(base.compose())


def test_defaults_declare_time_journal_sysctl_dns_and_masks() -> None:
    items = _items(TeeBase())
    files = {item.path: item for item in items if isinstance(item, File)}
    assert set(files) == {
        "/etc/chrony/chrony.conf",
        "/etc/systemd/journald.conf.d/50-tundravm-tee.conf",
        "/etc/sysctl.d/50-tundravm-tee.conf",
        "/etc/resolv.conf",
    }
    chrony = str(files["/etc/chrony/chrony.conf"].content)
    assert "server time.cloudflare.com iburst nts\n" in chrony
    assert "maxdistance 1.0\n" in chrony
    sysctl = str(files["/etc/sysctl.d/50-tundravm-tee.conf"].content)
    assert sysctl.count("\n-") == len(TEE_SYSCTL)
    assert "nameserver 1.1.1.1\n" in str(files["/etc/resolv.conf"].content)
    assert {item.name for item in items if isinstance(item, Package)} == {
        "chrony",
        "ca-certificates",
    }
    units = {item.name: item for item in items if isinstance(item, Unit)}
    assert units.pop("chrony.service").enabled is True
    assert sorted(units) == sorted(name for name, _ in TEE_MASKED_UNITS)
    assert all(unit.masked for unit in units.values())


def test_each_piece_opts_out() -> None:
    assert _items(TeeBase(time=False, journal=False, sysctl=False, dns=False, mask=False)) == []
    hypervisor_free = _items(TeeBase(hypervisor_clocks=(), journal=False, sysctl=False))
    chrony = next(i for i in hypervisor_free if isinstance(i, File) and "chrony" in i.path)
    assert "169.254" not in str(chrony.content)
    with pytest.raises(ValidationError, match="NTS server"):
        _items(TeeBase(nts_servers=()))
