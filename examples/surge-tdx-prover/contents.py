"""File contents the surge-tdx-prover image ships, byte-exact with the committed tree.

The system files live under ``files/`` (``image.py`` copies them) and are read
from there. Strings keep their trailing newline only where the shipped file has one.
Unit bodies omit the ``After=``/``Requires=runtime-init.service`` that ``after_init`` adds.
The base layer and the service fragments own their files; they are re-exported
here so ``tests/compiler/test_surge_contents.py`` checks every constant against ``mkosi/default/``.
"""

from __future__ import annotations

from examples.fragments.nethermind import NETHERMIND_ENV, NETHERMIND_UNIT
from examples.fragments.raiko import RAIKO_ENV, RAIKO_UNIT
from examples.fragments.taiko_client import TAIKO_CLIENT_ENV, TAIKO_CLIENT_UNIT
from examples.nethermind_base import NETWORK_SETUP_SERVICE, RESOLV_CONF, TDX_INIT

from tundravm.declarative import Resources

# ── System configuration (mkosi.extra/etc/), shipped from files/ by image.py ──

_ASSETS = Resources.from_module(__name__)
DROPBEAR_CONFIG = _ASSETS.text("files/etc/default/dropbear")
SYSCTL_CONF = _ASSETS.text("files/etc/sysctl.d/99-surge.conf")
TDX_GUEST_PERMISSIONS = _ASSETS.text("files/etc/udev/rules.d/65-tdx-guest.rules")
TDX_GUEST_SYMLINK = _ASSETS.text("files/etc/udev/rules.d/99-tdx-symlink.rules")
OPENNTPD_CONF = _ASSETS.text("files/etc/openntpd/ntpd.conf")
PROMETHEUS_DEFAULTS = _ASSETS.text("files/etc/default/prometheus")

__all__ = [
    "DROPBEAR_CONFIG",
    "NETHERMIND_ENV",
    "NETHERMIND_UNIT",
    "NETWORK_SETUP_SERVICE",
    "OPENNTPD_CONF",
    "PROMETHEUS_DEFAULTS",
    "RAIKO_ENV",
    "RAIKO_UNIT",
    "RESOLV_CONF",
    "SYSCTL_CONF",
    "TAIKO_CLIENT_ENV",
    "TAIKO_CLIENT_UNIT",
    "TDX_GUEST_PERMISSIONS",
    "TDX_GUEST_SYMLINK",
    "TDX_INIT",
]
