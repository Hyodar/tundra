"""The execution gate (plan 6.1): ``tdx-evidence.service`` and what a profile's mode makes of it.

Every tree ships the gate, a root oneshot (``RemainAfterExit=yes``) the compiler enables into
the boot target, and every unit lowered from a ``Service`` (and every scheduled ``Task``)
gets ``Requires=`` and ``After=`` on it. The gate checks positively for a TDX quote path:
the ``tdx_guest`` CPU flag, then ``/dev/tdx_guest`` or configfs-tsm's report directory
(waiting up to :data:`WAIT_SECONDS` for udev to load ``tdx_guest``). ``ConditionSecurity=``
is not used: it knows ``cvm``, not ``tdx``, and a failed condition does not propagate
through ``Requires=``; a failed start does.

- A production profile (``Variant(release=True)``): no quote path is a failed start, with
  :data:`REFUSED` on the console, and systemd starts none of the gated units ("Dependency
  failed"). No fallback exists.
- A development profile (``release=False``): no quote path is :data:`SIMULATED` in
  :data:`MODE_FILE` and a successful start, so the profile runs under plain QEMU.
- Either, with a quote path: ``tdx`` in :data:`MODE_FILE`.

Not gated, on purpose: boot ``Task`` units (probes, waits, host keys), packaged and
verbatim ``Unit`` declarations, mounts, ``runtime-init.service`` and its steps (key
generation, disk setup, secret delivery: the control plane, which must be up before any
application and is what an operator reads when the gate refuses), and the platform glue
the cloud targets add (``gcp-metadata-dns``).

The gate does not ask for a test quote: a quote round-trip goes through the host's quote
service (QGS) and configfs, which the systemd debloat leaves unmounted; ``tdxs`` makes the
first quote and the verifier is what proves it. The gate's mode is also what the operator
side reads (``attest`` takes :data:`MODE_FILE` over the session), which can only turn
evidence into ``simulated``, never the reverse.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from tundravm.models import ServiceSpec

Execution = Literal["production", "development"]

GATE_UNIT = "tdx-evidence.service"
GATE_SCRIPT = "/usr/lib/tundravm/tdx-evidence"
GATE_DROPIN = f"/usr/lib/systemd/system/{GATE_UNIT}.d/50-tundravm-execution.conf"
"""The drop-in a profile whose mode differs from the payload's sets its mode through."""
EXECUTION_ENV = "TUNDRAVM_EXECUTION"
MODE_FILE = "/run/tundravm/mode"
TDX = "tdx"
SIMULATED = "simulated"
TSM_REPORT = "/sys/kernel/config/tsm/report"
TDX_GUEST = "/dev/tdx_guest"
WAIT_SECONDS = 30
REFUSED = "tdx-evidence: refused: no TDX quote path; a production profile starts no services"
"""The console line of a production boot without TDX (CI's fixture boot asserts it)."""
ALLOWED = "tdx-evidence: no TDX quote path; development profile, running simulated"

SCRIPT = f"""\
#!/bin/sh
# tundravm execution gate (plan 6.1): the services require this unit. A production profile
# without a TDX quote path fails it; a development profile records "{SIMULATED}" instead.
set -u
execution=${{{EXECUTION_ENV}:-production}}
record() {{
    mkdir -p /run/tundravm
    printf '%s\\n' "$1" > {MODE_FILE}.tmp && mv {MODE_FILE}.tmp {MODE_FILE}
}}
quote_path() {{
    grep -qw tdx_guest /proc/cpuinfo || return 1
    waited=0
    while [ "$waited" -lt {WAIT_SECONDS} ]; do
        if [ -c {TDX_GUEST} ]; then echo {TDX_GUEST}; return 0; fi
        if [ -d {TSM_REPORT} ]; then echo {TSM_REPORT}; return 0; fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}}
if found=$(quote_path); then
    record {TDX}
    echo "tdx-evidence: TDX quote path $found"
    exit 0
fi
if [ "$execution" = development ]; then
    record {SIMULATED}
    echo "{ALLOWED}"
    exit 0
fi
echo "{REFUSED}"
exit 1
"""
"""``/usr/lib/tundravm/tdx-evidence``: ``/bin/sh`` and coreutils only."""

_HARDENING: tuple[tuple[str, str], ...] = (
    ("ProtectSystem", "full"),
    ("ProtectHome", "yes"),
    ("PrivateTmp", "yes"),
    ("PrivateNetwork", "yes"),
    ("NoNewPrivileges", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectControlGroups", "yes"),
    ("RestrictSUIDSGID", "yes"),
    ("MemoryDenyWriteExecute", "yes"),
    ("ProtectKernelLogs", "yes"),
    ("ProtectClock", "yes"),
    ("ProtectHostname", "yes"),
    ("RestrictRealtime", "yes"),
    ("RestrictNamespaces", "yes"),
    ("LockPersonality", "yes"),
    ("RestrictAddressFamilies", "AF_UNIX"),
    ("SystemCallArchitectures", "native"),
    ("CapabilityBoundingSet", ""),
)
"""Root without capabilities, no network. No ``PrivateDevices=``: it would hide the guest
device the gate looks for."""


def service(execution: Execution) -> ServiceSpec:
    """The gate's unit in a profile whose mode is *execution*."""
    return ServiceSpec(
        name=GATE_UNIT,
        command=(GATE_SCRIPT,),
        description="TDX quote path the services require",
        after=("systemd-udev-trigger.service",),
        type="oneshot",
        remain_after_exit=True,
        restart="no",
        restart_sec="",
        env={EXECUTION_ENV: execution},
        timeout_start=f"{WAIT_SECONDS * 2}s",
        hardening=_HARDENING,
        needs=(),
        extra_unit={"Service": {"StandardOutput": "journal+console"}},
    )


def dropin(execution: Execution) -> str:
    """The drop-in that gives a profile mode *execution* over the payload's gate."""
    return f"[Service]\nEnvironment={EXECUTION_ENV}={execution}\n"


def execution(release: bool) -> Execution:
    """A ``Variant(release=)``'s mode."""
    return "production" if release else "development"


def gated(spec: ServiceSpec) -> ServiceSpec:
    """*spec* requiring and ordered after the gate, after its own relations."""
    return replace(
        spec,
        after=(*(u for u in spec.after if u != GATE_UNIT), GATE_UNIT),
        requires=(*(u for u in spec.requires if u != GATE_UNIT), GATE_UNIT),
    )


__all__ = [
    "ALLOWED",
    "GATE_DROPIN",
    "GATE_SCRIPT",
    "GATE_UNIT",
    "MODE_FILE",
    "REFUSED",
    "SCRIPT",
    "SIMULATED",
    "TDX",
    "Execution",
    "dropin",
    "execution",
    "gated",
    "service",
]
