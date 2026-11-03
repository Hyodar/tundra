"""The initramfs root's own mode: ``tundravm-root-mode.service``.

The initramfs layout runs the whole OS from the UKI's initrd, which the kernel unpacks
into its rootfs, a tmpfs. mkosi's cpio carries no entry for ``/`` itself, so ``/`` keeps
tmpfs's default, ``1777``: anyone writes beside ``/etc``, and sshd refuses every key file
under ``/etc/ssh`` ("bad ownership or modes for directory /"). A remount cannot fix it
(tmpfs ignores ``mode=`` on remount), so every initramfs-rooted tree ships this root
oneshot, ordered before ``sysinit.target`` and so before every other unit, which makes
``/`` ``root:root 0755``. A verity root is an overlay over the read-only erofs root, whose
``/`` is already ``0755``; the boot probe's ``root`` row checks both.
"""

from __future__ import annotations

from tundravm.models import ServiceSpec

UNIT = "tundravm-root-mode.service"
MODE = "0755"

_HARDENING: tuple[tuple[str, str], ...] = (
    ("PrivateNetwork", "yes"),
    ("NoNewPrivileges", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectControlGroups", "yes"),
    ("ProtectKernelLogs", "yes"),
    ("ProtectClock", "yes"),
    ("ProtectHostname", "yes"),
    ("RestrictRealtime", "yes"),
    ("RestrictNamespaces", "yes"),
    ("LockPersonality", "yes"),
    ("MemoryDenyWriteExecute", "yes"),
    ("RestrictAddressFamilies", "AF_UNIX"),
    ("SystemCallArchitectures", "native"),
    ("CapabilityBoundingSet", "CAP_CHOWN CAP_FOWNER"),
)
"""Root with the two capabilities ``chown`` and ``chmod`` on ``/`` need, and nothing else;
no ``ProtectSystem=``, which would make ``/`` read-only to it."""


def service() -> ServiceSpec:
    """The unit: ``/`` root:root 0755, before ``sysinit.target``."""
    return ServiceSpec(
        name=UNIT,
        command=("/usr/bin/chmod", MODE, "/"),
        exec_start_pre=("/usr/bin/chown root:root /",),
        description="Make the initramfs root root:root 0755",
        before=("sysinit.target",),
        type="oneshot",
        remain_after_exit=True,
        restart="no",
        restart_sec="",
        timeout_start="30s",
        hardening=_HARDENING,
        needs=(),
        extra_unit={"Unit": {"DefaultDependencies": "no"}},
    )
