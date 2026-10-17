"""Fragments: named, reusable groups of declarations, plain or parametrised.

Teaches:

- a plain ``Fragment`` is a value: define it once, nest it in any recipe or variant;
- ``@component`` turns an annotated class into a fragment whose items ``compose()`` yields;
- ``ConfigFile.yaml``/``ConfigFile.env`` render config files from data;
- ``requires=`` names fragments the same variant must also hold (lint reports
  ``fragment-requires-missing`` otherwise); a component holding another's export
  (``HttpEndpoint``) requires that component without naming it;
- a component's ``check()`` is a lint rule that sees each resolved variant.

    tundravm inspect examples/03_fragments.py
    tundravm lint examples/03_fragments.py
    tundravm compile examples/03_fragments.py --tree build/fragments/mkosi
"""

from __future__ import annotations

from collections.abc import Iterator

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Debian,
    Declaration,
    Diagnostic,
    File,
    Fragment,
    Hook,
    Package,
    Recipe,
    Repository,
    Resolved,
    Service,
    Unit,
    Variant,
    component,
)
from tundravm.declarative.utils import Composite, ConfigFile, HttpEndpoint

SYSCTL = "kernel.kptr_restrict=2\nkernel.dmesg_restrict=1\nnet.ipv4.conf.all.rp_filter=1\n"

TIMER = "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\n\n[Install]\nWantedBy=timers.target\n"

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


@component(requires=("node-exporter",))
class Metrics(Composite):
    """Prometheus scraping the node exporter; *listen* is where its web UI and API bind.

    Fragment name: ``metrics``. Requires ``node-exporter``. Exports :attr:`health`.
    """

    listen: str = "127.0.0.1"
    port: int = 9090
    scrape_interval: str = "15s"

    @property
    def health(self) -> HttpEndpoint:
        """Prometheus's health endpoint, for a component that probes it."""
        return HttpEndpoint(self.listen, self.port, "/-/healthy", provider=self.name)

    def compose(self) -> Iterator[Declaration]:
        yield Package("prometheus")
        yield ConfigFile.yaml(
            "/etc/prometheus/prometheus.yml",
            {
                "global": {"scrape_interval": self.scrape_interval},
                "scrape_configs": [
                    {"job_name": "node", "static_configs": [{"targets": ["127.0.0.1:9100"]}]}
                ],
            },
        )
        args = {"ARGS": f"--web.listen-address={self.health.address}"}
        yield ConfigFile.env("/etc/default/prometheus", args)
        yield Unit("prometheus.service", enabled=True)

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: a cloud image must not serve metrics on a public address."""
        if self.listen.startswith("127.") or not {"azure", "gcp"} & set(resolved.outputs):
            return ()
        message = f"prometheus listens on {self.listen} in a cloud image"
        return (Diagnostic("metrics-exposed", "warning", message),)


@component
class HealthCheck(Composite):
    """A timer probing *target* once a minute; requires the fragment that exports it."""

    target: HttpEndpoint

    def compose(self) -> Iterator[Declaration]:
        yield Package("curl")
        probe = f"/usr/bin/curl -fsS -o /dev/null {self.target.url}"
        yield Service("healthcheck", probe, description="Probe the service", type="oneshot")
        yield Unit("healthcheck.timer", TIMER, enabled=True)


metrics = Metrics()  # Metrics(listen="0.0.0.0") makes lint warn for the gcp variant

recipe = Recipe(
    name="fragments",
    platform=Debian("bookworm"),
    items=(
        Package("ca-certificates"),
        hardening,
        node_exporter,
        metrics,
        HealthCheck(target=metrics.health),
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("gcp", inherits="default", outputs=("gcp",)),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
