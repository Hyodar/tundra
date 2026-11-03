"""The ACPI power button: ``tundravm-power-button.service``.

A cloud stop and QEMU's ``system_powerdown`` press the ACPI power button and cut the power
after a grace period. ``systemd-logind`` would handle it, but it needs D-Bus, which no image
runs. Every image runs Debian's ``acpid`` instead, reading the kernel's ACPI netlink events
(the ``button`` driver, a curated module), with one rule: the power button runs
``systemctl poweroff``, so services stop, volumes unmount and the dm-crypt mappings close
before the power goes. Azure's shutdown request needs none of this: ``hv_utils`` runs
``/sbin/poweroff`` itself.
"""

from __future__ import annotations

from tundravm.models import ServiceSpec

UNIT = "tundravm-power-button.service"
PACKAGE = "acpid"
RULES = "/usr/lib/tundravm/acpi"
"""The rule directory acpid reads: not ``/etc/acpi/events``, which the package's
``acpid.path`` watches."""
RULE_PATH = f"{RULES}/power-button"
RULE = "event=button[ /]power\naction=/usr/bin/systemctl poweroff\n"
"""The one rule: ``button/power`` (the fixed-hardware ``LNXPWRBN`` or the ``PNP0C0C``
device) powers the guest off in order."""
MASKED_UNITS = ("acpid.path", "acpid.service", "acpid.socket")
"""The package's own units, which its postinst enables: they would start a second acpid,
unsandboxed, on ``/etc/acpi/events``."""
RUNTIME = "tundravm-power-button"

_HARDENING: tuple[tuple[str, str], ...] = (
    ("ProtectSystem", "strict"),
    ("ProtectHome", "yes"),
    ("PrivateTmp", "yes"),
    ("NoNewPrivileges", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectKernelLogs", "yes"),
    ("ProtectControlGroups", "yes"),
    ("ProtectClock", "yes"),
    ("ProtectHostname", "yes"),
    ("RestrictNamespaces", "yes"),
    ("RestrictRealtime", "yes"),
    ("LockPersonality", "yes"),
    ("MemoryDenyWriteExecute", "yes"),
    ("RestrictAddressFamilies", "AF_NETLINK AF_UNIX"),
    ("SystemCallArchitectures", "native"),
    ("CapabilityBoundingSet", ""),
)
"""Root with no capability: it reads the ACPI netlink group, which needs none, and asks
systemd to power off over ``/run/systemd/private``, which takes uid 0. No
``PrivateNetwork=``: the kernel sends ACPI events in the host's network namespace only."""


def service() -> ServiceSpec:
    """The unit: acpid in the foreground on netlink, with no client socket."""
    return ServiceSpec(
        name=UNIT,
        command=(
            "/usr/sbin/acpid",
            "--foreground",
            "--netlink",
            "--nosocket",
            "--logevents",
            "--confdir",
            RULES,
            "--pidfile",
            f"/run/{RUNTIME}/acpid.pid",
        ),
        description="Power off on the ACPI power button",
        restart="always",
        hardening=_HARDENING,
        needs=(),
        extra_unit={"Service": {"RuntimeDirectory": RUNTIME}},
    )


__all__ = ["MASKED_UNITS", "PACKAGE", "RULE", "RULE_PATH", "UNIT", "service"]
