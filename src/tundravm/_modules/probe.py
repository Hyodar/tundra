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
- ``cmdline``: ``/proc/cmdline`` holds every parameter the image appends to the UKI's
  command line (``TeeBase``'s CPU mitigations first), so no boot loader replaced it;
- ``rtmr``: always ``skip``, with the reason: the guest reads its registers only from a
  quote, which ``tdxs`` (tools/) makes; ``tundravm attest`` compares them with
  ``measurements.json``;
- ``rootfs:LAYOUT``: ``/`` is what ``rootfs=`` declared (always): the initrd's tmpfs, with
  the ``size=`` ``RootFS.initramfs(max_size=)`` set, or the volatile overlay
  ``systemd.volatile=overlay`` stacks on the erofs root the initrd opened as a read-only
  dm-verity target, which takes a write under ``/usr/bin``;
- ``service:UNIT``: each ``Service`` (a ``Service.packaged`` too) is active (or exited 0),
  ``socket:UNIT`` its socket when socket-activated, ``timer:UNIT`` the timer of a
  scheduled one;
- ``port:N/PROTO``: each port a service exposes has a listener in ``/proc/net`` (what
  ``ss -lnt``/``-lnu`` reads; ``iproute2`` is not in a minimal image);
- ``volume:PATH``: each ``Disk`` (a ``Volume``) is mounted there, ext4, and on a dm-crypt
  device when it has a key; an owned one (``OperatorKey()``) also says ``owner SHA256:...``
  (or ``owner none``), is a ``skip`` while it waits for ``tundravm secrets unlock`` (the
  probe does not wait for an operator), and fails when its header's owner is not the one
  the volume keeps inside; a ``BrokerKey`` one is a ``skip`` while it waits for its broker.
  The probe is ordered after nothing of a service whose state is on such a volume, and its
  ``service:`` and ``port:`` rows are a ``skip locked`` while the volume is not mounted, so
  a boot waiting for its operator still finishes its probe. Once the volume's step and the
  units waiting on it ran, :data:`AGAIN_UNIT` runs the whole probe again, its first line
  ``tundravm-probe: again: ...``, so the last run's lines and ``done`` are the verdicts
  after the unlock;
- ``dns``: with ``TeeBase``'s resolver, the first resolver's TLS name resolves through the
  stub, ``systemd-resolved`` holds a connection to port 853 (DNS over TLS) and, where
  ``resolvectl`` reaches it over D-Bus, reports ``+DNSOverTLS``;
- ``container:NAME``: each ``Container``'s unit is active in its root image, whose
  ``sha256:`` it prints (``fixture-boot`` compares it with the manifest's), and a datagram
  sent from inside its network namespace finds no route out, or with a veth its namespace
  holds its address; ``container:NAME:egress``: from its namespace an allowed TCP
  destination connects, while one no rule of its accepts in the mode the probe boots in
  and the metadata server are refused (TCP reset); ``container:NAME:endpoint:PORT``: each
  host endpoint answers inside its namespace; ``container:NAME:serves:PORT``: the host
  reaches each port it serves at its veth address; ``container:NAME:udp:PORT``: a datagram
  sent from a namespace of the probe's to the host's port reaches a socket in the
  container (its ``Udp InDatagrams``), for each exposed UDP port no firewall mode names;
- ``container-ssh:NAME``: each ``Container`` with ``ssh=``: its sshd runs as the uid the
  root's ``passwd`` names for its user, listens on its port inside its namespace, its host
  key is owned by that uid, and the evidence on the management port binds that key under
  the container's name;
- ``egress:ADDRESS:PORT``: with ``Egress``, a UDP datagram to an address and port no rule
  accepts is refused by the output chain (``EPERM``, at once; TCP would only time out);
  ``egress:HOST``: each host name a rule accepts holds addresses in its sets, which it
  prints (``egress-resolve`` filled them); ``time:nts``: chrony selected one of its NTS
  sources (``chronyc -N sources``), which it prints;
  ``egress:metadata:OWNER``: with a firewall, a datagram to the cloud metadata server sent
  as each service's user (``setpriv``; root with its group for the cloud glue) is refused
  unless that service's ``egress`` names it, and leaves the guest when it does;
- ``observability:UNIT``: with an ``Observability``, ``journal-export.service`` active and
  connected to its sink's port (the proxy's with one: the upload streams while it follows
  the journal) or with an upload's cursor saved, or restarting after an upload its journal
  names failing (the sink away), in place of its ``service:`` check;
  ``metrics-export.service`` active with its configuration accepted by ``promtool check
  config --agent``, or without promtool by the agent's own ``Completed loading of
  configuration file``; ``observability:series``: with a sink on the loopback (the in-guest
  receiver, or one a dev profile runs), the sink holds ``tundravm_image_digest`` and no
  series the keep-list drops (``promtool query labels``); ``observability:no-wait``: no
  other service requires or is ordered
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
- ``login:NAME``: each ``Login`` of an ``SshAccess``: the account exists with a locked
  password, its ``sudoers.d`` file parses (``visudo -c``), sshd's effective configuration
  for it (``sshd -T -C user=NAME``) forces its command (or none) and disables forwarding,
  and each baked key in its ``authorized_keys.d`` file carries the declared options;
- ``menu:NAME``: each ``Login`` with ``commands=``: its program is ``root:root 0755``,
  run as the login it lists every command for an empty request and refuses ``sh`` and an
  int above its bound, and ``sudo -l -U NAME`` lists exactly the lines its ``sudoers.d``
  file holds;
- ``ssh:root``: with ``PermitRootLogin no`` (an ``SshAccess`` without a key for root),
  sshd's effective configuration refuses root and root has no ``authorized_keys`` file;
- ``root``: ``/`` is ``root:root 0755`` (always: the initramfs layout's own unit makes it
  so, :mod:`tundravm._modules.root_mode`; a verity root is read-only beneath);
- ``key:instance``: the instance key (:mod:`tundravm._modules.instance_key`, always) is
  ``root:root 0600`` and its public key file holds its public key;
- ``mode:firewall``: with ``Firewall(modes=)``, ``mode-switch status``: the mode in force,
  the switches since boot, and whether the RTMR3 the evidence service reports is their
  replay;
- ``endpoint:PORT``: the attested endpoint on ``PORT`` answers ``GET /evidence`` with the
  nonce it was given, quoted by ``tdxs`` for the caller ``endpoint`` (the tag tdxs gives
  uid 0, :data:`ENDPOINT_TAG`), so no other service of the guest asked for it;
- ``attested-tls:PORT``: the attested TLS listener on ``PORT`` serves a certificate, and
  its key is the one the attested endpoint's evidence binds for that port
  (``Secrets(attested_tls=)``);
- ``owner:PATH``: each path a ``File(owner=)`` or ``Tree(owner=)`` declares is owned by
  that user and group once the ``owners`` runtime-init step ran, with the mode its ``File``
  declares.

The probe runs inside the guest it checks: it proves what the guest says of itself, which a
compromised guest could fake. It is a development aid, not evidence.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from tundravm._modules import container_ssh
from tundravm.attestation import ENDPOINT_CALLER, tagged
from tundravm.models import ServiceSpec, unit_name

from . import execution, instance_key, root_mode
from .disk_encryption import OWNER_DIRECTORY
from .init import HELD_TARGET

if TYPE_CHECKING:
    from tundravm.declarative.model import (
        ContainerNetwork,
        ContainerRoot,
        Declaration,
        EgressRule,
        Firewall,
    )
    from tundravm.declarative.utils import Command, Login

UNIT = "tundravm-probe.service"
SCRIPT_PATH = "/usr/lib/tundravm/probe"
PREFIX = "tundravm-probe: "
DONE = "done"
AGAIN = "again"
"""The first line of the run :data:`AGAIN_UNIT` starts: a reader drops what came before."""
AGAIN_UNIT = "tundravm-probe-again.service"
"""The probe's second run, once a held volume opens (:meth:`Probe.again`)."""
DROPIN_AGAIN = f"/usr/lib/systemd/system/{AGAIN_UNIT}.d/50-tundravm-production.conf"
WAIT_SECONDS = 30
"""How long a service may stay ``activating`` and a declared port stay unbound."""
DROPIN = f"/usr/lib/systemd/system/{UNIT}.d/50-tundravm-production.conf"
"""What a production profile extending a development default sets: the probe never runs."""
ENDPOINT_TAG = (b'"user_data":"' + base64.b64encode(tagged(ENDPOINT_CALLER, b""))).hex()
"""What the evidence's ``document`` (hex of the tdxs document's JSON) holds when tdxs quoted
it for the caller ``endpoint``: the start of its ``user_data``, ``lp("endpoint")`` in
base64, which ends on a whole base64 group."""
DENIED_ADDRESSES = ("192.0.2.1", "198.51.100.1", "203.0.113.1")
"""Documentation addresses (RFC 5737) the egress check sends to, the first no rule accepts."""
DENIED_PORTS = (9, 7, 13, 19, 37)
"""Ports the egress check sends to (discard first), the first no rule accepts."""
RESOLVE_WAIT_SECONDS = 90
"""How long a host name's sets may stay empty: ``egress-resolve`` runs once the network is
online and again every 30s."""

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
    "cmdline",
    "dns",
    "rootfs",
    "service",
    "active",
    "port",
    "volume",
    "container",
    "container-egress",
    "container-endpoint",
    "container-serves",
    "container-udp",
    "container-ssh",
    "egress",
    "hostset",
    "time",
    "metadata",
    "upload",
    "agent",
    "series",
    "nowait",
    "rules",
    "hardening",
    "symbols",
    "modules",
    "keys",
    "instance",
    "root",
    "login",
    "menu",
    "noroot",
    "endpoint",
    "attested-tls",
    "owner",
    "firewall-mode",
    "locked",
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
    """A profile's checks, and the units a held volume holds, which :meth:`again` waits
    for."""

    checks: tuple[Check, ...]
    held: tuple[str, ...] = ()

    @property
    def tools(self) -> tuple[str, ...]:
        """The ``systemd`` package's binaries the debloat must keep for this probe."""
        if any(check.helper == "hardening" for check in self.checks):
            return ("systemd-analyze",)
        return ()

    def script(self) -> str:
        """:data:`SCRIPT_PATH`: ``/bin/sh``, coreutils, grep, sed and the image's own tools."""
        used = {check.helper for check in self.checks}
        if any(check.call.startswith("probe_locked ") for check in self.checks):
            used.add("locked")
        parts = [_HEADER]
        parts += [_HELPERS[name] for name in _HELPERS if name in used]
        if self.held:
            parts.append(f'[ "${{1:-}}" = {AGAIN} ] && say "{AGAIN}: a held volume opened"\n')
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

    def again(self) -> ServiceSpec | None:
        """:data:`AGAIN_UNIT`, with a held volume: the whole probe again once the volume's
        step and the units waiting on it ran, so the last run's lines (and its ``done``) are
        the verdicts after ``tundravm secrets unlock``. ``None`` without one."""
        if not self.held:
            return None
        return ServiceSpec(
            name=AGAIN_UNIT,
            command=("/bin/sh", SCRIPT_PATH, AGAIN),
            description="Check again once a held volume opens",
            after=(UNIT, *self.held),
            type="oneshot",
            remain_after_exit=True,
            restart="no",
            restart_sec="",
            timeout_start="10min",
            hardening=(),
            needs=(),
            wanted_by=HELD_TARGET,
            extra_unit={"Service": {"StandardOutput": "journal+console"}},
        )

    def manifest(self) -> list[dict[str, str]]:
        return [check.manifest() for check in self.checks]


def derive(
    items: Sequence[Declaration],
    *,
    modules: Sequence[str] | None,
    kernel_off: Iterable[str] = (),
    glue: Sequence[Declaration] = (),
    cmdline: Sequence[str] = (),
    rootfs: str = "initramfs",
    root_size: str | None = None,
) -> Probe:
    """The probe *items* (a variant's resolved declarations) imply.

    *modules* is the curated module set the profile keeps (``None``: no module check, the
    image keeps every module); *kernel_off* the options a built kernel turns off (empty for
    a distribution kernel); *glue* the cloud units lowering adds that read the metadata
    servers (``tundravm._platforms.metadata_units``), checked for their egress only;
    *cmdline* the parameters the image appends to the UKI's command line; *rootfs*
    the root layout (``initramfs`` or ``verity``) and *root_size* the initramfs
    root's tmpfs cap, when one is set."""
    from tundravm._modules.secret_delivery import ENDPOINT_UNIT
    from tundravm.declarative._network import host_names
    from tundravm.declarative.model import (
        ContainerRoot,
        Disk,
        File,
        Firewall,
        Port,
        Secrets,
        Service,
        Task,
        Unit,
        host_sets,
    )
    from tundravm.declarative.systemd import ceiling
    from tundravm.declarative.utils import RESOLVED_DROPIN, RESOLVERS_DROPIN, SSH_HOST_KEY
    from tundravm.models import CONTAINERS_PATH

    checks = [
        Check("mode", f"{execution.MODE_FILE} says simulated or tdx", ""),
        Check("rtmr", "RTMR0..2 against measurements.json (skipped: needs a quote)", ""),
        _rootfs(rootfs, root_size),
    ]
    if cmdline:
        checks.append(
            Check(
                "cmdline",
                f"/proc/cmdline holds {' '.join(cmdline)}",
                " ".join(["probe_cmdline", *(_quote(p) for p in cmdline)]),
                "cmdline",
            )
        )
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
        elif isinstance(item, Unit) and _packaged(item):
            checks.append(_running(item.name))
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
    held = _held(items)
    for item in items:
        if isinstance(item, Disk):
            encrypted = item.key is not None
            waits = item.name in held
            owned = f" {OWNER_DIRECTORY}/{item.name}" if item.owner else ""
            checks.append(
                Check(
                    f"volume:{item.mount}",
                    f"{item.mount} is mounted, ext4"
                    + (", on dm-crypt" if encrypted else "")
                    + (", and who owns it" if item.owner else ""),
                    f"probe_volume {item.mount} {'yes' if encrypted else 'no'} "
                    f"{held[item.name][1] if waits else '-'}{owned}",
                    "volume",
                    () if waits else ("runtime-init.service",),
                )
            )
    for item in items:
        if isinstance(item, ContainerRoot):
            checks += _container(item, items, CONTAINERS_PATH)
    endpoint_ports = [item.port for item in items if isinstance(item, Secrets)]
    for item in items:
        if isinstance(item, ContainerRoot) and item.ssh_port is not None:
            assert item.account is not None
            management = str(endpoint_ports[0]) if endpoint_ports else "-"
            call = f"probe_container_ssh {item.name} {item.account.name} {item.ssh_port}"
            checks.append(
                Check(
                    f"container-ssh:{item.name}",
                    f"{item.name}'s sshd runs as the uid its root names {item.account.name}, "
                    f"listens on {item.ssh_port}, and the evidence binds its host key",
                    f"{call} {management}",
                    "container-ssh",
                    (unit_name(item.name),),
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
        for name in host_names(firewall, items):
            four, six = host_sets(name)
            checks.append(
                Check(
                    f"egress:{name}",
                    f"{name} resolved into {four} and {six}, the addresses its rules accept",
                    f"probe_hostset {name} {four} {six}",
                    "hostset",
                    ("nftables.service",),
                )
            )
    if any(isinstance(item, Firewall) for item in items):
        checks += _metadata(items, glue)
    files = {item.path: item for item in items if isinstance(item, File)}
    if RESOLVED_DROPIN in files:
        name = _resolver_name(files.get(RESOLVERS_DROPIN))
        checks.append(
            Check(
                "dns",
                f"{name} resolves over TLS: systemd-resolved connected to port 853",
                f"probe_dns {_quote(name)}",
                "dns",
                ("systemd-resolved.service", "systemd-networkd-wait-online.service"),
            )
        )
    chrony = files.get(CHRONY_CONF)
    servers = [] if chrony is None else _nts_servers(str(chrony.content))
    if servers:
        checks.append(
            Check(
                "time:nts",
                f"chrony selected an NTS source ({', '.join(servers)})",
                " ".join(["probe_time", *servers]),
                "time",
                ("chrony.service",),
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
        for port in item.attested_tls:
            relay = f"attested-tls-{port}.service"
            checks.append(
                Check(
                    f"attested-tls:{port}",
                    f"the certificate served on {port} carries the key the evidence on "
                    f"{item.port} binds for it",
                    f"probe_attested_tls {port} {item.port} {relay} {unit}",
                    "attested-tls",
                    (relay, unit),
                )
            )
    switchable = any(
        (isinstance(i, Unit) and i.name == "mode-switch.service")
        or (isinstance(i, Firewall) and i.modes)
        for i in items
    )
    if switchable:
        checks.append(
            Check(
                "mode:firewall",
                "the firewall mode in force, its switches since boot, and RTMR3 their replay",
                "probe_firewall_mode",
                "firewall-mode",
                ("mode-switch.service", "tdxs.socket"),
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
    checks.append(Check("root", "/ is root:root 0755", "probe_root", "root"))
    checks += _logins(items)
    checks.append(
        Check(
            "key:instance",
            f"{instance_key.KEY} root:root 0600, {instance_key.PUBLIC} its public key",
            f"probe_instance_key {instance_key.KEY} {instance_key.PUBLIC}",
            "instance",
            (instance_key.UNIT,),
        )
    )
    for step, path, owner in _owned(items):
        file = files.get(path)
        mode = [] if file is None or file.mode is None else [f"{file.mode:o}"]
        checks.append(
            Check(
                f"owner:{path}",
                " ".join([f"{path} is {owner}", *mode]),
                " ".join(["probe_owner", _quote(path), owner, *mode]),
                "owner",
                (f"runtime-init-{step}.service",),
            )
        )
    unheld, held_units = _unheld(checks, items)
    return Probe(tuple(unheld), held_units)


def _container(item: ContainerRoot, items: Sequence[Declaration], containers: str) -> list[Check]:
    """``container:NAME`` and, with a veth or host endpoints, the rows of its network."""
    from tundravm.declarative.model import CLOUD_METADATA, Firewall, Port

    unit = unit_name(item.name)
    image = f"{containers}/{item.name}.raw"
    network = item.network
    if network is None:
        address, number = DENIED_ADDRESSES[0], DENIED_PORTS[0]
        checks = [
            Check(
                f"container:{item.name}",
                f"{unit} is active in its root image, and a datagram to {address}:{number}"
                "/udp from its network namespace finds no route",
                f"probe_container {unit} {image} {address} {number}",
                "container",
                (unit,),
            )
        ]
    else:
        checks = [
            Check(
                f"container:{item.name}",
                f"{unit} is active in its root image, at {network.address} on its veth",
                f"probe_container {unit} {image} veth {network.address}",
                "container",
                (unit,),
            ),
            Check(
                f"container:{item.name}:egress",
                f"from its namespace what its egress allows connects, the rest and "
                f"{CLOUD_METADATA[0]} are refused",
                " ".join(["probe_container_egress", unit, *_container_targets(network)]),
                "container-egress",
                (unit, "nftables.service"),
            ),
        ]
    for endpoint in item.endpoints:
        host, _, port = endpoint.rpartition(":")
        relay = f"{item.name}-endpoint-{port}.socket"
        checks.append(
            Check(
                f"container:{item.name}:endpoint:{port}",
                f"{endpoint} answers inside its namespace",
                f"probe_container_endpoint {unit} {host} {port} {relay}",
                "container-endpoint",
                (unit, relay),
            )
        )
    if network is None:
        return checks
    for served in network.serves:
        if served.protocol == "tcp":
            checks.append(
                Check(
                    f"container:{item.name}:serves:{served.number}",
                    f"the host reaches {network.address}:{served.number} on its veth",
                    f"probe_container_serves {network.address} {served.number}",
                    "container-serves",
                    (unit, "nftables.service"),
                )
            )
    firewall = next((i for i in items if isinstance(i, Firewall)), None)
    moded = {port for mode in (firewall.modes if firewall else ()) for port in mode.ingress}
    for exposed in network.expose:
        if isinstance(exposed, Port) and exposed.protocol == "udp" and exposed not in moded:
            checks.append(
                Check(
                    f"container:{item.name}:udp:{exposed.number}",
                    f"a datagram to the host's {exposed.number}/udp reaches a socket in it",
                    f"probe_container_udp {unit} {exposed.number}",
                    "container-udp",
                    (unit, "nftables.service"),
                )
            )
    return checks


def _container_targets(network: ContainerNetwork) -> tuple[str, str, str]:
    """``(allowed, denied, metadata)`` TCP ``HOST:PORT`` the egress row connects to: an
    address its egress names, one it refuses in production (a mode's own, when one has
    one) and the metadata server unless it reaches it; ``-`` for none."""
    from tundravm.declarative.model import CLOUD_METADATA

    def single(host: str | None) -> bool:
        return host is not None and "/" not in host and ":" not in host

    rules = network.egress_rules
    allowed = next(
        (
            f"{r.host}:{r.port.number}"
            for r in rules
            if single(r.host) and r.port is not None and r.port.protocol == "tcp"
        ),
        "-",
    )
    always = {(r.host, r.port) for r in rules}
    moded = next(
        (
            f"{r.host}:{r.port.number}"
            for mode, r in network.modes
            if mode != "production"
            and single(r.host)
            and r.port is not None
            and r.port.protocol == "tcp"
            and (r.host, r.port) not in always
        ),
        None,
    )
    if moded is None:
        networks = [ipaddress.ip_network(r.host, strict=False) for r in rules if r.host]
        open_ports = {r.port.number for r in rules if r.host is None and r.name is None and r.port}
        address = next(
            (
                a
                for a in DENIED_ADDRESSES
                if not any(ipaddress.ip_address(a) in n for n in networks)
            ),
            None,
        )
        number = next((n for n in DENIED_PORTS if n not in open_ports), None)
        moded = "-" if address is None or number is None else f"{address}:{number}"
    reaches = any(r.host in CLOUD_METADATA for r in rules)
    return allowed, moded, "-" if reaches else f"{CLOUD_METADATA[0]}:80"


def _metadata(items: Sequence[Declaration], glue: Sequence[Declaration]) -> list[Check]:
    """One ``egress:metadata:OWNER`` check per user a service runs as (a task counts when
    its egress names a server; root counts with the group of a unit whose egress does): the
    metadata server refused unless one of its units' egress names it."""
    from tundravm.declarative.model import CLOUD_METADATA, Service, Task, egress_owner
    from tundravm.declarative.systemd import Dynamic, Root

    owners: dict[tuple[str, str | None], str | None] = {}
    for item in (*items, *glue):
        if not isinstance(item, (Service, Task)) or item.name.endswith("@"):
            continue
        if (not item.enabled) if isinstance(item, Service) else not item.egress:
            continue
        if not item.egress and (isinstance(item.user, (Dynamic, Root)) or item.user == "root"):
            continue
        owner = egress_owner(item)
        declared = next((r.host for r in item.egress if r.host in CLOUD_METADATA), None)
        if owners.get(owner) is None:
            owners[owner] = declared
    checks: list[Check] = []
    for (user, group), host in owners.items():
        name = user if group is None else group
        address = host or CLOUD_METADATA[0]
        what = "leaves the guest" if host else "is refused"
        checks.append(
            Check(
                f"egress:metadata:{name}",
                f"a datagram from {user} to {address} {what}"
                + (" (its egress names it)" if host else " (no egress of its names it)"),
                f"probe_metadata {user} {group or '-'} {address} {'yes' if host else 'no'}",
                "metadata",
                ("nftables.service",),
            )
        )
    return checks


def _logins(items: Sequence[Declaration]) -> Iterator[Check]:
    """``login:NAME`` for each ``Login``'s ``sshd_config.d`` file in *items*, ``menu:NAME``
    for each with commands, and ``ssh:root`` where sshd lets root in by no key."""
    from tundravm._source import TreeGoBuild
    from tundravm.declarative.model import Build, File
    from tundravm.declarative.utils import (
        SSH_AUTHORIZED_KEYS_DIR,
        SSH_CONFIG,
        SSH_LOGIN_CONFIG,
        SUDOERS_DIRECTORY,
        Login,
    )

    files = {item.path: item for item in items if isinstance(item, File)}
    programs = {
        item.using.origin.name: item.using.origin
        for item in items
        if isinstance(item, Build)
        and isinstance(item.using, TreeGoBuild)
        and isinstance(item.using.origin, Login)
    }
    prefix, _, suffix = SSH_LOGIN_CONFIG.partition("{name}")
    for path, item in files.items():
        if not (path.startswith(prefix) and path.endswith(suffix)):
            continue
        name = path[len(prefix) : -len(suffix)]
        config = item.content if isinstance(item.content, str) else ""
        forced = next(
            (line.split(None, 1)[1] for line in config.splitlines() if "ForceCommand" in line),
            "none",
        )
        sudoers = f"{SUDOERS_DIRECTORY}/{name}"
        keys = files.get(f"{SSH_AUTHORIZED_KEYS_DIR}/{name}")
        text = keys.content if keys is not None and isinstance(keys.content, str) else ""
        options = text.split(" ssh-", 1)[0] if text else "-"
        what = [f"{name} exists, password locked", f"sshd forces {forced}, no forwarding"]
        if sudoers in files:
            what.append(f"{sudoers} parses")
        if options != "-":
            what.append(f"its keys carry {options}")
        yield Check(
            f"login:{name}",
            "; ".join(what),
            " ".join(
                [
                    "probe_login",
                    name,
                    sudoers if sudoers in files else "-",
                    _quote(options),
                    _quote(forced),
                ]
            ),
            "login",
            ("ssh-host-keys.service",),
        )
        login = programs.get(name)
        if login is not None:
            developed = "PermitTTY yes" in config
            yield _menu(login.development() if developed else login, sudoers in files)
    sshd = files.get(SSH_CONFIG)
    if sshd is not None and "PermitRootLogin no\n" in str(sshd.content):
        yield Check(
            "ssh:root",
            "sshd refuses root; root has no authorized keys",
            "probe_no_root",
            "noroot",
            ("ssh-host-keys.service",),
        )


def _menu(login: Login, sudo: bool) -> Check:
    """``menu:NAME``: what the login's program lists and refuses as the login, and the sudo
    lines."""
    from tundravm.declarative.utils import SUDOERS_DIRECTORY

    name, path = login.name, str(login.program_path)
    bad = next(filter(None, (_over(n, c) for n, c in login.entries)), "-")
    names = [entry for entry, _ in login.entries]
    what = [f"{path} root:root 0755", f"lists {', '.join(names)}", "refuses sh"]
    if bad != "-":
        what.append(f"refuses {bad!r}")
    if sudo:
        what.append(f"sudo -l -U {name} lists only {SUDOERS_DIRECTORY}/{name}")
    sudoers = f"{SUDOERS_DIRECTORY}/{name}" if sudo else "-"
    call = ["probe_menu", name, path, sudoers, _quote(bad), *names]
    return Check(
        f"menu:{name}", "; ".join(what), " ".join(call), "menu", ("ssh-host-keys.service",)
    )


def _over(name: str, command: Command) -> str | None:
    """A request for *command* whose first int is one above its bound, every other value
    in its domain; ``None`` when it takes no int or a value no request can name."""
    words, over = [name], False
    for param in command.params:
        low, high = _bounds(param.type.usage()) if param.kind == "int" else (0, 0)
        if param.kind == "int" and not over:
            words += [param.name, str(high + 1)] if param.option else [str(high + 1)]
            over = True
        elif param.option:
            continue
        elif param.kind == "int":
            words.append(str(low))
        elif param.kind == "str":
            words.append(param.type.usage().split("|")[0])
        else:
            return None
    return " ".join(words) if over else None


def _bounds(usage: str) -> tuple[int, int]:
    low, _, high = usage.partition("-")
    return int(low), int(high)


def _resolver_name(file: object) -> str:
    """The TLS name of the first resolver the ``Network``'s ``DNS=`` line names, the name the
    ``dns`` check looks up (``deb.debian.org`` without one)."""
    content = getattr(file, "content", None)
    text = content if isinstance(content, str) else ""
    for line in text.splitlines():
        if line.startswith("DNS="):
            for entry in line.removeprefix("DNS=").split():
                if "#" in entry:
                    return entry.partition("#")[2]
    return "deb.debian.org"


def _rootfs(layout: str, size: str | None) -> Check:
    """The ``rootfs:LAYOUT`` check: ``/`` is what the image's ``rootfs=`` declared."""
    if layout == "verity":
        return Check(
            "rootfs:verity",
            "/ is the volatile overlay on the erofs root, a read-only dm-verity target, and "
            "takes a write under /usr/bin",
            "probe_rootfs verity",
            "rootfs",
        )
    kib = _kib(size)
    capped = "" if kib is None else f", capped at {size} (size={kib}k)"
    return Check(
        "rootfs:initramfs",
        f"/ is the initrd's tmpfs{capped}",
        " ".join(["probe_rootfs", "initramfs", *([] if kib is None else [str(kib)])]),
        "rootfs",
        () if kib is None else ("systemd-remount-fs.service",),
    )


def _kib(size: str | None) -> int | None:
    """*size* (``"4G"``, ``"512M"``) in KiB, as the kernel reports a tmpfs ``size=``;
    ``None`` for no cap or a percentage, which the kernel reports resolved."""
    if size is None or not size[:-1].isdigit() or size[-1] not in "KMG":
        return None
    return int(size[:-1]) << {"K": 0, "M": 10, "G": 20}[size[-1]]


def _owned(items: Iterable[Declaration]) -> Iterator[tuple[str, str, str]]:
    """``(step, path, user:group)`` of each ``chown`` line of the ``owners`` steps in
    *items*, what ``File(owner=)`` and ``Tree(owner=)`` lower to."""
    from tundravm._image._lower import OWNERS_STEP
    from tundravm.declarative.model import BootStep

    for item in items:
        if isinstance(item, BootStep) and item.name.partition("-")[0] == OWNERS_STEP:
            for line in item.script.splitlines():
                words = shlex.split(line)
                if words[:1] == ["chown"]:
                    yield item.name, words[-1], words[-2]


def _held(items: Sequence[Declaration]) -> dict[str, tuple[str, str]]:
    """``{volume: (mount, operator|broker)}``: each volume whose key arrives over a FIFO
    (``OperatorKey()``, ``BrokerKey``), which stays closed until someone hands it over."""
    from tundravm.declarative.model import Disk, Key, Ref

    pipes = {i.name for i in items if isinstance(i, Key) and i.strategy == "pipe"}
    found: dict[str, tuple[str, str]] = {}
    for item in items:
        if not isinstance(item, Disk):
            continue
        key = item.key
        name = key.identity[0] if isinstance(key, Ref) and key.identity else None
        if isinstance(key, Key):
            name = key.name if key.strategy == "pipe" else None
        if item.owner or name in pipes:
            found[item.name] = (item.mount, "operator" if item.owner else "broker")
    return found


def _unheld(
    checks: list[Check], items: Sequence[Declaration]
) -> tuple[list[Check], tuple[str, ...]]:
    """*checks* with each one that reads a service waiting on a held volume (:func:`_held`)
    ordered after none of its units and run only once that volume is mounted (``hardening:``
    reads the unit file, so it runs regardless): ordering the probe after the service would
    hold the whole probe until someone unlocks the volume. Then the units :data:`AGAIN_UNIT`
    waits for: each held volume's step and the units the checks no longer wait for."""
    from tundravm.declarative.model import Service

    held = _held(items)
    waiting: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Service):
            continue
        for volume in held:
            if f"runtime-init-volume-{volume}.service" in map(str, item.requires):
                stem = unit_name(item.name).removesuffix(".service")
                waiting[f"{stem}.service"] = waiting[f"{stem}-ready.service"] = volume
    found: list[Check] = []
    units = [f"runtime-init-volume-{volume}.service" for volume in held]
    for check in checks:
        on = next((waiting[unit] for unit in check.after if unit in waiting), None)
        if on is None:
            found.append(check)
            continue
        units += [unit for unit in check.after if unit in waiting and unit not in units]
        after = tuple(unit for unit in check.after if unit not in waiting)
        if check.helper == "hardening":
            found.append(replace(check, after=after))
            continue
        mount, how = held[on]
        found.append(
            replace(
                check,
                call=f"probe_locked {mount} {on} {how} || {check.call}",
                after=after,
            )
        )
    return found, tuple(units)


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
    return [_running(unit)]


def _running(unit: str) -> Check:
    """The ``service:UNIT`` check: *unit* active or exited 0, else why not."""
    stem = unit.removesuffix(".service")
    return Check(
        f"service:{unit}",
        f"{unit} is active, or exited 0",
        f"probe_service {unit}",
        "service",
        (unit, f"{stem}-ready.service"),
    )


def _packaged(item: Declaration) -> bool:
    """Whether *item* is a ``Service.packaged``: a package's enabled ``.service``, held to
    the execution gate by its drop-in as a generated service is."""
    from tundravm.declarative.model import Unit

    assert isinstance(item, Unit)
    return (
        bool(item.enabled)
        and item.content is None
        and item.name.endswith(".service")
        and item.dropin is not None
        and execution.GATE_UNIT in item.dropin.requires
    )


def _observability(
    items: Sequence[Declaration], firewall: Firewall | None
) -> tuple[frozenset[str], list[Check]]:
    """The exporters an ``Observability`` renders among *items*, and their checks: each
    running (or retrying a sink), its configuration taken by the program itself, no service
    waiting on it and, with a filtering *firewall*, the rules its sinks asked for loaded."""
    from tundravm.components._observability import LOGS_SERVICE, METRICS_SERVICE
    from tundravm.declarative._network import egress_matches
    from tundravm.declarative.model import File, Service

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
        argv = metrics.exec_start
        words = argv.split() if isinstance(argv, str) else argv
        features = [w.partition("=")[2] for w in words if w.startswith("--enable-feature=")]
        agent = any("agent" in flag.split(",") for flag in features)
        checks.append(
            Check(
                f"observability:{unit}",
                f"{unit} is active; Prometheus takes {config} (promtool, else its log)",
                f"probe_agent {unit} {config}{' --agent' if agent else ''}",
                "agent",
                (unit,),
            )
        )
        rendered = next((i for i in items if isinstance(i, File) and i.path == config), None)
        written = None if rendered is None else _remote_write(str(rendered.content))
        if written is not None:
            sink, keep = written
            held = ["tundravm_image_digest", *(name for name in keep.split("|") if ":" in name)]
            checks.append(
                Check(
                    "observability:series",
                    f"the sink at {sink} holds {', '.join(held)} and only series {keep} keeps, "
                    "each with instance= this boot's instance key",
                    " ".join(["probe_series", _quote(sink), _quote(keep), *map(_quote, held)]),
                    "series",
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
        if "observability" in rule.origins
    ]
    if rules:
        checks.append(
            Check(
                "observability:egress",
                f"the output chain accepts {', '.join(map(str, rules))} (the sinks' egress)",
                " ".join(
                    ["probe_rules", *(_quote(m) for rule in rules for m in egress_matches(rule))]
                ),
                "rules",
                ("nftables.service",),
            )
        )
    return frozenset(found), checks


def _remote_write(config: str) -> tuple[str, str] | None:
    """The agent's remote-write sink (``scheme://host:port``) and keep-list regex in
    *config* (``metrics-export.json``), when the sink is on the loopback, where the probe can
    ask it."""
    remote = json.loads(config)["remote_write"][0]
    sink = remote["url"]
    regex = next(r["regex"] for r in remote["write_relabel_configs"] if r["action"] == "keep")
    parts = urlsplit(sink)
    try:
        local = (
            parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
        )
    except ValueError:
        local = False
    return (f"{parts.scheme}://{parts.netloc}", regex) if local else None


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
    ports = [rule.port for rule in rules if rule.host is None and rule.name is None]
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


CHRONY_CONF = "/etc/chrony/chrony.conf"


def _nts_servers(config: str) -> list[str]:
    """The ``server NAME ... nts`` sources of a chrony configuration."""
    return [
        words[1]
        for words in (line.split() for line in config.splitlines())
        if len(words) > 2 and words[0] == "server" and "nts" in words[2:]
    ]


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
    "series": f"""\
probe_series() {{
    sink=$1
    keep=$2
    shift 2
    if ! command -v promtool >/dev/null 2>&1; then
        skip "no promtool in the image to ask the sink $sink"
        return
    fi
    waited=0
    while :; do
        names=$(promtool query labels "$sink" __name__ 2>/dev/null) || names=
        missing=
        for name in "$@"; do
            printf '%s\\n' "$names" | grep -qxF "$name" || missing="$missing $name"
        done
        [ -z "$missing" ] && break
        if [ "$waited" -ge {3 * WAIT_SECONDS} ]; then
            fail "the sink at $sink holds no$missing after {3 * WAIT_SECONDS}s: a rule whose expression selects nothing records nothing; journalctl -u image-metrics -u metrics-export says why"
            return
        fi
        sleep 1
        waited=$((waited + 1))
    done
    dropped=$(printf '%s\\n' "$names" | grep -Evx "$keep" | tr '\\n' ' ')
    if [ -n "$dropped" ]; then
        fail "the sink at $sink holds series the keep-list drops: $dropped"
        return
    fi
    instances=$(promtool query labels "$sink" instance 2>/dev/null | grep . | tr '\\n' ' ')
    key=$(cat {instance_key.PUBLIC} 2>/dev/null || true)
    if [ "$instances" != "$key " ]; then
        fail "the sink at $sink labels its series instance=${{instances:-none}}, not this boot's instance key $key: metrics-export reads it from {instance_key.ENVIRONMENT}; journalctl -u metrics-export"
        return
    fi
    ok "the sink at $sink holds only what export= keeps, instance=$key: $(printf '%s\\n' "$names" | grep . | tr '\\n' ' ')"
}}
""",  # noqa: E501
    "cmdline": """\
probe_cmdline() {
    line=" $(cat /proc/cmdline) "
    missing=
    for parameter in "$@"; do
        case $line in *" $parameter "*) ;; *) missing="$missing $parameter" ;; esac
    done
    if [ -z "$missing" ]; then
        ok "$*"
    else
        fail "missing:$missing: the kernel did not boot the UKI's own command line; boot the UKI without a loader that replaces it (cat /proc/cmdline)"
    fi
}
""",  # noqa: E501
    "dns": f"""\
probe_dns() {{
    name=$1
    if ! command -v getent >/dev/null 2>&1; then
        skip "no getent in the image to resolve with"
        return
    fi
    if protocols=$(resolvectl status --no-pager 2>/dev/null | sed -n 's/^ *Protocols: //p' | head -n 1) &&
        [ -n "$protocols" ]; then
        case " $protocols " in
            *" +DNSOverTLS "*) ;;
            *) fail "resolvectl status says $protocols, not +DNSOverTLS: systemd-resolved does not read TeeBase's resolved.conf.d; journalctl -u systemd-resolved"; return ;;
        esac
    fi
    waited=0
    until answer=$(getent ahostsv4 "$name" 2>&1); do
        if [ "$waited" -ge {WAIT_SECONDS} ]; then
            fail "$name does not resolve after {WAIT_SECONDS}s: systemd-resolved reaches its resolvers on 853/tcp only (DNSOverTLS=yes), so the output chain must accept it; journalctl -u systemd-resolved"
            return
        fi
        sleep 1
        waited=$((waited + 1))
    done
    address=$(echo "$answer" | head -n 1 | cut -d ' ' -f 1)
    peer=
    for table in /proc/net/tcp /proc/net/tcp6; do
        [ -r "$table" ] || continue
        while read -r _ _ remote state _; do
            [ "${{remote##*:}}" = 0355 ] && [ "$state" != 0A ] || continue
            peer=${{remote%:*}}
            if [ "${{#peer}}" -eq 8 ]; then
                set -- $(echo "$peer" | sed 's/\\(..\\)\\(..\\)\\(..\\)\\(..\\)/0x\\4 0x\\3 0x\\2 0x\\1/')
                peer=$(printf '%d.%d.%d.%d' "$@")
            fi
            break 2
        done <"$table"
    done
    if [ -z "$peer" ]; then
        fail "$name resolved to $address, but nothing is connected to port 853: the answer did not come over TLS; journalctl -u systemd-resolved"
    else
        ok "DNSOverTLS=yes${{protocols:+ ($protocols)}}: $name is $address, resolved over TLS to $peer:853"
    fi
}}
""",  # noqa: E501
    "rootfs": """\
probe_rootfs() {
    layout=$1
    size=${2:-}
    mount=
    while read -r line; do
        set -- $line
        [ "$5" = / ] && mount=${line#* - }
    done </proc/self/mountinfo
    set -- $mount
    fstype=${1:-}
    options=${3:-}
    if [ "$layout" = verity ]; then
        device=
        for dm in /sys/block/dm-*; do
            [ -e "$dm/dm/name" ] && [ "$(cat "$dm/dm/name")" = root ] || continue
            device="${dm##*/} uuid=$(cat "$dm/dm/uuid") ro=$(cat "$dm/ro")"
        done
        case $device in
            *" uuid=CRYPT-VERITY-"*" ro=1") ;;
            "") fail "no device-mapper device named root: the initrd did not open the verity root; journalctl -b -u systemd-veritysetup@root.service"; return ;;
            *) fail "the root device is $device, not a read-only dm-verity target"; return ;;
        esac
        case $fstype,$options, in
            overlay,*,lowerdir=/sysroot,*) ;;
            *) fail "/ is $fstype ($options), not the overlay systemd.volatile=overlay stacks on /sysroot"; return ;;
        esac
        if touch /usr/bin/.tundravm-probe 2>/dev/null; then
            rm -f /usr/bin/.tundravm-probe
            ok "overlay on the erofs root under dm-verity (${device%% *}, read-only); a write under /usr/bin lands in the overlay"
        else
            fail "a write under /usr/bin is refused: the overlay's upper layer is not writable"
        fi
        return
    fi
    case $fstype in
        rootfs|tmpfs) ;;
        *) fail "/ is $fstype, not the initrd's tmpfs: the OS is not in the UKI's initrd"; return ;;
    esac
    if [ -z "$size" ]; then
        ok "$fstype"
    else
        case ,$options, in
            *,size=${size}k,*) ok "$fstype, size=${size}k" ;;
            *) fail "/ is $fstype ($options), not capped at size=${size}k: systemd-remount-fs did not apply /etc/fstab's RootFS.initramfs(max_size=); journalctl -u systemd-remount-fs.service" ;;
        esac
    fi
}
""",  # noqa: E501
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
    elif systemctl status --no-pager -n 20 "$1" 2>&1 | grep -Eq 'CAPABILIT|EPERM|Operation not permitted'; then
        fail "$state ($result): a denied capability; grant it with Hardening.preset(..., capabilities=[...]); journalctl -u $1 says which"
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
            *"\\"nonce\\":\\"$nonce\\""*{ENDPOINT_TAG}*) ok "listening, evidence answers nonce $nonce, caller endpoint"; return ;;
            *"\\"nonce\\":\\"$nonce\\""*) fail "evidence for nonce $nonce on :$1 is not quoted for caller endpoint: journalctl -u tdxs says who asked"; return ;;
        esac
        [ "$waited" -ge {WAIT_SECONDS} ] && break
        sleep 1
        waited=$((waited + 1))
    done
    fail "no evidence for nonce $nonce on :$1: journalctl -u $2 -u tdxs; ${{said:-no answer}}"
}}
""",  # noqa: E501
    "attested-tls": f"""\
probe_attested_tls() {{
    if ! command -v curl >/dev/null 2>&1; then
        skip "no curl in the image to ask the endpoint"
        return
    fi
    waited=0
    while :; do
        served=$(openssl s_client -connect "127.0.0.1:$1" </dev/null 2>/dev/null |
            openssl x509 -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | base64 -w 0)
        nonce=$(tr -d '-' </proc/sys/kernel/random/uuid)
        said=$(curl -ksS --max-time 10 "https://127.0.0.1:$2/evidence?nonce=$nonce" 2>&1) || true
        if [ -n "$served" ]; then
            case $said in
                *"\\"port\\":$1,\\"public_key\\":\\"$served\\""*) ok "certificate served, its key bound by the evidence on :$2"; return ;;
                *"\\"nonce\\":\\"$nonce\\""*) fail "the certificate on :$1 carries a key the evidence on :$2 does not bind for it: journalctl -u $3 -u $4"; return ;;
            esac
        fi
        [ "$waited" -ge {WAIT_SECONDS} ] && break
        sleep 1
        waited=$((waited + 1))
    done
    if [ -z "$served" ]; then
        fail "no certificate served on :$1 after {WAIT_SECONDS}s: journalctl -u $3"
    else
        fail "no evidence for nonce $nonce on :$2: journalctl -u $4 -u tdxs; ${{said:-no answer}}"
    fi
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
    held=$3
    owned=${4:-}
    owner=
    if [ -n "$owned" ]; then
        owner=", owner none"
        waited=0
        until findmnt -n --mountpoint "$mount" >/dev/null 2>&1 || [ "$waited" -ge 30 ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if [ -r "$owned" ]; then
            read -r _ _ fingerprint _ <"$owned" || true
            owner=", owner ${fingerprint:-none}"
        fi
        if [ -r "$owned.mismatch" ]; then
            fail "$(cat "$owned.mismatch"): the host rewrote its header, so it stays closed; keep the disk and attach it to an instance you attested"
            return
        fi
    fi
    set -- $(findmnt -n -o FSTYPE,SOURCE --mountpoint "$mount" 2>/dev/null)
    if [ $# -lt 2 ] && [ "$held" = operator ]; then
        skip "locked until \\`tundravm secrets unlock INSTANCE ${owned##*/}\\`$owner"
    elif [ $# -lt 2 ] && [ "$held" = broker ]; then
        skip "locked until its broker releases the key"
    elif [ $# -lt 2 ]; then
        fail "nothing is mounted at $mount: unlock it (tundravm secrets unlock INSTANCE VOLUME) or read journalctl -u runtime-init.service"
    elif [ "$1" != ext4 ]; then
        fail "$mount is $1 on $2, not ext4: disk-setup formats ext4, so this is not the volume"
    elif [ "$encrypted" = no ]; then
        ok "ext4 on $2"
    else
        device=$(readlink -f "$2")
        uuid=$(cat "/sys/class/block/${device##*/}/dm/uuid" 2>/dev/null || true)
        case $uuid in
            CRYPT-*) ok "ext4 on $2, dm-crypt$owner" ;;
            *) fail "$mount is on $2, not a dm-crypt device: an encrypted volume mounts from its LUKS mapping; journalctl -u runtime-init.service" ;;
        esac
    fi
}
""",  # noqa: E501
    "locked": """\
probe_locked() {
    command -v findmnt >/dev/null 2>&1 || return 1
    findmnt -n --mountpoint "$1" >/dev/null 2>&1 && return 1
    case $3 in
        operator) skip "locked: it starts once \\`tundravm secrets unlock INSTANCE $2\\` opens $1" ;;
        *) skip "locked: it starts once the broker releases the key of $2" ;;
    esac
}
""",  # noqa: E501
    "container": f"""\
container_connect() {{
    if said=$(timeout 10 nsenter -t "$1" -n bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$2" "$3" 2>&1); then
        return 0
    fi
    echo "${{said:-no answer in 10s}}" | tail -n 1
    return 1
}}
probe_container() {{
    state=$(systemctl show -P ActiveState "$1")
    waited=0
    while [ "$state" != active ] && [ "$waited" -lt {WAIT_SECONDS} ]; do
        sleep 1
        waited=$((waited + 1))
        state=$(systemctl show -P ActiveState "$1")
    done
    if [ "$state" != active ]; then
        fail "$state: journalctl -u $1 says why"
        return
    fi
    if [ "$(systemctl show -P RootImage "$1")" != "$2" ]; then
        fail "$1 does not run in $2: its drop-in is missing; systemctl cat $1"
        return
    fi
    if ! digest=$(sha256sum "$2" 2>/dev/null); then
        fail "no root image at $2"
        return
    fi
    digest=sha256:${{digest%% *}}
    pid=$(systemctl show -P MainPID "$1")
    if [ "$3" = veth ]; then
        if grep -qF -- "$4" "/proc/$pid/net/fib_trie" 2>/dev/null; then
            ok "active, root $digest, at $4 on its veth"
        else
            fail "root $digest, but its namespace holds no $4: journalctl -u ${{1%.service}}-net.service"
        fi
        return
    fi
    if ! command -v nsenter >/dev/null 2>&1 || ! command -v bash >/dev/null 2>&1; then
        skip "active, root $digest; no nsenter or bash to send a datagram from its namespace"
        return
    fi
    if said=$(nsenter -t "$pid" -n bash -c 'printf x >"/dev/udp/$0/$1"' "$3" "$4" 2>&1); then
        fail "root $digest, but a datagram to $3:$4/udp left its namespace: it must hold lo only; cat /proc/$pid/net/dev"
        return
    fi
    case $said in
        *unreachable*) ok "active, root $digest, no route out of its namespace" ;;
        *) fail "root $digest, a datagram to $3:$4/udp failed with $(echo "$said" | tail -n 1), not for want of a route" ;;
    esac
}}
""",  # noqa: E501
    "container-egress": """\
probe_container_egress() {
    if ! command -v nsenter >/dev/null 2>&1 || ! command -v bash >/dev/null 2>&1; then
        skip "no nsenter or bash to connect from its namespace"
        return
    fi
    pid=$(systemctl show -P MainPID "$1")
    shown=
    if [ "$2" != - ]; then
        if ! said=$(container_connect "$pid" "${2%:*}" "${2##*:}"); then
            fail "$2/tcp, which its egress allows, did not connect ($said): nft list chain inet filter forward"
            return
        fi
        shown="reaches $2/tcp, "
    fi
    for denied in "$3" "$4"; do
        [ "$denied" = - ] && continue
        if said=$(container_connect "$pid" "${denied%:*}" "${denied##*:}"); then
            fail "$denied/tcp connected, but no egress rule of its accepts it: nft list chain inet filter forward"
            return
        fi
        case $said in
            *refused*) shown="$shown$denied/tcp refused, " ;;
            *) fail "$denied/tcp failed with $said, not a reset from the forward chain"; return ;;
        esac
    done
    ok "${shown%, }"
}
""",  # noqa: E501
    "container-endpoint": """\
