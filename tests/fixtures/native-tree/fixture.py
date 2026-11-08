"""Gate 1 fixture: the smallest image that exercises every node of the mkosi-native tree.

One service, one component, a Go program built from a git source and a Go and a Rust one
from local directories, the ``azure`` and ``gcp`` wrapper subimages beside the QEMU disk, an
attested TLS ingress (:data:`ATTESTED`), a tenant login (:data:`TENANT`) with its
commands that SSH reaches only in a firewall maintenance mode (:data:`FIREWALL`) and takes the
key ``attest`` hands over, root never logging in, and a ``dev`` profile that adds content: a
scheduled job, an ``Observability`` export to an in-guest
sink, a delivered secret, the two built programs run as services, and the ``box`` container
(:data:`BOX`). ``image_with(root=...)`` builds it with either root layout;
``tests/compiler/test_native_tree.py`` compiles both into ``tree-initramfs/`` and
``tree-verity/`` beside this file and asserts every file and the resolved mkosi summary.

``image`` is the ``initramfs`` layout and ``verity`` the ``verity`` one, which
``tundravm build ... --attr verity`` selects. Both boot in CI from ``qemu.raw`` through the
ESP (``tests/integration/test_fixture_boot.py``, ``test_verity_boot.py``): the production
profile fail-closed, where nothing but the kernel's and systemd's own console lines say what
the root is; the ``dev`` profile under ``tundravm run --probe``, whose derived probe prints
the rest (``web`` and ``greeter`` active and sandboxed, :data:`WEB_PORT` bound, the curated
modules, the SSH host key, a refused egress datagram, the ``tick`` timer, the exporters
running on the configuration they took, nothing waiting on them and their sinks' egress
loaded, ``web`` owning its private page, the attested endpoint, the certificate the ingress
serves, the ``box`` container running in its own root, and the root file system as
``rootfs=`` declared it) while :data:`SINK` prints what the export delivered and the
``go-probe`` and ``rust-probe`` services print the line each built program prints.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from tundravm import Debian, File, Image, Mkosi, Package, Profile, RootFS, Service
from tundravm.components import Component
from tundravm.components.access import Command, CommandParam, Login, ParamType, SshAccess
from tundravm.components.container import Container, ContainerSsh
from tundravm.components.network import Egress, Firewall, Mode, Network
from tundravm.components.observability import (
    JournalExport,
    Observability,
    PrometheusRemoteWrite,
    RecordingRule,
)
from tundravm.ports import TCP, UDP, AttestedTLS
from tundravm.secrets import Secret
from tundravm.sources import CargoBinary, Git, GoBinary, Local
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import DropIn, Every, TcpProbe

SNAPSHOT = "20261018T082309Z"


class Greeter(Component):
    """The component: the greeting a service reads, and the package it needs."""

    greeting: str = "hello from the payload"

    def compose(self) -> Iterator[object]:
        yield Package("curl")
        yield File.text("/etc/greeter/greeting", f"{self.greeting}\n")


REVIEW = GoBinary(
    Git("https://github.com/golang/review", "6c2c97b0f412fc91be975ada5e0628294794e570"),
    package="./git-codereview",
)
"""A Go program whose go.mod asks for a newer Go than trixie's 1.24: golang.org/x/review at a
commit that says ``go 1.26.0`` and requires no module. The build fetches go1.26.0 on the
host and builds offline with it."""

RUST_PROBE = CargoBinary(Local("../rust-probe"))
"""A Rust program whose rust-toolchain.toml pins 1.91.0 (trixie's Rust is 1.85) and which
needs 1.88: ``tests/fixtures/rust-probe`` beside this directory, a local source pinned by
its content digest. The build installs 1.91.0 on the host with rustup and builds offline
with it; the program prints the release of the rustc that built it."""

SINK = GoBinary(Local("../sink"))
"""A Go program from a local directory, ``tests/fixtures/sink``: the proxy and the receiver
the ``dev`` profile's :data:`OBSERVABILITY` delivers through and to, on the loopback,
printing ``sink:`` console lines for what it carries and receives. Every profile shares the
payload's builds, so the production image carries the binary and never runs it."""


class Programs(Component):
    """The built programs, in every profile's payload: the ``dev`` profile runs them."""

    def compose(self) -> Iterator[object]:
        yield REVIEW
        yield RUST_PROBE
        yield SINK


LOGS_SINK = "127.0.0.2:19580"
METRICS_SINK = "127.0.0.2:19581"
PROXY = "127.0.0.1:19582"
RULES = [
    RecordingRule(
        "local:memory_available_ratio",
        "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
        export=False,
    ),
    RecordingRule(
        "fixture:memory_available_over_10_percent", "local:memory_available_ratio > bool 0.1"
    ),
    RecordingRule("fixture:node_up", 'min(up{job="node"})'),
    RecordingRule("fixture:box_alive", 'max(tundravm_unit_active{unit="box.service"})'),
    RecordingRule(
        "local:box_cpu_ratio",
        'sum(rate(tundravm_unit_cpu_seconds_total{unit="box.service"}[1m]))'
        ' / count(node_cpu_seconds_total{mode="idle"})',
        export=False,
    ),
    RecordingRule("fixture:box_cpu_under_80_percent", "local:box_cpu_ratio < bool 0.8"),
]
OBSERVABILITY = Observability(
    logs=JournalExport(f"http://{LOGS_SINK}"),
    metrics=PrometheusRemoteWrite(
        f"http://{METRICS_SINK}/api/v1/write",
        interval="10s",
        rules=RULES,
    ),
    proxy=PROXY,
)
"""The ``dev`` profile's export path, to :data:`SINK` inside the guest through :data:`SINK`
as its proxy too: QEMU's user network reaches nothing that would receive it. The sinks are
on 127.0.0.2, which no client treats as the local host and sends around its proxy. Plain
http on the loopback, which ``observability-plaintext`` reports. The ``fixture:`` booleans
of :data:`RULES` (memory, the node exporter, the ``box`` container's liveness and CPU) and
the image's identity leave, and nothing else: the probe's ``observability:series`` row
fails on a raw series or a ``local:`` ratio at the sink."""


DEV_SLIM = (
    "/usr/lib/udev/hwdb.bin",
    "/usr/lib/udev/hwdb.d",
    "/usr/share/doc",
    "/usr/share/fonts",
    "/usr/share/info",
    "/usr/share/javascript",
    "/usr/share/locale",
    "/usr/share/man",
)
"""What the build removes to keep the ``dev`` UKI, the whole OS in the initramfs layout,
small with Prometheus, promtool and the node exporter in it: the hardware database,
documentation, translations and the web assets the loopback-only agent never serves. An
image's mkosi scripts run for every profile; the debloat already removes most of these from
the production one."""

SLIM = Mkosi(scripts=[("finalize", " ".join(["rm -rf", *(f'"$BUILDROOT{p}"' for p in DEV_SLIM)]))])


WWW = "/srv/www"
PRIVATE = f"{WWW}/private.html"
"""A page only its owner may read (``0600``), declared ``owner="web"``: httpd, running as
``web``, serves it only when the image applied that owner, which the probe's ``owner:``
check reads back: on the verity root the ``chown`` lands in the volatile overlay's tmpfs,
as any write there does."""

WEB_PORT = 8080
PAGE = "<!doctype html>\n<title>fixture</title>\n<h1>fixture</h1>\n"

ATTESTED_PORT = 8443
LOOPBACK_PORT = 8090
LOOPBACK_WEB = Service(
    "web-loopback",
    ["/usr/bin/busybox", "httpd", "-f", "-p", f"127.0.0.1:{LOOPBACK_PORT}", "-h", WWW],
    readiness=TcpProbe(LOOPBACK_PORT),
)
"""The same pages on the loopback only, the upstream :data:`ATTESTED` fronts: an ingress
must be the only path to its upstream, so ``web``'s exposed port cannot be it. :data:`BOX`
requires it as a host endpoint, so it declares readiness."""
ATTESTED = AttestedTLS(ATTESTED_PORT, upstream=LOOPBACK_WEB.endpoint(LOOPBACK_PORT))
"""Attested TLS in front of :data:`LOOPBACK_WEB`, whose certificate key the endpoint's
evidence binds: the dev probe's ``attested-tls:`` row reads the certificate back, and CI
checks it from the host with ``attest --tls`` and ``tools/cmd/attested-client``."""


BOX_PORT = 8081
BOX_WINDOW_PORT = 8082
BOX_UDP_PORT = 9999
BOX_SSH_PORT = 10022
BOX_PAGE = "<!doctype html>\n<title>box</title>\n<h1>box</h1>\n"
BOX_PROGRAM = (
    f"/usr/bin/busybox httpd -p {BOX_WINDOW_PORT} -h {WWW}; "
    f"/usr/bin/socat -u UDP-RECV:{BOX_UDP_PORT} - & "
    f"exec /usr/bin/busybox httpd -f -p {BOX_PORT} -h {WWW}"
)
BOX = Container(
    "box",
    ["/usr/bin/busybox", "sh", "-c", BOX_PROGRAM],
    packages=["busybox", "socat"],
    files=[File.text(f"{WWW}/index.html", BOX_PAGE)],
    expose=[TCP(BOX_PORT), TCP(BOX_WINDOW_PORT), UDP(BOX_UDP_PORT)],
    egress=Egress(hosts=["1.1.1.1:853"]),
    modes={"maintenance": Egress(hosts=["1.0.0.1:853"])},
    requires=[LOOPBACK_WEB.endpoint(LOOPBACK_PORT)],
    serves=[TCP(BOX_PORT)],
    probe=TcpProbe(BOX_PORT),
    ssh=ContainerSsh(port=BOX_SSH_PORT, keys=Secret("box-keys", required=False)),
)
"""The ``dev`` profile's container: busybox httpd in a Debian root of its own, built as the
``mkosi.images/box/`` subimage, serving the page shipped in that root on :data:`BOX_PORT`
(DNATed from the host) and on :data:`BOX_WINDOW_PORT`, which only the ``maintenance`` mode
opens, and socat taking datagrams on :data:`BOX_UDP_PORT`. Through its veth it reaches the
first resolver's TLS port, the second's only in maintenance, and on its loopback
:data:`LOOPBACK_WEB`, a host endpoint; the host reaches its :data:`BOX_PORT` at its veth
address. The probe's ``container:box`` rows print its root's digest, which CI compares
with the manifest's, and each of those paths. Its own sshd on :data:`BOX_SSH_PORT` lets
``box`` in with the key CI delivers; ``container-ssh:box`` reads the account and the bound
host key back."""


class Web(Component):
    """A page busybox httpd serves on :data:`WEB_PORT`, so a boot proves the network (the
    image's ``Network``) and a sandboxed service: the host fetches it through a QEMU forward.
    :data:`PRIVATE` beside it is ``web``'s own, and :data:`LOOPBACK_WEB` serves the same
    pages to :data:`ATTESTED`."""

    def compose(self) -> Iterator[object]:
        yield Package("busybox")
        yield File.text(f"{WWW}/index.html", PAGE)
        yield File.text(PRIVATE, PAGE, mode=0o600, owner="web")
        yield Service(
            "web",
            ["/usr/bin/busybox", "httpd", "-f", "-p", str(WEB_PORT), "-h", WWW],
            expose=[TCP(WEB_PORT)],
        )
        yield LOOPBACK_WEB


TOKEN = Secret("greeting-token", required=False)
"""The secret CI delivers to the dev boot over the attested endpoint; optional, so nothing
waits for it and ``run --probe`` finishes before it arrives."""

VAULT = Volume("vault", unlock=OperatorKey())
"""The ``dev`` profile's volume, owned by the operator: CI's first signed write (``attest
--identity`` handing root's key) pins that key, and ``secrets unlock`` formats the volume
with the key's token in its header and its copy inside. The probe, which never waits for
an operator, says ``skip locked ... owner none`` for it and ``ledger``; ``secrets unlock``
and ``run status`` then name the owner. Its units are the whole image's, as every volume's
are: the runtime contracts lower once for all profiles."""

VAULT_STATE = VAULT.directory("ledger")
"""What :data:`VAULT` holds: the ``ledger`` service's state directory."""


@dataclass(frozen=True, repr=False)
class Hex(ParamType):
    """A value of *digits* lower-case hex digits: the fixture's own param type."""

    digits: int

    def __repr__(self) -> str:
        return f"Hex({self.digits})"

    def usage(self) -> str:
        return f"{self.digits} hex digits"

    def check(self) -> str:
        return f'Match(value, "{self.pattern()}")'

    def pattern(self) -> str:
        return f"^[0-9a-f]{{{self.digits}}}$"


TENANT = Login(
    "tenant",
    owner=True,
    help="read the greeter",
    commands={
        "greeting": Command(runs=["/usr/bin/cat", "/etc/greeter/greeting"], help="the greeting"),
        "web": Command(
            runs=["/usr/bin/systemctl", "is-active", "web"], sudo=True, help="whether web runs"
        ),
        "logs": Command(
            params=[CommandParam("lines", type=int, min=1, max=1000)],
            runs=[
                "/usr/bin/journalctl",
                "--unit",
                "greeter.service",
                "--lines",
                "{{lines}}",
                "--no-pager",
                "--output",
                "cat",
            ],
            sudo=True,
            help="the greeter's last lines",
        ),
        "seen": Command(
            params=[CommandParam("digest", type=Hex(64), help="a sha256 in hex")],
            runs=[
                "/usr/bin/grep",
                "--count",
                "--fixed-strings",
                "{{digest}}",
                "/etc/greeter/greeting",
            ],
            sudo=True,
            help="whether the greeting holds a digest",
        ),
        "switch": Command(
            params=[CommandParam("mode", choices=["production", "maintenance"])],
            runs=["/usr/bin/mode-switch", "to", "{{mode}}"],
            sudo=True,
            help="switch the firewall mode",
        ),
    },
    home=True,
)
"""A non-root login that takes the key ``attest`` hands over, instead of root, into its
program: the greeting as itself, and as root through the sudoers lines its argvs take
``web``'s state, the greeter's last lines, whether the greeting holds a digest (a custom
:class:`Hex` param, admitted by its pattern) and a local firewall switch, which RTMR3 holds
like the endpoint's. The probe's ``login:tenant``, ``menu:tenant`` and ``ssh:root`` rows
read it back; CI logs in."""

FIREWALL = Firewall(
    modes={
        "production": Mode(),
        "maintenance": Mode(ingress=[TCP(22), TCP(BOX_WINDOW_PORT)], lasts="30m"),
    }
)
"""SSH and :data:`BOX`'s :data:`BOX_WINDOW_PORT` answer only in the ``maintenance`` mode,
which CI's ``fixture-boot`` switches to with ``run switch``, logs in as :data:`TENANT`, and
switches back from, the quote's RTMR3 holding both switches; every profile boots into
``production``, both ports closed. A login that must answer in every mode leaves 22
out of the modes."""

CONSOLE = DropIn(extra={"Service": {"StandardOutput": "journal+console"}})
"""What a service that prints for the boot tests says: its output on the serial console."""

DEV = Profile(
    add=[
        Service("tick", ["/usr/bin/true"], timer=Every("1min")),
        OBSERVABILITY,
        BOX,
        Service(
            "sink",
            [SINK, LOGS_SINK, METRICS_SINK, PROXY],
            user="root",
            hardening="relaxed",
        ),
        Service("token", ["/usr/bin/cat", TOKEN]),
        Service("go-probe", [REVIEW, "help"], unit=CONSOLE),
        Service("rust-probe", [RUST_PROBE], unit=CONSOLE),
        Service("ledger", ["/usr/bin/sleep", "infinity"], state=VAULT_STATE),
    ],
    production=False,
)
"""What the ``dev`` profile adds: a scheduled job, the export path with
its proxy and receivers, so the derived probe boots its ``egress:``, ``timer:`` and
``observability:`` checks and the console shows the ``sink:`` lines of what arrived; a
service bound to :data:`TOKEN`, so the attested endpoint on the management port gives the
probe its ``endpoint:`` check and CI its simulated ``attest`` and ``secrets deliver``, through
the tdxs issuer a development profile picks at boot (the simulator without TDX); and the two
built programs as services, whose first line reaches the console; and ``ledger`` on
:data:`VAULT`, which starts once CI unlocks it. :data:`SINK` runs as root
under the relaxed sandbox: it writes to ``/dev/console``, not to the journal its upload
would feed itself from."""


def image_with(root: Literal["initramfs", "verity"] = "initramfs") -> Image:
    """The fixture image with the payload's root filesystem in *root*."""
    return Image(
        "fixture",
        base=Debian("trixie", snapshot=SNAPSHOT),
        # the initramfs root tmpfs capped, which the dev probe reads back
        rootfs=RootFS.initramfs(max_size="4G") if root == "initramfs" else RootFS.verity(),
        components=[Greeter(), Programs(), Web(), SshAccess(login=TENANT), FIREWALL],
        services=[Service("greeter", ["/usr/bin/cat", "/etc/greeter/greeting"])],
        network=Network(egress=Egress()),
        ingress=[ATTESTED],
        targets=("qemu", "azure", "gcp"),
        profiles={"dev": DEV},
        mkosi=SLIM,
    )


image = image_with()
verity = image_with("verity")
