"""Gate 1 fixture: the smallest recipe that exercises every node of the mkosi-native tree.

One service, one component, a Go program built from source, a ``dev`` variant that adds
content (a profile overlay with SSH access by public key only) and two variants that only
add outputs (the ``azure`` and ``gcp`` wrapper subimages). ``recipe_with(root=...)``
builds it with either root layout; ``tests/compiler/test_native_tree.py`` compiles both
into ``tree-initramfs/`` and ``tree-verity/`` beside this file and asserts every file and
the resolved mkosi summary.

``recipe`` is the ``initramfs`` layout and ``verity_recipe`` the ``verity`` one, which
``tundravm build ... --attr verity_recipe`` selects. Both boot in CI from ``qemu.raw``
through the ESP (``tests/integration/test_fixture_boot.py``, ``test_verity_boot.py``):
``Web`` serves a page the host fetches, and ``BootProbe`` prints the command line, what
the root is and the usage line of the Go program on the console.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from tundravm.declarative import (
    Debian,
    Declaration,
    File,
    Git,
    Hardening,
    Mkosi,
    Package,
    Recipe,
    Root,
    Service,
    Task,
    Variant,
    component,
)
from tundravm.declarative.utils import Composite, GoBinary, Network, SshAccess

SNAPSHOT = "20251113T083151Z"
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


PROBE = "/usr/lib/tundravm/boot-probe"
PROBE_SCRIPT = f"""\
#!/bin/sh
# What the boot tests assert, one "boot-probe:" line each: the command line, the mounts
# stacked on / (fstype, source, options), each device-mapper device, where a write to
# /usr/bin lands, and the first line the Go program prints.
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
say "go $({REVIEW.executable} help | head -n 1)"
say done
"""
"""The probe ``BootProbe`` installs; ``/bin/sh``, coreutils and :data:`REVIEW`."""

WWW = "/srv/www"
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


@component
class Web(Composite):
    """A page busybox httpd serves on :data:`WEB_PORT`, so a boot proves the network (the
    recipe's ``Network``) and a sandboxed service: the host fetches it through a QEMU forward."""

    def compose(self) -> Iterator[Declaration]:
        yield Package("busybox")
        yield File(f"{WWW}/index.html", PAGE)
        yield Service(
            "web",
            ("/usr/bin/busybox", "httpd", "-f", "-p", str(WEB_PORT), "-h", WWW),
            ports=(WEB_PORT,),
        )


def recipe_with(root: Literal["initramfs", "verity"] = "initramfs") -> Recipe:
    """The fixture recipe with the payload's root filesystem in *root*."""
    return Recipe(
        name="fixture",
        platform=Debian("trixie", snapshot=SNAPSHOT),
        mkosi=Mkosi(seed=SEED, root=root),
        items=(
            Greeter(),
            Service("greeter", ("/usr/bin/cat", "/etc/greeter/greeting")),
            Network(ipv6=False),
            Web(),
            REVIEW,
            BootProbe(),
        ),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",)),
            Variant("gcp", outputs=("gcp",)),
            Variant("dev", add=SshAccess(authorized_keys=(DEV_KEY,)), release=False),
        ),
    )


recipe = recipe_with()
verity_recipe = recipe_with("verity")