probe_container_endpoint() {
    if ! command -v nsenter >/dev/null 2>&1 || ! command -v bash >/dev/null 2>&1; then
        skip "no nsenter or bash to connect from its namespace"
        return
    fi
    pid=$(systemctl show -P MainPID "$1")
    if said=$(container_connect "$pid" "$2" "$3"); then
        ok "$2:$3 answers inside its namespace"
    else
        fail "$2:$3 does not answer inside its namespace ($said): systemctl status $4"
    fi
}
""",
    "container-serves": """\
probe_container_serves() {
    if ! command -v bash >/dev/null 2>&1; then
        skip "no bash to connect with"
        return
    fi
    if said=$(timeout 10 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>&1); then
        ok "the host reaches $1:$2 on its veth"
    else
        fail "the host does not reach $1:$2 (${said:-no answer in 10s}): nft list chain inet filter output"
    fi
}
""",  # noqa: E501
    "container-udp": """\
udp_in() {
    set -- $(grep '^Udp:' "/proc/$1/net/snmp" | sed -n 2p)
    echo "${2:-0}"
}
probe_container_udp() {
    for tool in ip nsenter bash; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            skip "no $tool to send a datagram from a namespace of the probe's"
            return
        fi
    done
    pid=$(systemctl show -P MainPID "$1")
    ip link delete vp-probe >/dev/null 2>&1
    ip netns delete tundravm-probe >/dev/null 2>&1
    if ! { ip netns add tundravm-probe &&
        ip link add vp-probe type veth peer name probe0 netns tundravm-probe &&
        ip addr add 169.254.255.1/30 dev vp-probe && ip link set vp-probe up &&
        ip -n tundravm-probe addr add 169.254.255.2/30 dev probe0 &&
        ip -n tundravm-probe link set probe0 up; } >/dev/null 2>&1; then
        fail "the probe could not make its namespace and veth: is veth among the curated modules?"
        ip netns delete tundravm-probe >/dev/null 2>&1
        return
    fi
    before=$(udp_in "$pid")
    after=$before
    waited=0
    while [ "$after" -le "$before" ] && [ "$waited" -lt 10 ]; do
        nsenter --net=/run/netns/tundravm-probe bash -c 'printf tundravm-probe >"/dev/udp/$0/$1"' 169.254.255.1 "$2" 2>/dev/null
        sleep 1
        waited=$((waited + 1))
        after=$(udp_in "$pid")
    done
    ip link delete vp-probe >/dev/null 2>&1
    ip netns delete tundravm-probe >/dev/null 2>&1
    if [ "$after" -gt "$before" ]; then
        ok "a datagram to the host's $2/udp reached a socket in it (Udp InDatagrams $before -> $after)"
    else
        fail "no datagram to the host's $2/udp reached a socket in it in 10s: nft list chain inet filter prerouting; is its program listening?"
    fi
}
""",  # noqa: E501
    "container-ssh": f"""\
