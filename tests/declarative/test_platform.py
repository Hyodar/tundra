"""``Recipe.platform``: the distribution, its boot set, a pinned EFI stub and its items."""

from __future__ import annotations

import pytest

from tundravm.declarative import (
    Debian,
    Package,
    Platform,
    Recipe,
    Setting,
    Variant,
    lower,
    resolve,
)
from tundravm.declarative.resolve import PLATFORM_LEVEL, provenance
from tundravm.declarative.utils import EfiStub
from tundravm.errors import ValidationError

SNAPSHOT = "20251113T083151Z"
ARCHIVE = f"https://snapshot.debian.org/archive/debian/{SNAPSHOT}/"


def test_boot_modes_pick_the_package_set() -> None:
    assert Debian("trixie").packages == (
        "linux-image-amd64",
        "systemd",
        "systemd-sysv",
        "udev",
        "kmod",
        "systemd-boot-efi",
    )
    assert Debian("trixie", boot="uki", kernel_package=None).packages == (
        "kmod",
        "systemd",
        "systemd-boot-efi",
    )
    assert Debian("trixie", boot=None).packages == ()
    assert Debian("trixie").name == "debian/trixie"


def test_the_efi_stub_comes_from_the_platform_archive() -> None:
    def stub(platform: Debian) -> object:
        return next(i for i in platform.contribution() if isinstance(i, EfiStub))

    pinned = Debian("trixie", snapshot=SNAPSHOT, efi_stub="257.8-1~deb13u1")
    assert stub(pinned) == EfiStub(snapshot=SNAPSHOT, version="257.8-1~deb13u1")
    rooted = Debian("trixie", snapshot=SNAPSHOT, mirror="https://m.example/", efi_stub="1")
    assert stub(rooted) == EfiStub(
        snapshot=f"https://m.example/archive/debian/{SNAPSHOT}", version="1"
    )
    assert stub(Debian("trixie", mirror=ARCHIVE, efi_stub="1")) == EfiStub(
        snapshot=ARCHIVE, version="1"
    )
    with pytest.raises(ValidationError, match="efi_stub needs the archive"):
        Debian("trixie", efi_stub="1")


@pytest.mark.parametrize(
    "make",
    [
        lambda: Debian("debian/trixie"),
        lambda: Debian("trixie", snapshot=ARCHIVE),
        lambda: Debian("trixie", boot="grub"),  # type: ignore[arg-type]
        lambda: Debian("trixie", epoch=-1),
        lambda: Platform("trixie"),
        lambda: Recipe("r", platform="debian/trixie"),  # type: ignore[arg-type]
    ],
)
def test_malformed_platforms_are_rejected(make: object) -> None:
    with pytest.raises(ValidationError):
        make()  # type: ignore[operator]


def test_every_variant_gets_the_platform_with_its_provenance() -> None:
    recipe = Recipe(
        "r",
        platform=Debian("trixie", items=(Setting("Output", "CompressOutput", ("zstd",)),)),
        items=(Package("curl"),),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("rescue", inherits=None, outputs=("qemu",)),
            Variant("no-udev", changes=(Package.ref("udev").remove(),)),
        ),
    )
    rescue = resolve(recipe, variant="rescue").items
    assert Package("linux-image-amd64") in rescue and Package("curl") not in rescue
    assert Package("udev") not in resolve(recipe, variant="no-udev").items
    origins = provenance(recipe, variant="default")[("Package", "systemd", "runtime")]
    assert [(o.action, o.variant) for o in origins] == [("declared", PLATFORM_LEVEL)]
    assert origins[0].describe() == "declared in the platform"
    assert lower(recipe).base == "debian/trixie"
