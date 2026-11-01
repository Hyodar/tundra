"""The instance key: a private key every image generates at boot, for the services that ask.

Every tree ships ``tundravm-instance-key.service``, a root oneshot (``RemainAfterExit=yes``)
enabled into the boot target and ordered before the execution gate, so before every unit a
``Service`` lowers to. It runs :data:`SCRIPT_PATH`, which generates an ECDSA key on the
secp256k1 curve (the curve Ethereum signs with, so an address derives from it) with
``openssl``, fresh on every boot and on tmpfs only:

- :data:`KEY`, root:root 0600, the private key as SEC1 PEM (``BEGIN EC PRIVATE KEY``);
- :data:`PUBLIC`, world-readable, the compressed public key (33 bytes) as one hex line;
- one console line, :data:`CONSOLE` and the hex, which ``run status`` reads.

tundra-tools' ``key-gen`` makes random bytes and pipe keys only, so ``openssl`` (the
``openssl`` package, which every image then carries) makes this one. It starts no network
and writes nothing a production boot would not, so production profiles carry it too.

A service reads the key as a systemd credential: ``Service(credentials=[InstanceKey()])``
renders ``LoadCredential=instance-key:/run/tundravm/instance-key.pem`` with ``Requires=``
and ``After=`` on :data:`UNIT` (:mod:`tundravm.secrets`). Nothing else may name
:data:`KEY` (the ``instance-key-exposed`` rule). The quote does not bind the public key yet;
that is tundra-tools work (plan 6.3).
"""

from __future__ import annotations

import re

from tundravm.models import ServiceSpec

from . import execution

UNIT = "tundravm-instance-key.service"
SCRIPT_PATH = "/usr/lib/tundravm/instance-key"
KEY = "/run/tundravm/instance-key.pem"
PUBLIC = "/run/tundravm/instance-key.pub"
CREDENTIAL = "instance-key"
"""The credential name a bound service reads under ``$CREDENTIALS_DIRECTORY``."""
CURVE = "secp256k1"
PACKAGE = "openssl"
CONSOLE = "tundravm-instance-key: pub "
"""The console line's prefix; the compressed public key in hex follows."""
PUBLIC_KEY = re.compile(r"0[23][0-9a-f]{64}")
"""A compressed secp256k1 public key in hex."""
CONSOLE_LINE = re.compile(re.escape(CONSOLE) + r"(0[23][0-9a-f]{64})\b")

SCRIPT = f"""\
#!/bin/sh
# tundravm instance key: a fresh {CURVE} key every boot, on tmpfs only. The private key is
# root:root 0600; services read it through LoadCredential=, nothing else.
set -eu
pub_of() {{
    openssl ec -in "$1" -pubout -conv_form compressed -outform DER 2>/dev/null |
        tail -c 33 | od -An -v -tx1 | tr -d ' \\n'
}}
mkdir -p -m 0711 /run/tundravm
if [ -s {KEY} ] && [ -s {PUBLIC} ]; then
    echo "{CONSOLE}$(cat {PUBLIC})"
    exit 0
fi
umask 077
openssl ecparam -name {CURVE} -genkey -noout -out {KEY}.tmp
pub=$(pub_of {KEY}.tmp)
if [ "${{#pub}}" -ne 66 ] || [ "${{pub#0[23]}}" = "$pub" ]; then
    echo "tundravm-instance-key: failed: openssl made no {CURVE} key"
    exit 1
fi
printf '%s\\n' "$pub" > {PUBLIC}.tmp
chmod 0644 {PUBLIC}.tmp
mv {KEY}.tmp {KEY}
mv {PUBLIC}.tmp {PUBLIC}
echo "{CONSOLE}$pub"
"""
"""``/usr/lib/tundravm/instance-key``: ``/bin/sh``, coreutils and ``openssl``."""

_HARDENING: tuple[tuple[str, str], ...] = (
    ("ProtectSystem", "full"),
    ("ProtectHome", "yes"),
    ("PrivateTmp", "yes"),
    ("PrivateDevices", "yes"),
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
"""Root without capabilities, no network, no devices beyond the pseudo ones."""


def service() -> ServiceSpec:
    """The generating unit: before the gate, so before every unit a ``Service`` lowers to."""
    return ServiceSpec(
        name=UNIT,
        command=("/bin/sh", SCRIPT_PATH),
        description=f"Generate this boot's {CURVE} instance key",
        before=(execution.GATE_UNIT,),
        type="oneshot",
        remain_after_exit=True,
        restart="no",
        restart_sec="",
        timeout_start="60s",
        hardening=_HARDENING,
        needs=(),
        extra_unit={"Service": {"StandardOutput": "journal+console"}},
    )


def public_key(console: list[str]) -> str | None:
    """The last instance public key *console* (its lines) printed, ``None`` when none."""
    found = None
    for line in console:
        match = CONSOLE_LINE.search(line)
        if match:
            found = match[1]
    return found


__all__ = [
    "CONSOLE",
    "CREDENTIAL",
    "CURVE",
    "KEY",
    "PACKAGE",
    "PUBLIC",
    "PUBLIC_KEY",
    "SCRIPT",
    "SCRIPT_PATH",
    "UNIT",
    "public_key",
    "service",
]