probe_container_ssh() {{
    unit=$1-ssh.service
    [ "$(systemctl show -P LoadState "$unit")" = loaded ] || unit=$1.service
    dir={container_ssh.DIRECTORY}/$1/host
    if ! want=$(id -u "$2" 2>/dev/null); then
        fail "no user $2 on the host: the image's useradd for it did not run; read the build log"
        return
    fi
    hex=$(printf '%04X' "$3")
    waited=0
    while :; do
        pid=$(systemctl show -P MainPID "$unit")
        listening=
        if [ "$pid" -gt 0 ]; then
            for table in /proc/"$pid"/net/tcp /proc/"$pid"/net/tcp6; do
                [ -r "$table" ] || continue
                while read -r _ local _ state _; do
                    [ "$state" = 0A ] && [ "${{local##*:}}" = "$hex" ] && listening=yes
                done <"$table"
            done
        fi
        [ -n "$listening" ] && break
        if [ "$waited" -ge {WAIT_SECONDS} ]; then
            fail "$unit does not listen on $3 in its namespace after {WAIT_SECONDS}s: journalctl -u $unit -u $1-ssh-keys"
            return
        fi
        sleep 1
        waited=$((waited + 1))
    done
    running=$(sed -n 's/^Uid:[[:space:]]*\\([0-9]*\\).*/\\1/p' /proc/"$pid"/status)
    if [ "$running" != "$want" ]; then
        fail "$unit runs as uid $running, not $2's $want"
        return
    fi
    named=$(grep "^$2:" /proc/"$pid"/root/etc/passwd | cut -d: -f3)
    if [ "$named" != "$want" ]; then
        fail "its root's passwd gives $2 uid ${{named:-none}}, the host $want: sshd would not find its own account"
        return
    fi
    if [ "$(stat -c %u "$dir/{container_ssh.KEY}" 2>/dev/null)" != "$want" ]; then
        fail "no host key in $dir owned by uid $want: journalctl -u $1-ssh-keys"
        return
    fi
    key=$(cut -d ' ' -f 1,2 "$dir/{container_ssh.KEY}.pub")
    print=$(ssh-keygen -l -f "$dir/{container_ssh.KEY}.pub" | cut -d ' ' -f 2)
    if [ "$4" = - ]; then
        ok "uid $want ($2 in its root), sshd on :$3, host key $print; no endpoint to bind it"
        return
    fi
    nonce=$(tr -d '-' </proc/sys/kernel/random/uuid)
    said=$(curl -ksS --max-time 10 "https://127.0.0.1:$4/evidence?nonce=$nonce" 2>&1) || true
    case $said in
        *"\\"name\\":\\"$1\\",\\"port\\":$3,\\"ssh_host_keys\\":[\\"$key\\"]"*)
            ok "uid $want ($2 in its root), sshd on :$3, host key $print bound by the evidence on :$4" ;;
        *) fail "the evidence on :$4 does not bind $1's host key $print: journalctl -u tundravm-endpoint; ${{said:-no answer}}" ;;
    esac
}}
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
    "hostset": f"""\
probe_hostset() {{
    if ! command -v nft >/dev/null 2>&1; then
        skip "no nft in the image"
        return
    fi
    waited=0
    while :; do
        held=
        for set in "$2" "$3"; do
            elements=$(nft list set inet filter "$set" 2>/dev/null | tr -d '\\n\\t' | tr -s ' ' |
                sed -n 's/.*elements = {{ *\\([^}}]*[^ }}]\\) *}}.*/\\1/p')
            [ -z "$elements" ] || held="$held${{held:+, }}$elements"
        done
        if [ -n "$held" ]; then
            ok "$1 holds $held"
            return
        fi
        if [ "$waited" -ge {RESOLVE_WAIT_SECONDS} ]; then
            fail "$1 resolved to nothing after {RESOLVE_WAIT_SECONDS}s, so its rules accept no address: egress-resolve fills $2 and $3 through systemd-resolved; journalctl -u egress-resolve.service"
            return
        fi
        sleep 1
        waited=$((waited + 1))
    done
}}
""",  # noqa: E501
    "time": f"""\
probe_time() {{
    if ! command -v chronyc >/dev/null 2>&1; then
        skip "no chronyc in the image"
        return
    fi
    waited=0
    while :; do
        sources=$(chronyc -N sources 2>/dev/null)
        for name in "$@"; do
            line=$(echo "$sources" | grep -E "^\\^[*+] +$name " | head -n 1)
            if [ -n "$line" ]; then
                ok "$(echo "$line" | tr -s ' ')"
                return
            fi
        done
        if [ "$waited" -ge {RESOLVE_WAIT_SECONDS} ]; then
            seen=$(for name in "$@"; do echo "$sources" | grep -E "^.. +$name "; done | tr -s ' ' | paste -sd ';' -)
            fail "no NTS source selected after {RESOLVE_WAIT_SECONDS}s (${{seen:-chronyc lists none of $*}}): chrony's key exchange (TCP 4460) and NTP (UDP 123 as _chrony) must pass the output chain; chronyc -N authdata"
            return
        fi
        sleep 1
        waited=$((waited + 1))
    done
}}
""",  # noqa: E501
    "metadata": """\
probe_metadata() {
    if ! command -v setpriv >/dev/null 2>&1 || ! command -v bash >/dev/null 2>&1; then
        skip "no setpriv or bash to send a datagram as $1"
        return
    fi
    group=$2
    [ "$group" = - ] && group=$(id -g "$1" 2>/dev/null)
    if [ -z "$group" ]; then
        fail "no user $1 in the image: the service runs as a user lowering declares; journalctl -u nftables.service"
        return
    fi
    if said=$(setpriv --reuid "$1" --regid "$group" --clear-groups bash -c 'printf x >"/dev/udp/$0/80"' "$3" 2>&1); then
        verdict=sent
    else
        case $said in *"not permitted"*) verdict=refused ;; *) verdict=failed ;; esac
    fi
    case $4/$verdict in
        no/refused) ok "refused for $1:$group (Operation not permitted): it declares no Egress(metadata=True)" ;;
        no/sent) fail "a datagram from $1:$group to $3 left the guest: the output chain must drop the metadata server for a service that does not declare it; systemctl status nftables.service" ;;
        yes/sent) ok "a datagram from $1:$group to $3 left the guest: its egress names the metadata server" ;;
        yes/refused) fail "refused for $1:$group, whose egress names $3: the ruleset lacks its rule, or the user or group differs; nft list chain inet filter output" ;;
        *) skip "a datagram to $3 failed before the firewall ($(echo "$said" | tail -n 1)): no route, so the rule is untested" ;;
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
        if ! said=$(promtool check config ${{3:-}} --syntax-only "$2" 2>&1); then
            fail "promtool check config ${{3:+$3 }}$2: $(echo "$said" | tail -n 1): this Prometheus refuses what PrometheusRemoteWrite renders"
            return
        fi
        parsed="promtool check config ${{3:+$3 }}accepts $2"
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
    "root": f"""\
