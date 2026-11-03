"""Curated kernel modules: ``Debian(kernel_modules=...)``, ``KernelModules=``, debloat."""

import subprocess
from pathlib import Path

import pytest

from tundravm.compiler.emit_mkosi import drivers_check
from tundravm.declarative import Debian, Git, Kernel, Recipe, Variant
from tundravm.declarative.model import Target
from tundravm.errors import ValidationError
from tundravm.kernel_modules import (
    CONTAINER_MODULES,
    DISK_MODULES,
    FIREWALL_MODULES,
    MODULE_OPTIONS,
    TARGET_MODULES,
    TDX_GUEST_MODULES,
    VERITY_MODULES,
    CloudDrivers,
)
from tundravm.testing._model import compile_tree

RM_MODULES = 'rm -rf "$BUILDROOT/usr/lib/modules"'


def _recipe(*outputs: Target, **platform: object) -> Recipe:
    return Recipe(
        "mods",
        variants=(Variant("default", outputs=outputs or ("qemu",)),),
        platform=Debian("trixie", **platform),  # type: ignore[arg-type]
    )


def _listed(conf: str) -> list[str]:
    lines = conf.splitlines()
    if "KernelModules=" not in lines:
        return []
    start = lines.index("KernelModules=") + 1
    names = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        names.append(line.strip())
    return names


BUILT_KERNEL = Recipe(
    "built",
    items=(Kernel("6.12.1", Git("https://github.com/gregkh/linux", "v6.12.1")),),
    variants=(Variant("default"),),
    platform=Debian("trixie", kernel_package=None),
)


@pytest.mark.parametrize(
    ("recipe", "listed", "in_finalize", "not_in_finalize"),
    [
        (
            _recipe(),
            sorted(TDX_GUEST_MODULES),
            ("for module in efivarfs nvme sd_mod tdx_guest",),
            (RM_MODULES,),
        ),
        (
            _recipe(kernel_modules=("vsock", "tun")),
            sorted({*TDX_GUEST_MODULES, "vsock", "tun"}),
            (),
            (),
        ),
        (_recipe(kernel_modules="all"), [], (), ("Kernel modules", RM_MODULES)),
        (BUILT_KERNEL, [], (RM_MODULES,), ()),
    ],
    ids=["qemu-default", "tuple-adds", "all", "built-kernel"],
)
def test_the_selection_lists_modules_and_debloat_follows(
    tmp_path: Path,
    recipe: Recipe,
    listed: list[str],
    in_finalize: tuple[str, ...],
    not_in_finalize: tuple[str, ...],
) -> None:
    tree = compile_tree(recipe, path=tmp_path)
    assert _listed(tree.conf()) == listed
    finalize = tree.script("finalize")
    for text in in_finalize:
        assert text in finalize
    for text in not_in_finalize:
        assert text not in finalize


@pytest.mark.parametrize(
    ("platform", "message"),
    [
        ({"kernel_modules": ("virtio*",)}, "is not a module name"),
        ({"kernel_modules": "some"}, "must be 'all' or a tuple"),
        ({"kernel_modules": ("x.ko",)}, "is not a module name"),
        ({"kernel_package": None, "kernel_modules": ("vsock",)}, "does not install"),
    ],
    ids=["glob", "word", "ko-suffix", "no-distribution-kernel"],
)
def test_invalid_selections_are_rejected(platform: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Debian("trixie", **platform)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("targets", "required", "absent"),
    [
        (
            ("qemu",),
            {
                "CONFIG_TDX_GUEST_DRIVER": "qemu",
                "CONFIG_VIRTIO_NET": "qemu",
                "CONFIG_DM_CRYPT": "an encrypted Volume",
                "CONFIG_CRYPTO_CRC32C": "the Firewall, RootFS.verity(), a Container",
            },
            {"CONFIG_GVE", "CONFIG_HYPERV", "CONFIG_BLK_DEV_PCIESSD_MTIP32XX"},
        ),
        (("gcp",), {"CONFIG_GVE": "gcp", "CONFIG_BLK_DEV_NVME": "gcp"}, {"CONFIG_HYPERV"}),
        (
            ("qemu", "azure", "gcp"),
            {"CONFIG_MICROSOFT_MANA": "azure", "CONFIG_SCSI_VIRTIO": "qemu, azure, gcp"},
            set(),
        ),
    ],
    ids=["qemu", "gcp", "every-target"],
)
def test_cloud_drivers_derive_from_the_module_tables(
    targets: tuple[str, ...], required: dict[str, str], absent: set[str]
) -> None:
    curated = {
        *TDX_GUEST_MODULES,
        *DISK_MODULES,
        *FIREWALL_MODULES,
        *VERITY_MODULES,
        *CONTAINER_MODULES,
    }
    curated.update(*TARGET_MODULES.values())
    assert set(MODULE_OPTIONS) == curated
    drivers = dict(CloudDrivers(targets).required)
    assert required.items() <= drivers.items()
    assert not absent & set(drivers)
    assert CloudDrivers(targets).denied == ("CONFIG_BLK_DEV_PCIESSD_MTIP32XX",)
    with pytest.raises(ValidationError, match="is not a target"):
        CloudDrivers(("aws",))


GCP_DRIVERS = CloudDrivers(("gcp",))
BUILT_IN = "".join(f"{option}=y\n" for option, _ in GCP_DRIVERS.required)


@pytest.mark.parametrize(
    ("config", "said"),
    [
        (BUILT_IN, ""),
        (
            BUILT_IN.replace("CONFIG_GVE=y", "CONFIG_GVE=m"),
            "kernel-driver-missing: CONFIG_GVE is m, gcp needs it built in: "
            "add CONFIG_GVE=y to the kernel's config (Kernel.build(config=...))",
        ),
        (BUILT_IN.replace("CONFIG_DM_VERITY=y\n", ""), "CONFIG_DM_VERITY is n, RootFS.verity()"),
        (
            BUILT_IN + "CONFIG_BLK_DEV_PCIESSD_MTIP32XX=y\n",
            "kernel-driver-denied: CONFIG_BLK_DEV_PCIESSD_MTIP32XX is y, no target offers the "
            "device: add '# CONFIG_BLK_DEV_PCIESSD_MTIP32XX is not set'",
        ),
    ],
    ids=["built-in", "module", "unset", "denied"],
)
def test_the_drivers_check_reads_the_final_config(tmp_path: Path, config: str, said: str) -> None:
    (tmp_path / ".config").write_text(config, encoding="utf-8")
    script = f"set -eu\nKERNEL_CACHE=.\n{drivers_check(GCP_DRIVERS)}\n[ $missing -eq 0 ]\n"
    done = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert (done.returncode != 0) == bool(said)
    assert said in done.stderr
    report = (tmp_path / "kernel-drivers.txt").read_text(encoding="utf-8").splitlines()
    assert len(report) == len(GCP_DRIVERS.required) + len(GCP_DRIVERS.denied)
    gve = "m" if "GVE=m" in config else "y"
    assert f"CONFIG_GVE={gve} required by gcp" in report
