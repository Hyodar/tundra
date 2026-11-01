"""QEMU firmware search paths, read by ``run``, ``doctor`` and the boot harness."""

import os
from collections.abc import Callable

# Common OVMF firmware paths
OVMF_CODE_PATHS = (
    "/usr/share/OVMF/OVMF_CODE.fd",
    "/usr/share/edk2/ovmf/OVMF_CODE.fd",
    "/usr/share/qemu/OVMF_CODE.fd",
    "/usr/share/OVMF/OVMF_CODE_4M.fd",
)

OVMF_VARS_PATHS = (
    "/usr/share/OVMF/OVMF_VARS.fd",
    "/usr/share/edk2/ovmf/OVMF_VARS.fd",
    "/usr/share/qemu/OVMF_VARS.fd",
    "/usr/share/OVMF/OVMF_VARS_4M.fd",
)

# A TDX guest boots TDVF (OVMF built with TDX support) as one image through -bios.
TDVF_PATHS = (
    "/usr/share/ovmf/OVMF.fd",
    "/usr/share/edk2/ovmf/OVMF.inteltdx.fd",
    "/usr/share/OVMF/OVMF.fd",
    "/usr/share/qemu/OVMF.fd",
)


def ovmf_firmware(exists: Callable[[str], bool] = os.path.isfile) -> tuple[str, str] | None:
    """The first OVMF ``(code, vars)`` pair that *exists*, or ``None``: a simulated boot
    needs both."""
    pairs = zip(OVMF_CODE_PATHS, OVMF_VARS_PATHS, strict=True)
    return next(((c, v) for c, v in pairs if exists(c) and exists(v)), None)