probe_root() {{
    said=$(stat -c '%U:%G %a' /)
    if [ "$said" = "root:root 755" ]; then
        ok "0755 root:root"
    else
        fail "/ is $said: it must be root:root 0755, or anyone writes beside /etc and sshd refuses every key under /etc/ssh; journalctl -u {root_mode.UNIT}"
    fi
}}
""",  # noqa: E501
    "login": """\
probe_login() {
    if ! entry=$(getent shadow "$1"); then
        fail "no user $1: the image's useradd for it did not run; read the build log"
        return
    fi
    hash=$(echo "$entry" | cut -d: -f2)
    case $hash in
        '!'* | '*'*) ;;
        *) fail "$1 has a password: a login has none; something set one (chpasswd, usermod -p)"; return ;;
    esac
    if [ "$2" != - ] && ! said=$(visudo -c -f "$2" 2>&1); then
        fail "visudo -c -f $2: $(echo "$said" | tail -n 1): sudo refuses the file, so no rule runs"
        return
    fi
    if ! effective=$(sshd -T -C "user=$1,host=probe,addr=127.0.0.1" 2>&1); then
        fail "sshd -T: $(echo "$effective" | tail -n 1)"
        return
    fi
    forced=$(echo "$effective" | sed -n 's/^forcecommand //p')
    if [ "$forced" != "$4" ]; then
        fail "sshd forces '$forced' for $1, declared '$4': another sshd_config.d file overrides its Match block"
        return
    fi
    if ! echo "$effective" | grep -qx 'disableforwarding yes'; then
        fail "sshd lets $1 forward: another sshd_config.d file overrides its Match block"
        return
    fi
    keys=/etc/ssh/authorized_keys.d/$1
    if [ "$3" != - ] && [ -e "$keys" ]; then
        while read -r line; do
            case $line in
                "$3 "*) ;;
                *) fail "a key in $keys lacks '$3': the file is not the one the image declares"; return ;;
            esac
        done <"$keys"
    fi
    parsed=
    [ "$2" = - ] || parsed="; $2 parses"
    ok "password locked; sshd forces $4, no forwarding$parsed"
}
""",  # noqa: E501
    "menu": """\
