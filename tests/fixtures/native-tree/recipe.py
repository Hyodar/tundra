"""Gate 1 fixture: the smallest recipe that exercises every node of the mkosi-native tree.

One service, one component, a Go program built from a git source and a Go and a Rust one
from local directories, a ``dev`` variant that adds content (a profile overlay with SSH
access by public key only and an ``Observability`` export to an in-guest sink) and two
variants that only add outputs (the ``azure`` and ``gcp`` wrapper subimages).
``recipe_with(root=...)`` builds it with either root layout;
``tests/compiler/test_native_tree.py`` compiles both into ``tree-initramfs/`` and
``tree-verity/`` beside this file and asserts every file and the resolved mkosi summary.

``recipe`` is the ``initramfs`` layout and ``verity_recipe`` the ``verity`` one, which
``tundravm build ... --attr verity_recipe`` selects. Both boot in CI from ``qemu.raw``
through the ESP (``tests/integration/test_fixture_boot.py``, ``test_verity_boot.py``):
``Web`` serves a page the host fetches, and ``BootProbe`` prints the command line, what
the root is and the first line each built program prints on the console. ``BootProbe`` is
the half the derived boot probe cannot be: it runs in the production profile, which carries
no ``tundravm-probe.service``, and it reads what no declaration states (the root's layers,
the built programs). The ``dev`` profile's derived probe checks the rest (``web`` and
``greeter`` active and sandboxed, :data:`WEB_PORT` bound, the curated modules, the SSH host
key, a refused egress datagram, the ``tick`` timer, the exporters running on the
configuration they took, nothing waiting on them and their sinks' egress loaded, ``web``
owning its private page) and CI's
``fixture-boot`` job runs it with ``tundravm run --probe``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import tundravm
from tundravm import Image
from tundravm._image import recipe_of
from tundravm._image._lower import OWNERS_STEP
from tundravm._image._runtime import Runtime
from tundravm._modules import attested_tls
from tundravm.components import Endpoint, JournalExport, Observability, PrometheusRemoteWrite
from tundravm.components._base import egress_rules
from tundravm.declarative import (
    BootStep,
    Debian,
    Declaration,
    Every,
    File,
    Firewall,
    FirewallMode,
    Fragment,
    Git,
    Hardening,
    Hook,
    Local,
    Mkosi,
    Package,
    Port,
    Recipe,
    Root,
    Secret,
    SecretFile,
    Secrets,
    Service,
    Task,
    Variant,
    component,
)
from tundravm.declarative.utils import (
    CargoBinary,
    Composite,
    GoBinary,
    Login,
    Network,
    SshAccess,
    Sudo,
    Tdxs,
)
from tundravm.ports import AttestedTLS
from tundravm.secrets import Secret as Delivered

SNAPSHOT = "20261018T082309Z"
SEED = "6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70"
DEV_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureFixtureFixtureFixtureFixtureFixture dev@fixture"
)


@component
class Greeter(Composite):
    """The component: the greeting a service reads, and the package it needs."""

    greeting: str = "hello from the payload"

    def compose(self) -> Iterator[Declaration]:
        yield Package("curl")
        yield File("/etc/greeter/greeting", f"{self.greeting}\n")


REVIEW = GoBinary(
    "git-codereview",
    Git("https://github.com/golang/review", "6c2c97b0f412fc91be975ada5e0628294794e570"),
    package="./git-codereview",
)
"""A Go program whose go.mod asks for a newer Go than trixie's 1.24: golang.org/x/review at a
commit that says ``go 1.26.0`` and requires no module. The build fetches go1.26.0 on the
host and builds offline with it."""

RUST_PROBE = CargoBinary("rust-probe", Local("../rust-probe"))
"""A Rust program whose rust-toolchain.toml pins 1.91.0 (trixie's Rust is 1.85) and which
needs 1.88: ``tests/fixtures/rust-probe`` beside this directory, a local source pinned by
its content digest. The build installs 1.91.0 on the host with rustup and builds offline
with it; the program prints the release of the rustc that built it."""

SINK = GoBinary("sink", Local("../sink"))
"""A Go program from a local directory, ``tests/fixtures/sink``: the proxy and the receiver
the ``dev`` profile's :data:`OBSERVABILITY` delivers through and to, on the loopback,
printing ``sink:`` console lines for what it carries and receives. Every profile shares the
payload's builds, so the production image carries the binary and never runs it."""

LOGS_SINK = "127.0.0.2:19580"
METRICS_SINK = "127.0.0.2:19581"
PROXY = "127.0.0.1:19582"
OBSERVABILITY = Observability(
    logs=JournalExport(f"http://{LOGS_SINK}"),
    metrics=PrometheusRemoteWrite(f"http://{METRICS_SINK}/api/v1/write", interval="10s"),
    proxy=PROXY,
)
"""The ``dev`` profile's export path, to :data:`SINK` inside the guest through :data:`SINK`
as its proxy too: QEMU's user network reaches nothing that would receive it. The sinks are
on 127.0.0.2, which no client treats as the local host and sends around its proxy. Plain
http on the loopback, which ``observability-plaintext`` reports."""


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
"""What the ``dev`` profile removes to keep its UKI, the whole OS in the initramfs layout,
small with Prometheus, promtool and the node exporter in it: the hardware database,
documentation, translations and the web assets the loopback-only agent never serves."""


def lowered(observability: Observability) -> Fragment:
    """*observability* as the fragment an ``Image`` lowers it to: the units, configuration
    and packages its exporters run with."""
    items = recipe_of(Image("fixture", observability=observability)).items
    return next(i for i in items if isinstance(i, Fragment) and i.name == observability.name)


WWW = "/srv/www"
PRIVATE = f"{WWW}/private.html"
"""A page only its owner may read (``0600``), declared ``owner="web"``: httpd, running as
``web``, serves it only when the image applied that owner, which the probe's ``owner:``
check and ``BootProbe`` read back: on the verity root the ``chown`` lands in the
volatile overlay's tmpfs, as any write there does."""


PROBE = "/usr/lib/tundravm/boot-probe"
PROBE_SCRIPT = f"""\
#!/bin/sh
# What the boot tests assert, one "boot-probe:" line each: the command line, the mounts
# stacked on / (fstype, source, options), each device-mapper device, where a write to
# /usr/bin lands, the owner and mode of the web service's private page, and the first line
# the Go and the Rust program print.
say() {{ echo "boot-probe: $*"; }}
say "cmdline $(cat /proc/cmdline)"
while read -r line; do
    set -- $line
    [ "$5" = / ] && say "mount / ${{line#* - }}"
done </proc/self/mountinfo
for dm in /sys/block/dm-*; do
    [ -e "$dm/dm/name" ] || continue
    say "dm $(cat "$dm/dm/name") ${{dm##*/}} uuid=$(cat "$dm/dm/uuid") ro=$(cat "$dm/ro")"
done
if touch /usr/bin/.boot-probe 2>/dev/null; then
    say "usr-bin write=accepted"
    rm -f /usr/bin/.boot-probe
else
    say "usr-bin write=refused"
fi
say "owner {PRIVATE} $(stat -c '%U:%G %a' {PRIVATE})"
say "go $({REVIEW.executable} help | head -n 1)"
say "rust $({RUST_PROBE.executable})"
say done
"""
"""The probe ``BootProbe`` installs; ``/bin/sh``, coreutils, :data:`REVIEW` and
:data:`RUST_PROBE`."""

WEB_PORT = 8080
PAGE = "<!doctype html>\n<title>fixture</title>\n<h1>fixture</h1>\n"


@component
class BootProbe(Composite):
    """A boot task printing what the root is: root plumbing, unsandboxed, so it sees the
    real mounts and devices."""

    def compose(self) -> Iterator[Declaration]:
        yield File(PROBE, PROBE_SCRIPT, mode=0o755)
        yield Task(
            "boot-probe",
            ("/bin/sh", PROBE),
            description="Print where the root file system comes from",
            user=Root(),
            hardening=Hardening.none(),
        )


def owned(file: tundravm.File) -> tuple[Declaration, ...]:
    """*file* as an ``Image`` lowers it: the file, and the boot step that applies its owner."""
    items = recipe_of(Image("fixture", files=[file])).items
    return tuple(
        item
        for item in items
        if (isinstance(item, File) and item.path == file.path)
        or (isinstance(item, BootStep) and item.name == OWNERS_STEP)
    )


@component
class Web(Composite):
    """A page busybox httpd serves on :data:`WEB_PORT`, so a boot proves the network (the
    recipe's ``Network``) and a sandboxed service: the host fetches it through a QEMU forward.
    :data:`PRIVATE` beside it is ``web``'s own."""

    def compose(self) -> Iterator[Declaration]:
        yield Package("busybox")
        yield File(f"{WWW}/index.html", PAGE)
        yield from owned(tundravm.File.text(PRIVATE, PAGE, mode=0o600, owner="web"))
        yield Service(
            "web",
            ("/usr/bin/busybox", "httpd", "-f", "-p", str(WEB_PORT), "-h", WWW),
            ports=(WEB_PORT,),
        )


MANAGEMENT_PORT = 8745
DEV_SECRET = Secret(
    "greeting-token",
    (SecretFile("/run/tundravm/credentials/greeting-token"),),
    required=False,
)
"""The secret CI delivers to the dev boot over the attested endpoint; optional, so nothing
waits for it and ``run --probe`` finishes before it arrives."""

TENANT = Login(
    "tenant",
    keys=Delivered("tenant-keys", required=False),
    command="/bin/sh -c 'cat /etc/greeter/greeting; /usr/bin/sudo -n /usr/bin/systemctl "
    "is-active web'",
    sudo=[Sudo("/usr/bin/systemctl is-active web")],
    home=True,
)
"""A non-root login whose key CI delivers after attestation: whatever the client asks, it
prints the greeting and runs its one sudo rule. The probe's ``login:tenant`` row reads it
back."""
TENANT_KEYS = Secret("tenant-keys", (SecretFile(TENANT.key_file, mode=0o644),), required=False)
""":data:`TENANT`'s keys as the attested endpoint writes them (an ``Image`` adds this
target itself)."""

ATTESTED_PORT = 8443
ATTESTED = AttestedTLS(ATTESTED_PORT, upstream=Endpoint("127.0.0.1", WEB_PORT))
"""Attested TLS in front of :class:`Web`'s page, whose certificate key the endpoint's
evidence binds: the probe's ``attested-tls:`` row reads the certificate back, and CI checks
it from the host with ``attest --tls``."""


def relayed(way: AttestedTLS) -> tuple[Declaration, ...]:
    """*way* as an ``Image`` lowers an attested ingress: the key unit's script, the key
    unit, the relay and their packages."""
    runtime = Runtime(
        volumes=(),
        secrets=(),
        ingress=(way,),
        services=(),
        tools=tundravm.components.Tdxs().source,
        rootfs="initramfs",
    )
    return (File(attested_tls.SCRIPT_PATH, attested_tls.SCRIPT, mode=0o755), *runtime.relay(way))


MODES = (FirewallMode("production"), FirewallMode("maintenance", ingress=(Port(22),), lasts=1800))
"""The dev firewall's modes: SSH answers only in ``maintenance``, which CI's ``fixture-boot``
switches to with ``run switch``, logs in, and switches back from, the quote's RTMR3 holding
both switches."""


DEV = Fragment(
    "dev",
    items=(
        SshAccess(authorized_keys=(DEV_KEY,), logins=(TENANT,)),
        Firewall(
            egress="filter",
            egress_rules=egress_rules(((OBSERVABILITY.name, OBSERVABILITY.outbound()),)),
            modes=MODES,
        ),
        Task(
            "tick", "/usr/bin/true", description="A trivial scheduled job", schedule=Every("1min")
        ),
        lowered(OBSERVABILITY),
        Hook("dev-slim", "finalize", " ".join(["rm -rf", *(f'"$BUILDROOT{p}"' for p in DEV_SLIM)])),
        Service(
            "sink",
            (SINK.executable, LOGS_SINK, METRICS_SINK, PROXY),
            description="Receive the observability export",
            user=Root(),
            hardening=Hardening.none(),
        ),
        Tdxs(),
        Secrets(
            entries=(DEV_SECRET, TENANT_KEYS),
            port=MANAGEMENT_PORT,
            attested_tls=(ATTESTED_PORT,),
        ),
        *relayed(ATTESTED),
    ),
)
"""What the ``dev`` variant adds: SSH by public key for root and :data:`TENANT` in the
:data:`MODES` maintenance window, a firewall whose output chain allows nothing beyond DHCP
and what :data:`OBSERVABILITY`'s proxy asks for (the fixture fetches nothing once built), a
scheduled job, and the export path with its proxy and receivers, so the derived probe boots
its ``egress:``, ``timer:`` and ``observability:`` checks, and the console shows the
``sink:`` lines of what arrived. The evidence service with the simulator issuer and the
attested endpoint on :data:`MANAGEMENT_PORT` taking :data:`DEV_SECRET` give the probe its
``endpoint:`` check and CI its simulated ``attest`` and ``secrets deliver``
(:data:`TENANT_KEYS` among them, then an SSH login as :data:`TENANT`); :data:`ATTESTED` in
front of the page gives it the ``attested-tls:`` check and CI its ``attest --tls``.
:data:`SINK` runs as root, unsandboxed, to write to ``/dev/console``. No ``Volume``: ``run``
attaches disks only for an ``Image``'s runtime manifest, which this plain ``Recipe`` has
none of."""


def recipe_with(root: Literal["initramfs", "verity"] = "initramfs") -> Recipe:
    """The fixture recipe with the payload's root filesystem in *root*."""
    return Recipe(
        name="fixture",
        platform=Debian("trixie", snapshot=SNAPSHOT),
        # the initramfs root tmpfs capped, which the boot test reads back from the probe
        mkosi=Mkosi(seed=SEED, root=root, root_size="4G" if root == "initramfs" else None),
        items=(
            Greeter(),
            Service("greeter", ("/usr/bin/cat", "/etc/greeter/greeting")),
            Network(ipv6=False),
            Web(),
            REVIEW,
            RUST_PROBE,
            SINK,
            BootProbe(),
            # what the dev profile's tools/ builds need, declared where every profile builds
            Package("git", role="build"),
            Package("build-essential", role="build"),
        ),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",)),
            Variant("gcp", outputs=("gcp",)),
            Variant("dev", add=DEV, release=False),
        ),
    )


recipe = recipe_with()
verity_recipe = recipe_with("verity")
