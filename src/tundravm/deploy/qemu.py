"""QEMU firmware search paths, read by ``run``, ``doctor`` and the boot harness."""

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
