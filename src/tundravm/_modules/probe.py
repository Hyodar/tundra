"""The boot probe (plan 5.2 item 7): the checklist a profile's declarations imply, compiled in.

Every development profile (``release=False``: ``dev=DevAccess(...)``,
``Profile(production=False)``) carries ``tundravm-probe.service``, a root oneshot ordered
after the execution gate and every unit it checks, running :data:`SCRIPT_PATH`, a POSIX
``sh`` script generated from the profile's resolved declarations. A production profile
carries none: it starts nothing beyond what it declares. The script prints one line per
check on the serial console, ``tundravm-probe: <check> ok|fail|skip <detail>``, and ends with
``tundravm-probe: done <n> ok <m> fail``. A ``fail`` detail says what to do; a ``skip``
says why the check cannot run here.

The checks, each only when its declaration exists (:func:`derive`):

- ``mode``: ``/run/tundravm/mode`` says ``simulated`` or ``tdx`` (the gate, always);
- ``rtmr``: always ``skip``, with the reason: the guest reads its registers only from a
  quote, which ``tdxs`` (tools/) makes; ``tundravm attest`` compares them with
  ``measurements.json``;
- ``service:UNIT``: each ``Service`` is active (or exited 0), ``socket:UNIT`` its socket
  when socket-activated, ``timer:UNIT`` the timer of a scheduled one;
- ``port:N/PROTO``: each port a service exposes has a listener in ``/proc/net`` (what
  ``ss -lnt``/``-lnu`` reads; ``iproute2`` is not in a minimal image);
- ``volume:PATH``: each ``Disk`` (a ``Volume``) is mounted there, ext4, and on a dm-crypt
  device when it has a key;
- ``egress:ADDRESS:PORT``: with ``Egress``, a UDP datagram to an address and port no rule
  accepts is refused by the output chain (``EPERM``, at once; TCP would only time out);
- ``observability:UNIT``: with an ``Observability``, ``journal-export.service`` active and
  connected to its sink's port (the proxy's with one: the upload streams while it follows
  the journal) or with an upload's cursor saved, or restarting after an upload its journal
  names failing (the sink away), in place of its ``service:`` check;
  ``metrics-export.service`` active with its configuration accepted by ``promtool check
  config --agent``, or without promtool by the agent's own ``Completed loading of
  configuration file``; ``observability:no-wait``: no other service requires or is ordered
  after either; ``observability:egress``: the output-chain rules the sinks (or the proxy)
  asked for are loaded (``nft list chain``);
- ``hardening:UNIT``: ``systemd-analyze security UNIT`` under the preset's ceiling
  (:data:`~tundravm.declarative.systemd.CEILINGS`), for a service whose sandbox is exactly
  a preset;
- ``kernel:symbols``: a built kernel's options turned off leave none of the symbols
  :data:`SYMBOLS` names in ``/proc/kallsyms``; ``kernel:modules``: no module is loaded
  beyond the curated set (:mod:`tundravm.kernel_modules`) and what those hold
  (``/sys/module/*/holders``);
- ``host-keys``: the SSH host key directory is ``root:root`` and writable by root only, the
  key ``root:root 0600`` (plan 5.2 item 2: root-owned, read-only to everything else; the
  root is a tmpfs or erofs, which carry no ``chattr +i``);
- ``key:instance``: the instance key (:mod:`tundravm._modules.instance_key`, always) is
  ``root:root 0600`` and its public key file holds its public key.

The probe runs inside the guest it checks: it proves what the guest says of itself, which a
compromised guest could fake. It is a development aid, not evidence.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from tundravm.models import ServiceSpec, unit_name

from . import execution, instance_key

if TYPE_CHECKING:
    from tundravm.declarative.model import Declaration, EgressRule, Firewall

UNIT = "tundravm-probe.service"
SCRIPT_PATH = "/usr/lib/tundravm/probe"
PREFIX = "tundravm-probe: "
DONE = "done"
WAIT_SECONDS = 30
"""How long a service may stay ``activating`` and a declared port stay unbound."""
DROPIN = f"/usr/lib/systemd/system/{UNIT}.d/50-tundravm-production.conf"
"""What a production profile extending a development default sets: the probe never runs."""
DENIED_ADDRESSES = ("192.0.2.1", "198.51.100.1", "203.0.113.1")
"""Documentation addresses (RFC 5737) the egress check sends to, the first no rule accepts."""
DENIED_PORTS = (9, 7, 13, 19, 37)
"""Ports the egress check sends to (discard first), the first no rule accepts."""

SYMBOLS: dict[str, str] = {
    "CONFIG_MAGIC_SYSRQ": "handle_sysrq",
    "CONFIG_USERFAULTFD": "handle_userfault",
    "CONFIG_IO_URING": "__io_uring_cancel",
    "CONFIG_HIBERNATION": "hibernate",
    "CONFIG_FUSE_FS": "fuse_conn_init",
    "CONFIG_VHOST": "vhost_dev_init",
    "CONFIG_BONDING": "bond_create",
    "CONFIG_XDP_SOCKETS": "xsk_tx_peek_desc",
    "CONFIG_L2TP": "l2tp_tunnel_create",
    "CONFIG_PROC_KCORE": "kclist_add",
}
"""An option a kernel hardening turns off, and a global function only that option compiles
(a static one may be inlined, a syscall stub stays as ``sys_ni_syscall``'s alias)."""

Verdict = Literal["ok", "fail", "skip"]
Helper = Literal[
    "service",
    "active",
    "port",
    "volume",
    "egress",
    "upload",
    "agent",
    "nowait",
    "rules",
    "hardening",
    "symbols",
    "modules",
    "keys",
    "instance",
    "endpoint",
]


@dataclass(frozen=True, slots=True)
class Check:
    """One check: its name on the console, what it proves, the script line that runs it,
    the helper that line calls and the units it waits for."""

    name: str
    what: str
    call: str
    helper: Helper | None = None
    after: tuple[str, ...] = ()

    def manifest(self) -> dict[str, str]:
        return {"check": self.name, "what": self.what}


@dataclass(frozen=True, slots=True)
class Probe:
    """A profile's checks, and the systemd binaries they need kept."""

    checks: tuple[Check, ...]

    @property
    def tools(self) -> tuple[str, ...]:
        """The ``systemd`` package's binaries the debloat must keep for this probe."""
        if any(check.helper == "hardening" for check in self.checks):
            return ("systemd-analyze",)
        return ()

    def script(self) -> str:
        """:data:`SCRIPT_PATH`: ``/bin/sh``, coreutils, grep, sed and the image's own tools."""
        used = {check.helper for check in self.checks}
        parts = [_HEADER]
        parts += [_HELPERS[name] for name in _HELPERS if name in used]
        parts += [_MODE, _RTMR]
        parts += [f"check={check.name}\n{check.call}\n" for check in self.checks[2:]]
        parts.append('say "done $passed ok $failed fail"\n')
        return "".join(parts)

    def service(self) -> ServiceSpec:
        """``tundravm-probe.service``: after the gate and every unit a check reads."""
        after = [execution.GATE_UNIT]
        for check in self.checks:
            after += [unit for unit in check.after if unit not in after]
        return ServiceSpec(
            name=UNIT,
            command=("/bin/sh", SCRIPT_PATH),
            description="Check what this profile's declarations promise",
            after=tuple(after),
            type="oneshot",
            remain_after_exit=True,
            restart="no",
            restart_sec="",
            timeout_start="10min",
            hardening=(),
            needs=(),
            extra_unit={"Service": {"StandardOutput": "journal+console"}},
        )

    def manifest(self) -> list[dict[str, str]]:
        return [check.manifest() for check in self.checks]


def derive(
    items: Sequence[Declaration],
    *,
    modules: Sequence[str] | None,
    kernel_off: Iterable[str] = (),
) -> Probe:
    """The probe *items* (a variant's resolved declarations) imply.

    *modules* is the curated module set the profile keeps (``None``: no module check, the
    image keeps every module); *kernel_off* the options a built kernel turns off (empty for
    a distribution kernel)."""
    from tundravm._modules.secret_delivery import ENDPOINT_UNIT
    from tundravm.declarative.model import Disk, Firewall, Port, Secrets, Service, Task
    from tundravm.declarative.systemd import ceiling
    from tundravm.declarative.utils import SSH_HOST_KEY

    checks = [
        Check("mode", f"{execution.MODE_FILE} says simulated or tdx", ""),
        Check("rtmr", "RTMR0..2 against measurements.json (skipped: needs a quote)", ""),
    ]
    firewall = next((i for i in items if isinstance(i, Firewall) and i.egress == "filter"), None)
    exporters, observed = _observability(items, firewall)
    hardened: list[Check] = []
    for item in items:
        if isinstance(item, Service) and item.enabled:
            if unit_name(item.name) not in exporters:
                checks += _service(item)
            sandbox = item.sandbox
            found = None if sandbox is None or item.name.endswith("@") else ceiling(sandbox)
            if found is not None:
                preset, limit = found
                unit = unit_name(item.name)
                hardened.append(
                    Check(
                        f"hardening:{unit}",
                        f"systemd-analyze security {unit} at or under {limit:.1f} ({preset})",
                        f"probe_hardening {unit} {limit:.1f} {preset}",
                        "hardening",
                        (unit,),
                    )
                )
        elif isinstance(item, Task) and item.schedule is not None:
            timer = item.unit.removesuffix(".service") + ".timer"
            checks.append(
                Check(
                    f"timer:{timer}",
                    f"{timer} is active",
                    f"probe_active {timer} {_quote(_TIMER_FIX)}",
                    "active",
                    (timer,),
                )
            )
    for item in items:
        if isinstance(item, Service) and item.enabled:
            for port in item.ports:
                assert isinstance(port, Port)
                checks.append(
                    Check(
                        f"port:{port}",
                        f"something listens on {port}",
                        f"probe_port {port.protocol} {port.number}",
                        "port",
                        (unit_name(item.name),),
                    )
                )
    for item in items:
        if isinstance(item, Disk):
            encrypted = item.key is not None
            checks.append(
                Check(
                    f"volume:{item.mount}",
                    f"{item.mount} is mounted, ext4" + (", on dm-crypt" if encrypted else ""),
                    f"probe_volume {item.mount} {'yes' if encrypted else 'no'}",
                    "volume",
                    ("runtime-init.service",),
                )
            )
    if firewall is not None:
        denied = _denied(firewall.egress_rules)
        if denied is not None:
            address, number = denied
            checks.append(
                Check(
                    f"egress:{address}:{number}",
                    f"a datagram to {address}:{number}/udp is refused (no Egress rule accepts it)",
                    f"probe_egress {address} {number}",
                    "egress",
                    ("nftables.service",),
                )
            )
    endpoints = [item for item in items if isinstance(item, Secrets)]
    for item in endpoints:
        unit = ENDPOINT_UNIT if len(endpoints) == 1 else f"tundravm-endpoint-{item.name}.service"
        checks.append(
            Check(
                f"endpoint:{item.port}",
                f"the attested endpoint on {item.port} serves evidence with the nonce it is given",
                f"probe_endpoint {item.port} {unit}",
                "endpoint",
                (unit,),
            )
        )
    checks += observed
    checks += hardened
    off = sorted(option for option in set(kernel_off) if option in SYMBOLS)
    if off:
        symbols = " ".join(SYMBOLS[option] for option in off)
        checks.append(
            Check(
                "kernel:symbols",
                f"/proc/kallsyms holds none of {symbols}",
                f"probe_symbols {symbols}",
                "symbols",
            )
        )
    if modules is not None:
        names = sorted({name.replace("-", "_") for name in modules})
        checks.append(
            Check(
                "kernel:modules",
                f"no module loaded beyond the {len(names)} curated and what they hold",
                " ".join(["probe_modules", *names]),
                "modules",
                ("systemd-udev-trigger.service", "systemd-modules-load.service"),
            )
        )
    if any(isinstance(i, Task) and i.unit == "ssh-host-keys.service" for i in items):
        checks.append(
            Check(
                "host-keys",
                f"{SSH_HOST_KEY.rsplit('/', 1)[0]} root:root, writable by root only; "
                f"{SSH_HOST_KEY} root:root 0600",
                f"probe_keys {SSH_HOST_KEY}",
                "keys",
                ("ssh-host-keys.service",),
            )
        )
    checks.append(
        Check(
            "key:instance",
            f"{instance_key.KEY} root:root 0600, {instance_key.PUBLIC} its public key",
            f"probe_instance_key {instance_key.KEY} {instance_key.PUBLIC}",
            "instance",
            (instance_key.UNIT,),
        )
    )
    return Probe(tuple(checks))


def _service(item: object) -> list[Check]:
    from tundravm.declarative.model import Service

    assert isinstance(item, Service)
    unit = unit_name(item.name)
    stem = unit.removesuffix(".service").removesuffix("@")
    if item.socket is not None:
        socket = f"{stem}.socket"
        return [
            Check(
                f"socket:{socket}",
                f"{socket} is active",
                f"probe_active {socket} {_quote(_SOCKET_FIX)}",
                "active",
                (socket,),
            )
        ]
    if item.name.endswith("@"):
        return []
    return [
        Check(
            f"service:{unit}",
            f"{unit} is active, or exited 0",
            f"probe_service {unit}",
            "service",
            (unit, f"{stem}-ready.service"),
        )
    ]


def _observability(
    items: Sequence[Declaration], firewall: Firewall | None
) -> tuple[frozenset[str], list[Check]]:
    """The exporters an ``Observability`` renders among *items*, and their checks: each
    running (or retrying a sink), its configuration taken by the program itself, no service
    waiting on it and, with a filtering *firewall*, the rules its sinks asked for loaded."""
    from tundravm.components._observability import LOGS_SERVICE, METRICS_SERVICE
    from tundravm.declarative._network import egress_match
    from tundravm.declarative.model import Service

    logs_unit, metrics_unit = unit_name(LOGS_SERVICE), unit_name(METRICS_SERVICE)
    services = {
        unit_name(item.name): item
        for item in items
        if isinstance(item, Service) and item.enabled and not item.name.endswith("@")
    }
    found = {unit: s for unit, s in services.items() if unit in (logs_unit, metrics_unit)}
    if not found:
        return frozenset(), []
    checks: list[Check] = []
    units = list(found)
    logs = found.get(logs_unit)
    if logs is not None:
        unit = logs_unit
        url = _flag(logs.exec_start, "--url")
        state = _flag(logs.exec_start, "--save-state")
        proxy = logs.env.get("https_proxy" if url.startswith("https:") else "http_proxy")
        port = _port(proxy or url)
        checks.append(
            Check(
                f"observability:{unit}",
                f"{unit} is active or retrying; systemd-journal-upload streams to {url}, "
                "or names it failing",
                f"probe_upload {unit} {_quote(url)} {state} {port}",
                "upload",
                (unit,),
            )
        )
    metrics = found.get(metrics_unit)
    if metrics is not None:
        unit = metrics_unit
        config = _flag(metrics.exec_start, "--config.file")
        checks.append(
            Check(
                f"observability:{unit}",
                f"{unit} is active; Prometheus takes {config} (promtool, else its log)",
                f"probe_agent {unit} {config}",
                "agent",
                (unit,),
            )
        )
    others = [unit for unit in services if unit not in found]
    checks.append(
        Check(
            "observability:no-wait",
            f"no service waits on {', '.join(units)}",
            " ".join(["probe_nowait", *units, "--", *others]),
            "nowait",
            tuple(units),
        )
    )
    rules = [
        rule
        for rule in (() if firewall is None else firewall.egress_rules)
        if any(who == "observability" for who, _ in rule.origins)
    ]
    if rules:
        checks.append(
            Check(
                "observability:egress",
                f"the output chain accepts {', '.join(map(str, rules))} (the sinks' egress)",
                " ".join(["probe_rules", *(_quote(egress_match(rule)) for rule in rules)]),
                "rules",
                ("nftables.service",),
            )
        )
    return frozenset(found), checks


def _port(url: str) -> int:
    """The TCP port *url* connects to."""
    parts = urlsplit(url)
    return parts.port or (443 if parts.scheme == "https" else 80)


def _flag(argv: str | Sequence[str], name: str) -> str:
    """The value of ``NAME=VALUE`` in *argv*."""
    words = argv.split() if isinstance(argv, str) else argv
    return next(word.partition("=")[2] for word in words if word.startswith(f"{name}="))


def _denied(rules: Sequence[EgressRule]) -> tuple[str, int] | None:
    """An IPv4 address and UDP port no egress rule accepts, else ``None``."""
    hosts = [rule.host for rule in rules if rule.host is not None]
    ports = [rule.port for rule in rules if rule.host is None]
    networks = [ipaddress.ip_network(host, strict=False) for host in hosts]
    address = next(
        (
            a
            for a in DENIED_ADDRESSES
            if not any(ipaddress.ip_address(a) in network for network in networks)
        ),
        None,
    )
    allowed = {getattr(port, "number", None) for port in ports if str(port).endswith("/udp")}
    number = next((p for p in DENIED_PORTS if p not in allowed), None)
    if address is None or number is None:
        return None
    return address, number


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def off_options(base: Path | None, options: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """The options a built kernel turns off: *options* set to ``n`` and the base config
    file's ``# CONFIG_X is not set`` lines, unless *options* turns them back on."""
    found: dict[str, str] = {}
    if base is not None and base.is_file():
        for line in base.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _NOT_SET.fullmatch(line.strip())
            if match:
                found[match[1]] = "n"
            elif line.startswith("CONFIG_") and "=" in line:
                key, _, value = line.partition("=")
                found[key] = value.strip()
    found.update(dict(options))
    return tuple(sorted(key for key, value in found.items() if value == "n"))


_NOT_SET = re.compile(r"# (CONFIG_[A-Za-z0-9_]+) is not set")
_TIMER_FIX = "the timer must be enabled; systemctl status names why it is not"
_SOCKET_FIX = "the socket must listen; systemctl status names why it does not"

_HEADER = f"""\
#!/bin/sh
# tundravm boot probe (plan 5.2 item 7): what this profile's declarations promise, checked
# from inside the guest. One "{PREFIX}<check> ok|fail|skip <detail>" line per check.
set -u
passed=0
failed=0
check=
say() {{ echo "{PREFIX.strip()} $*"; }}
ok() {{ passed=$((passed + 1)); say "$check ok $*"; }}
fail() {{ failed=$((failed + 1)); say "$check fail $*"; }}
skip() {{ say "$check skip $*"; }}
"""

_HELPERS: dict[str, str] = {
    "service": f"""\
probe_service() {{
    state=$(systemctl show -P ActiveState "$1")
    waited=0
    while [ "$state" = activating ] && [ "$waited" -lt {WAIT_SECONDS} ]; do
        sleep 1
        waited=$((waited + 1))
        state=$(systemctl show -P ActiveState "$1")
    done
    result=$(systemctl show -P Result "$1")
    if [ "$state" = active ]; then
        ok active
    elif [ "$state" = inactive ] && [ "$result" = success ] &&
        [ "$(systemctl show -P ExecMainExitTimestampMonotonic "$1")" != 0 ]; then
        ok "exited 0"
    elif [ "$state" = inactive ] && [ "$result" = success ]; then
        fail "never started: a unit it requires failed; systemctl list-dependencies $1"
    else
        fail "$state ($result): journalctl -u $1 says why"
    fi
}}
""",  # noqa: E501
    "endpoint": f"""\
probe_endpoint() {{
    if ! command -v curl >/dev/null 2>&1; then
        skip "no curl in the image to ask the endpoint"
        return
    fi
    nonce=$(tr -d '-' </proc/sys/kernel/random/uuid)
    waited=0
    while :; do
        said=$(curl -ksS --max-time 10 "https://127.0.0.1:$1/evidence?nonce=$nonce" 2>&1) || true
        case $said in
            *"\"nonce\":\"$nonce\""*) ok "listening, evidence answers nonce $nonce"; return ;;
        esac
        [ "$waited" -ge {WAIT_SECONDS} ] && break
        sleep 1
        waited=$((waited + 1))
    done
    fail "no evidence for nonce $nonce on :$1: journalctl -u $2 -u tdxs; ${{said:-no answer}}"
}}
""",  # noqa: E501
    "active": """\
probe_active() {
    state=$(systemctl show -P ActiveState "$1")
    if [ "$state" = active ]; then ok active; else fail "$state: $2"; fi
}
""",  # noqa: E501
    "port": f"""\
probe_port() {{
    hex=$(printf '%04X' "$2")
    want=0A
    [ "$1" = udp ] && want=07
    waited=0
    while :; do
        for table in /proc/net/"$1" /proc/net/"$1"6; do
            [ -r "$table" ] || continue
            while read -r _ local _ state _; do
                if [ "$state" = "$want" ] && [ "${{local##*:}}" = "$hex" ]; then
                    ok listening
                    return
                fi
            done <"$table"
        done
        [ "$waited" -ge {WAIT_SECONDS} ] && break
        sleep 1
        waited=$((waited + 1))
    done
    fail "nothing listens on $2/$1 after {WAIT_SECONDS}s: the service must bind every port expose= names, or drop it from expose="
}}
""",  # noqa: E501
    "volume": """\
probe_volume() {
    if ! command -v findmnt >/dev/null 2>&1; then
        skip "no findmnt in the image (util-linux)"
        return
    fi
    mount=$1
    encrypted=$2
    set -- $(findmnt -n -o FSTYPE,SOURCE --mountpoint "$mount" 2>/dev/null)
    if [ $# -lt 2 ]; then
        fail "nothing is mounted at $mount: unlock it (tundravm secrets unlock INSTANCE VOLUME) or read journalctl -u runtime-init.service"
    elif [ "$1" != ext4 ]; then
        fail "$mount is $1 on $2, not ext4: disk-setup formats ext4, so this is not the volume"
    elif [ "$encrypted" = no ]; then
        ok "ext4 on $2"
    else
        device=$(readlink -f "$2")
        uuid=$(cat "/sys/class/block/${device##*/}/dm/uuid" 2>/dev/null || true)
        case $uuid in
            CRYPT-*) ok "ext4 on $2, dm-crypt" ;;
            *) fail "$mount is on $2, not a dm-crypt device: an encrypted volume mounts from its LUKS mapping; journalctl -u runtime-init.service" ;;
        esac
    fi
}
""",  # noqa: E501
    "egress": """\
probe_egress() {
    if ! command -v bash >/dev/null 2>&1; then
        skip "no bash to send a datagram with"
        return
    fi
    if said=$(bash -c 'printf x >"/dev/udp/$0/$1"' "$1" "$2" 2>&1); then
        fail "a datagram to $1:$2/udp left the guest: the output chain does not drop it; systemctl status nftables.service, and check Network(egress=Egress(...))"
        return
    fi
    case $said in
        *"not permitted"*) ok "refused by the output chain (Operation not permitted)" ;;
        *) skip "a datagram to $1:$2/udp failed before the firewall ($(echo "$said" | tail -n 1)): no route, so the deny is untested" ;;
    esac
}
""",  # noqa: E501
    "upload": f"""\
probe_upload() {{
    hex=$(printf '%04X' "$4")
    waited=0
    while :; do
        state=$(systemctl show -P ActiveState "$1")
        if [ "$state" = active ] && grep -q '^LAST_CURSOR=' "$3" 2>/dev/null; then
            ok "active, uploaded to $2 (its cursor is in $3)"
            return
        fi
        sockets=" "
        for fd in /proc/"$(systemctl show -P MainPID "$1")"/fd/*; do
            link=$(readlink "$fd" 2>/dev/null) || continue
            case $link in socket:\\[*\\]) link=${{link#socket:[}} sockets="$sockets${{link%]}} " ;; esac
        done
        for table in /proc/net/tcp /proc/net/tcp6; do
            [ "$state" = active ] && [ -r "$table" ] || continue
            while read -r _ _ remote st _ _ _ _ _ inode _; do
                case $sockets in *" $inode "*) ;; *) continue ;; esac
                if [ "$st" = 01 ] && [ "${{remote##*:}}" = "$hex" ]; then
                    ok "active, streaming to $2 (its connection to port $4 is open)"
                    return
                fi
            done <"$table"
        done
        [ "$waited" -ge {WAIT_SECONDS} ] && break
        sleep 1
        waited=$((waited + 1))
    done
    sub=$(systemctl show -P SubState "$1")
    said=$(journalctl -b -q -o cat -u "$1" 2>/dev/null | grep -F "Upload to $2" | tail -n 1)
    case $state/$sub in
        active/*) fail "active, but not connected to port $4 after {WAIT_SECONDS}s and nothing uploaded: systemd-journal-upload reads no journal${{said:+ ($said)}}; journalctl -u $1 says why" ;;
        */auto-restart*)
            if [ -n "$said" ]; then
                ok "retrying every 5s: $said"
            else
                fail "restarting without trying $2: systemd-journal-upload refuses its arguments; journalctl -u $1 says why"
            fi
            ;;
        *) fail "$state ($sub): an exporter retries an unreachable sink, it does not stop; journalctl -u $1 says why" ;;
    esac
}}
""",  # noqa: E501
    "agent": f"""\
probe_agent() {{
    if command -v promtool >/dev/null 2>&1; then
        if ! said=$(promtool check config --agent --syntax-only "$2" 2>&1); then
            fail "promtool check config --agent $2: $(echo "$said" | tail -n 1): this Prometheus refuses what PrometheusRemoteWrite renders"
            return
        fi
        parsed="promtool check config --agent accepts $2"
    else
        waited=0
        while :; do
            said=$(journalctl -b -q -o cat -u "$1" 2>/dev/null |
                grep -E 'Completed loading of configuration file|Error loading config' | tail -n 1)
            [ -n "$said" ] || [ "$waited" -ge {WAIT_SECONDS} ] && break
            sleep 1
            waited=$((waited + 1))
        done
        case $said in
            *"Error loading config"*)
                fail "Prometheus refuses $2: $said"
                return
                ;;
            "")
                fail "Prometheus logged no 'Completed loading of configuration file' in {WAIT_SECONDS}s: journalctl -u $1 says why"
                return
                ;;
        esac
        parsed="Prometheus loaded $2"
    fi
    state=$(systemctl show -P ActiveState "$1")
    retry=$(journalctl -b -q -o cat -u "$1" 2>/dev/null | grep -F 'retrying' | tail -n 1)
    if [ "$state" = active ]; then
        ok "active, $parsed${{retry:+; retrying the sink: $retry}}"
    else
        fail "$state ($(systemctl show -P SubState "$1")): $parsed, but the agent does not run; journalctl -u $1 says why"
    fi
}}
""",  # noqa: E501
    "nowait": """\
probe_nowait() {
    exporters=
    while [ "$1" != -- ]; do
        exporters="$exporters $1"
        shift
    done
    shift
    waiting=
    for exporter in $exporters; do
        after=" $(systemctl show --value -p RequiredBy,RequisiteOf,BoundBy,Before "$exporter" | tr '\\n' ' ') "
        for unit in "$@"; do
            case $after in *" $unit "*) waiting="$waiting $unit ($exporter)" ;; esac
        done
    done
    if [ -z "$waiting" ]; then
        ok "$# services start without waiting on them"
    else
        fail "waiting:$waiting: nothing may wait on an exporter, whose sink can be away; drop it from that service's requires="
    fi
}
""",  # noqa: E501
    "rules": """\
probe_rules() {
    if ! command -v nft >/dev/null 2>&1; then
        skip "no nft in the image"
        return
    fi
    if ! chain=$(nft list chain inet filter output 2>&1); then
        fail "nft list chain inet filter output: $(echo "$chain" | tail -n 1); systemctl status nftables.service"
        return
    fi
    missing=
    for rule in "$@"; do
        case $chain in *"$rule accept"*) ;; *) missing="$missing, $rule" ;; esac
    done
    if [ -z "$missing" ]; then
        ok "$# rules loaded"
    else
        fail "the output chain lacks ${missing#, }: nftables.service did not load /etc/nftables.conf; systemctl status nftables.service"
    fi
}
""",  # noqa: E501
    "hardening": """\
probe_hardening() {
    if ! command -v systemd-analyze >/dev/null 2>&1; then
        skip "no systemd-analyze in the image"
        return
    fi
    exposure=$(systemd-analyze security --no-pager "$1" 2>/dev/null |
        sed -n 's/.*Overall exposure level for [^:]*: \\([0-9][0-9]*\\.[0-9]\\).*/\\1/p')
    if [ -z "$exposure" ]; then
        fail "systemd-analyze security $1 printed no exposure level: run it in the guest for its error"
    elif [ "$(echo "$exposure" | tr -d .)" -le "$(echo "$2" | tr -d .)" ]; then
        ok "exposure $exposure, ceiling $2 ($3)"
    else
        fail "exposure $exposure over the $3 ceiling $2: a directive the preset sets does not hold; systemd-analyze security $1 lists each"
    fi
}
""",  # noqa: E501
    "symbols": """\
probe_symbols() {
    if [ ! -r /proc/kallsyms ]; then
        skip "no /proc/kallsyms (CONFIG_KALLSYMS=n): no symbols to read"
        return
    fi
    present=
    for symbol in "$@"; do
        if grep -Eq " $symbol([[:space:]]|\\$)" /proc/kallsyms; then present="$present $symbol"; fi
    done
    if [ -z "$present" ]; then
        ok "$# symbols absent"
    else
        fail "present:$present: the running kernel is not the one built with those options off; rebuild, and check Kernel.build(config=...)"
    fi
}
""",  # noqa: E501
    "modules": """\
probe_modules() {
    if [ ! -e /proc/modules ]; then
        ok "no loadable modules"
        return
    fi
    allowed=" $* "
    grew=yes
    while [ "$grew" = yes ]; do
        grew=no
        while read -r name _; do
            case $allowed in *" $name "*) continue ;; esac
            for holder in /sys/module/"$name"/holders/*; do
                case $allowed in *" ${holder##*/} "*) allowed="$allowed$name " grew=yes; break ;; esac
            done
        done </proc/modules
    done
    loaded=0
    outside=
    while read -r name _; do
        loaded=$((loaded + 1))
        case $allowed in *" $name "*) ;; *) outside="$outside $name" ;; esac
    done </proc/modules
    count=0
    shown=
    for name in $outside; do
        count=$((count + 1))
        [ "$count" -le 8 ] && shown="$shown $name"
    done
    [ "$count" -gt 8 ] && shown="$shown (and $((count - 8)) more)"
    if [ "$count" -eq 0 ]; then
        ok "$loaded loaded, each curated or held by one"
    else
        fail "$count loaded beyond the curated set:$shown: add each the image needs to Debian(kernel_modules=[...]), or find what loads it (udev, modprobe.d)"
    fi
}
""",  # noqa: E501
    "keys": """\
probe_keys() {
    directory=${1%/*}
    if [ ! -e "$1" ]; then
        fail "no $1: ssh-host-keys.service did not make it; journalctl -u ssh-host-keys.service"
        return
    fi
    set -- "$1" $(stat -c '%U:%G %a' "$directory") $(stat -c '%U:%G %a' "$1")
    if [ "$2" != root:root ] || [ $((0$3 & 022)) -ne 0 ]; then
        fail "$directory is $2 $3: it must be root:root and writable by root only (chown root:root, chmod go-w)"
    elif [ "$4" != root:root ] || [ "$5" != 600 ]; then
        fail "$1 is $4 $5: a host key must be root:root 0600 (chown root:root, chmod 600)"
    else
        ok "$directory root:root $3, ${1##*/} root:root 600"
    fi
}
""",  # noqa: E501
    "instance": f"""\
probe_instance_key() {{
    if [ ! -s "$1" ] || [ ! -s "$2" ]; then
        fail "no $1 or $2: {instance_key.UNIT} did not make them; journalctl -u {instance_key.UNIT}"
        return
    fi
    set -- "$1" "$2" $(stat -c '%U:%G %a' "$1")
    pub=$(cat "$2")
    made=$(openssl ec -in "$1" -pubout -conv_form compressed -outform DER 2>/dev/null |
        tail -c 33 | od -An -v -tx1 | tr -d ' \\n')
    if [ "$3" != root:root ] || [ "$4" != 600 ]; then
        fail "$1 is $3 $4: the instance key must be root:root 0600; only {instance_key.UNIT} writes it"
    elif [ "$made" != "$pub" ]; then
        fail "$2 is not the public key of $1: something besides {instance_key.UNIT} wrote one; journalctl -u {instance_key.UNIT}"
    else
        ok "root:root 600, pub $pub"
    fi
}}
""",  # noqa: E501
}

_MODE = f"""\
check=mode
mode=$(cat {execution.MODE_FILE} 2>/dev/null || true)
case $mode in
    {execution.SIMULATED}|{execution.TDX}) ok "$mode" ;;
    *) fail "no mode in {execution.MODE_FILE}: {execution.GATE_UNIT} did not record one; journalctl -u {execution.GATE_UNIT}" ;;
esac
"""  # noqa: E501

_RTMR = f"""\
check=rtmr
if [ "$mode" = {execution.TDX} ]; then
    skip "the guest reads RTMR0..2 only from a quote, which tdxs makes; tundravm attest INSTANCE --policy FILE compares them with measurements.json"
else
    skip "no TDX: a simulated guest has no registers to compare with measurements.json"
fi
"""  # noqa: E501


# ── reading the console ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Result:
    """One check's line: its name, verdict and detail."""

    check: str
    verdict: Verdict
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "verdict": self.verdict, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Report:
    """What the probe printed: each check's last line, and its ``done`` counts (``None``
    until it printed them)."""

    results: tuple[Result, ...] = ()
    done: tuple[int, int] | None = None
    expected: tuple[str, ...] = field(default=(), compare=False)
    """The checks the build's manifest says the probe carries (empty: unknown)."""

    @property
    def failed(self) -> tuple[Result, ...]:
        return tuple(r for r in self.results if r.verdict == "fail")

    @property
    def missing(self) -> tuple[str, ...]:
        """Expected checks that printed no line."""
        seen = {r.check for r in self.results}
        return tuple(name for name in self.expected if name not in seen)

    @property
    def ok(self) -> bool:
        """It finished, nothing failed and every expected check printed."""
        return self.done is not None and not self.failed and not self.missing

    def table(self) -> str:
        """The results as a table: check, verdict, detail; then the counts."""
        rows = [
            ("check", "verdict", "detail"),
            *((r.check, r.verdict, r.detail) for r in self.results),
        ]
        rows += [(name, "missing", "printed no line") for name in self.missing]
        width = max(len(row[0]) for row in rows)
        lines = [f"{c:<{width}}  {v:<7}  {d}".rstrip() for c, v, d in rows]
        counts = {v: sum(r.verdict == v for r in self.results) for v in ("ok", "fail", "skip")}
        summary = ", ".join(f"{n} {v}" for v, n in counts.items())
        lines.append(summary if self.done is not None else f"{summary}; the probe did not finish")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "done": None if self.done is None else {"ok": self.done[0], "fail": self.done[1]},
            "results": [r.to_dict() for r in self.results],
            "missing": list(self.missing),
        }


_RESULT = re.compile(r"(\S+) (ok|fail|skip)(?: (.*))?")
_DONE = re.compile(rf"{DONE} (\d+) ok (\d+) fail")


def read(lines: Iterable[str], expected: Sequence[str] = ()) -> Report:
    """The :class:`Report` in console *lines* (``console_lines``): the last line of each
    check (the journal may forward it a second time), in the order they first printed, and
    the last ``done``."""
    found: dict[str, Result] = {}
    done: tuple[int, int] | None = None
    for line in lines:
        _, sep, said = line.partition(PREFIX)
        if not sep:
            continue
        said = said.strip()
        counted = _DONE.fullmatch(said)
        if counted:
            done = (int(counted[1]), int(counted[2]))
            continue
        match = _RESULT.fullmatch(said)
        if match:
            name = match[1]
            found[name] = Result(name, match[2], match[3] or "")  # type: ignore[arg-type]
    return Report(tuple(found.values()), done, tuple(expected))


def checks_of(manifest: Mapping[str, object]) -> tuple[str, ...]:
    """The check names a build's manifest records for its probe."""
    rows = manifest.get("probe") or []
    assert isinstance(rows, list)
    return tuple(str(row["check"]) for row in rows)


__all__ = [
    "DONE",
    "DROPIN",
    "PREFIX",
    "SCRIPT_PATH",
    "SYMBOLS",
    "UNIT",
    "Check",
    "Probe",
    "Report",
    "Result",
    "checks_of",
    "derive",
    "off_options",
    "read",
]
