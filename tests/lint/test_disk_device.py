"""``disk-device-guessed``/``disk-device-unstable``: a formatted disk without a stable device."""

from __future__ import annotations

import pytest

from tundravm.check import check
from tundravm.declarative import (
    Disk,
    Git,
    Key,
    Package,
    Policy,
    Recipe,
    RuntimeTools,
    lint,
    lower,
)

TOOLS = RuntimeTools(Git("https://github.com/Hyodar/tundra-tools", "a" * 40))
KEY = Key("key_persistent", output="/run/keys/persistent")


def _findings(
    disk: Disk, policy: Policy | None = None, code: str = "disk-device-guessed"
) -> list[tuple[str, str | None]]:
    recipe = Recipe(
        "disk",
        items=(Package("linux-image-amd64"), TOOLS, KEY, disk),
        policy=policy,
    )
    return [(d.severity, d.subject) for d in lint(recipe) if d.code == code]


def test_a_disk_is_never_formatted_by_default() -> None:
    assert Disk("data", mount="/data").format == "never"


@pytest.mark.parametrize("format", ["on_fail", "always", "on_initialize"])
def test_formatting_without_a_device_is_an_error(format: str) -> None:
    disk = Disk("data", mount="/data", key=KEY, format=format)  # type: ignore[arg-type]
    assert _findings(disk) == [("error", "data")]


def test_the_error_names_the_boot_disk_risk_and_the_fix() -> None:
    disk = Disk("data", mount="/data", format="on_fail")
    recipe = Recipe("disk", items=(Package("linux-image-amd64"), TOOLS, disk))
    (found,) = (d for d in check(lower(recipe)) if d.code == "disk-device-guessed")
    assert "automatic device selection can pick the boot disk and format it" in found.message
    assert "format='on_fail'" in found.message
    hint = found.hint or ""
    assert "/dev/disk/by-id/virtio-SERIAL" in hint and "storage_safety='warn'" in hint


@pytest.mark.parametrize(
    "disk",
    [
        Disk("data", mount="/data", key=KEY, device="/dev/disk/by-id/virtio-data", format="always"),
        Disk("data", mount="/data", key=KEY, device="/dev/vdb", format="on_initialize"),
        Disk("data", mount="/data", key=KEY),
    ],
    ids=["by-id", "virtio-name", "never-format"],
)
def test_a_stable_device_or_no_formatting_is_clean(disk: Disk) -> None:
    assert _findings(disk) == []
    assert _findings(disk, code="disk-device-unstable") == []


def test_a_scsi_kernel_name_is_unstable() -> None:
    disk = Disk("data", mount="/data", key=KEY, device="/dev/sdb", format="on_fail")
    assert _findings(disk) == []
    assert _findings(disk, code="disk-device-unstable") == [("warning", "data")]
    never = Disk("data", mount="/data", key=KEY, device="/dev/sdb")
    assert _findings(never, code="disk-device-unstable") == []


def test_storage_safety_warn_demotes_it() -> None:
    disk = Disk("data", mount="/data", key=KEY, format="on_fail")
    assert _findings(disk, Policy(storage_safety="warn")) == [("warning", "data")]
    assert _findings(disk, Policy(storage_safety="error")) == [("error", "data")]
