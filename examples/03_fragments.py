"""Fragments: named, reusable groups of declarations, plain or parametrised.

Teaches:

- a plain ``Fragment`` is a value: define it once, nest it in any recipe or variant;
- a ``Composite`` turns its dataclass fields into a fragment through ``compose()``;
- ``requires=`` names fragments the same variant must also hold (lint reports
  ``fragment-requires-missing`` otherwise);
- ``checks=`` are lint rules that see each resolved variant.

    tundravm inspect examples/03_fragments.py
    tundravm lint examples/03_fragments.py
    tundravm compile examples/03_fragments.py --out build/fragments/mkosi
"""

from __future__ import annotations

from dataclasses import dataclass

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Diagnostic,
    File,
    Fragment,
    Hook,
    Package,
    Recipe,
    Repository,
    Resolved,
    Unit,
    Variant,
)
from tundravm.declarative.utils import Composite

BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

SYSCTL = "kernel.kptr_restrict=2\nkernel.dmesg_restrict=1\nnet.ipv4.conf.all.rp_filter=1\n"

# A plain fragment: security updates and kernel hardening, shared by every variant.
hardening = Fragment(
    "hardening",
    items=(
        Repository(
            "debian-security",
            "https://deb.debian.org/debian-security",
            suite="bookworm-security",
            priority=10,
        ),
        File("/etc/sysctl.d/90-hardening.conf", SYSCTL),
        Hook("ca-certificates", "postinst", "mkosi-chroot update-ca-certificates --fresh"),
    ),
)

node_exporter = Fragment(
    "node-exporter",
    items=(
        Package("prometheus-node-exporter"),
        Unit("prometheus-node-exporter.service", enabled=True),
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Metrics(Composite):
    """Prometheus scraping the node exporter; *listen* is where its web UI and API bind.

    Fragment name: ``metrics``. Requires ``node-exporter``.
    """

    listen: str = "127.0.0.1"
    port: int = 9090
    scrape_interval: str = "15s"

    def compose(self) -> Fragment:
        config = (
            f"global:\n  scrape_interval: {self.scrape_interval}\n"
            "scrape_configs:\n"
            "  - job_name: node\n"
            "    static_configs:\n"
            '      - targets: ["127.0.0.1:9100"]\n'
        )
        args = f'ARGS="--web.listen-address={self.listen}:{self.port}"\n'
        return Fragment(
            "metrics",
            requires=("node-exporter",),
            items=(
                Package("prometheus"),
                File("/etc/prometheus/prometheus.yml", config),
                File("/etc/default/prometheus", args),
                Unit("prometheus.service", enabled=True),
            ),
            checks=(self.check_exposure,),
        )

    def check_exposure(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: a cloud image must not serve metrics on a public address."""
        if self.listen.startswith("127.") or not {"azure", "gcp"} & set(resolved.targets):
            return ()
        message = f"prometheus listens on {self.listen} in a cloud image"
        return (Diagnostic("metrics-exposed", message, level="warning", variant=resolved.variant),)


recipe = Recipe(
    name="fragments",
    base="debian/bookworm",
    common=Fragment(
        "base",
        items=(
            *(Package(name) for name in (*BOOT, "ca-certificates")),
            hardening,
            node_exporter,
            Metrics(),  # Metrics(listen="0.0.0.0") makes lint warn for the gcp variant
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("gcp", parent="default", target="gcp"),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