probe_menu() {
    name=$1 file=$2 sudoers=$3 bad=$4
    shift 4
    said=$(stat -c '%U:%G %a' "$file" 2>&1)
    if [ "$said" != "root:root 755" ]; then
        fail "$file is $said, not root:root 755: the login could change its own program"
        return
    fi
    as_login() { setpriv --reuid "$name" --regid "$name" --init-groups env -i SSH_ORIGINAL_COMMAND="$1" "$file" 2>&1; }
    if ! listing=$(as_login ''); then
        fail "$file as $name lists nothing: $listing"
        return
    fi
    for entry in "$@"; do
        if ! printf '%s\\n' "$listing" | grep -Eq "^  $entry( |\\$)"; then
            fail "$file as $name does not list $entry: $listing"
            return
        fi
    done
    if said=$(as_login sh); then
        fail "$file as $name ran sh: $said"
        return
    fi
    if [ "$bad" != - ] && said=$(as_login "$bad"); then
        fail "$file as $name ran '$bad', outside its bounds: $said"
        return
    fi
    sudo=
    if [ "$sudoers" != - ]; then
        got=$(sudo -n -l -U "$name" 2>&1 | sed -n 's/^ *(root) //p')
        want=$(sed -n "s/^$name ALL=(root) //p" "$sudoers")
        if [ "$got" != "$want" ]; then
            fail "sudo -l -U $name lists '$got', $sudoers holds '$want'"
            return
        fi
        sudo="; sudo -l lists its $(printf '%s\\n' "$want" | grep -c .) lines and no other"
    fi
    ok "$file root:root 0755; lists $*; refuses sh${bad:+ and '$bad'}$sudo"
}
""",  # noqa: E501
    "noroot": """\
