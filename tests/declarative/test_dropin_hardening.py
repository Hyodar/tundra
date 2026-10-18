"""``DropIn(hardening=...)`` is a patch: merged over the unit it extends (base text, then the
drop-in, read through the lossless unit model), the effective sandbox is what the patch says."""

from __future__ import annotations

import pytest

from tundravm._units.text import UnitText
from tundravm.declarative import DropIn, Hardening, Service, Unit
from tundravm.testing import fragment_plan

# A switch's value when no line sets it (systemd's default).
SWITCHES = dict.fromkeys(
    (
        "ProtectSystem",
        "ProtectHome",
        "PrivateTmp",
        "PrivateDevices",
        "NoNewPrivileges",
        "RestrictSUIDSGID",
        "MemoryDenyWriteExecute",
        "ProtectKernelModules",
        "ProtectKernelTunables",
        "ProtectControlGroups",
        "ProtectKernelLogs",
        "ProtectClock",
        "ProtectHostname",
        "RestrictRealtime",
        "RestrictNamespaces",
        "LockPersonality",
    ),
    "no",
) | {"DevicePolicy": "auto", "UMask": "0022"}
# Lists: an empty assignment resets them; unset means unrestricted.
LISTS = (
    "DeviceAllow",
    "RestrictAddressFamilies",
    "SystemCallArchitectures",
    "SystemCallFilter",
    "AmbientCapabilities",
)
DEVICE = ("/dev/tdx_guest",)
PRESETS = [
    Hardening.strict(),
    Hardening.strict(jit=True, devices=DEVICE, capabilities=("CAP_DAC_OVERRIDE",)),
    Hardening.relaxed(),
    Hardening.none(),
]


def base(hardening: Hardening) -> str:
    image = fragment_plan(Service("app", "/usr/bin/app", hardening=hardening))
    return image.file("/usr/lib/systemd/system/app.service").text


def merged(under: Hardening, patch: Hardening) -> str:
    image = fragment_plan(Unit("app.service", dropin=DropIn(hardening=patch)))
    return base(under) + image.file("/usr/lib/systemd/system/app.service.d/20-tundravm.conf").text


def effective(text: str) -> dict[str, object]:
    """The sandbox *text* configures: last switch wins, lists honour their resets."""
    unit = UnitText.parse(text)
    lines = list(unit.directives("Service"))
    out: dict[str, object] = {}
    for key, default in SWITCHES.items():
        values = [d.value for d in lines if d.key == key]
        out[key] = values[-1] if values else default
    for key in LISTS:
        out[key] = unit.values("Service", key)
    caps = unit.values("Service", "CapabilityBoundingSet")
    # unset is every capability; "CapabilityBoundingSet=" alone is the empty set; "~" is all
    out["CapabilityBoundingSet"] = (
        caps if any(d.key == "CapabilityBoundingSet" for d in lines) else ("~",)
    )
    return out


@pytest.mark.parametrize("under", PRESETS)
@pytest.mark.parametrize("preset", PRESETS)
def test_a_preset_replaces_the_units_sandbox(under: Hardening, preset: Hardening) -> None:
    assert effective(merged(under, preset)) == effective(base(preset))


def test_jit_cancels_an_inherited_memory_deny_write_execute() -> None:
    sandbox = effective(merged(Hardening.strict(), Hardening(jit=True)))
    assert sandbox == effective(base(Hardening.strict(jit=True)))
    assert sandbox["MemoryDenyWriteExecute"] == "no"
    assert sandbox["SystemCallFilter"] == ("@system-service", "~@privileged @resources")


def test_allowing_a_device_cancels_private_devices() -> None:
    sandbox = effective(merged(Hardening.strict(), Hardening(devices=DEVICE)))
    assert sandbox["PrivateDevices"] == "no"
    assert sandbox["DevicePolicy"] == "closed"
    assert sandbox["DeviceAllow"] == ("/dev/tdx_guest rw",)


def test_none_removes_the_sandbox() -> None:
    sandbox = effective(merged(Hardening.strict(), Hardening.none()))
    assert sandbox == effective("[Service]\n")


def test_lists_are_replaced_not_extended() -> None:
    under = Hardening.strict(capabilities=("CAP_DAC_OVERRIDE",), network="unix")
    patch = Hardening(capabilities=("CAP_NET_BIND_SERVICE",), network="inet", syscalls=())
    sandbox = effective(merged(under, patch))
    assert sandbox["CapabilityBoundingSet"] == ("CAP_NET_BIND_SERVICE",)
    assert sandbox["AmbientCapabilities"] == ("CAP_NET_BIND_SERVICE",)
    assert sandbox["RestrictAddressFamilies"] == ("AF_UNIX AF_INET AF_INET6",)
    assert sandbox["SystemCallFilter"] == ("~@privileged @resources",)


def test_a_patch_renders_only_the_fields_it_sets() -> None:
    image = fragment_plan(Unit("app.service", dropin=DropIn(hardening=Hardening(jit=True))))
    text = image.file("/usr/lib/systemd/system/app.service.d/20-tundravm.conf").text
    assert text == "[Service]\nMemoryDenyWriteExecute=no\n"
    assert Hardening(jit=True) == Hardening.strict(jit=True)  # same unit, different patch
