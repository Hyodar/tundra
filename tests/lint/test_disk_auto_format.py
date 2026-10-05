"""``disk-auto-format``: a disk that picks its device at boot and may format it."""

from __future__ import annotations

import pytest

from tundravm.check import check
from tundravm.declarative import (
    Disk,
    Fragment,
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


def _findings(disk: Disk, policy: Policy | None = None) -> list[tuple[str, str | None]]:
    recipe = Recipe(
        "disk",
        Fragment("disk", items=(Package("linux-image-amd64"), TOOLS, KEY, disk)),
        policy=policy,
    )
    return [(d.level, d.subject) for d in lint(recipe) if d.code == "disk-auto-format"]


@pytest.mark.parametrize("format", ["on_fail", "always", "on_initialize"])
def test_automatic_device_with_formatting_warns(format: str) -> None:
    disk = Disk("data", mount="/data", key=KEY, format=format)  # type: ignore[arg-type]
    assert _findings(disk) == [("warning", "data")]


def test_the_warning_names_the_boot_disk_risk_and_the_fix() -> None:
    recipe = Recipe(
        "disk",
        Fragment("disk", items=(Package("linux-image-amd64"), TOOLS, Disk("data", mount="/data"))),
    )
    (found,) = (d for d in check(lower(recipe)) if d.code == "disk-auto-format")
    assert "automatic device selection can pick the boot disk and format it" in found.message
    assert "format='on_fail'" in found.message
    assert "device=" in (found.hint or "") and "storage_safety='error'" in (found.hint or "")


@pytest.mark.parametrize(
    "disk",
    [
        Disk("data", mount="/data", key=KEY, device="/dev/sdb"),
        Disk("data", mount="/data", key=KEY, device="/dev/sdb", format="always"),
        Disk("data", mount="/data", key=KEY, format="never"),
    ],
    ids=["explicit-device", "explicit-device-always", "never-format"],
)
def test_an_explicit_device_or_no_formatting_is_clean(disk: Disk) -> None:
    assert _findings(disk) == []


def test_storage_safety_error_promotes_it() -> None:
    disk = Disk("data", mount="/data", key=KEY)
    assert _findings(disk, Policy(storage_safety="error")) == [("error", "data")]
    assert _findings(disk, Policy(storage_safety="warn")) == [("warning", "data")]