probe_no_root() {
    if ! effective=$(sshd -T -C "user=root,host=probe,addr=127.0.0.1" 2>&1); then
        fail "sshd -T: $(echo "$effective" | tail -n 1)"
        return
    fi
    if ! echo "$effective" | grep -qx 'permitrootlogin no'; then
        fail "sshd lets root in ($(echo "$effective" | grep '^permitrootlogin')): another sshd_config.d file overrides SshAccess's"
        return
    fi
    for keys in /root/.ssh/authorized_keys /etc/ssh/authorized_keys.d/root; do
        if [ -e "$keys" ]; then
            fail "$keys exists: something gave root a key the image does not declare"
            return
        fi
    done
    ok "sshd says permitrootlogin no; no authorized_keys for root"
}
""",  # noqa: E501
    "firewall-mode": """\
probe_firewall_mode() {
    if said=$(/usr/bin/mode-switch status 2>&1); then
        ok "$said"
    else
        fail "${said#mode-switch: }; journalctl -u mode-switch -u tdxs"
    fi
}
""",
    "owner": """\
probe_owner() {
    if [ ! -e "$1" ]; then
        fail "no $1 in the image"
        return
    fi
    said=$(stat -c '%U:%G %a' "$1")
    if [ "${said% *}" = "$2" ] && [ "${3:-${said#* }}" = "${said#* }" ]; then
        ok "$said"
    else
        fail "$1 is $said, declared $2${3:+ $3}: its owner was not applied, so $2 cannot use it"
    fi
}
""",
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
    again: bool = False
    """These lines are the second run's, once a held volume opened."""

    @property
    def locked(self) -> bool:
        """A check skipped because its volume waits for its key."""
        return any(r.verdict == "skip" and r.detail.startswith("locked") for r in self.results)

    @property
    def failed(self) -> tuple[Result, ...]:
        """The checks whose last line was ``fail``."""
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
        """The report as JSON: ``ok``, the ``done`` counts, each result, the missing checks."""
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
    the last ``done``, of the last run (an :data:`AGAIN` line starts one)."""
    found: dict[str, Result] = {}
    done: tuple[int, int] | None = None
    again = False
    for line in lines:
        _, sep, said = line.partition(PREFIX)
        if not sep:
            continue
        said = said.strip()
        if said.startswith(f"{AGAIN}:"):
            found, done, again = {}, None, True
            continue
        counted = _DONE.fullmatch(said)
        if counted:
            done = (int(counted[1]), int(counted[2]))
            continue
        match = _RESULT.fullmatch(said)
        if match:
            name = match[1]
            found[name] = Result(name, match[2], match[3] or "")  # type: ignore[arg-type]
    return Report(tuple(found.values()), done, tuple(expected), again)


def checks_of(manifest: Mapping[str, object]) -> tuple[str, ...]:
    """The check names a build's manifest records for its probe."""
    rows = manifest.get("probe") or []
    assert isinstance(rows, list)
    return tuple(str(row["check"]) for row in rows)


__all__ = [
    "AGAIN",
    "AGAIN_UNIT",
    "DONE",
    "DROPIN",
    "DROPIN_AGAIN",
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
