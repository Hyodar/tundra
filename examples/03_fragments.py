"""Components: named, reusable groups of declarations, written as classes.

Teaches:

- a ``Component`` subclass is a frozen keyword-only dataclass: its fields are
  configuration and ``compose()`` returns what it stands for;
- exports: ``Endpoint(..., provider=self)`` is a typed fact another component consumes
  without parsing this one's files, and holding it in a field requires this component;
- ``requires=`` on the class names fragments the same profile must also hold (lint
  reports ``fragment-requires-missing`` otherwise);
- ``check()`` is a lint rule that sees each resolved profile;
- ``compose()`` may still yield an internal declaration (``Repository``, ``Unit``,
  ``Task``, ``ConfigFile``, ``Run``) while those concerns have no public spelling.

    tundravm inspect examples/03_fragments.py
    tundravm lint examples/03_fragments.py
    tundravm compile examples/03_fragments.py --tree build/fragments/mkosi
"""

from __future__ import annotations

from collections.abc import Iterator

from tundravm import (
    Component,
    Diagnostic,
    Endpoint,
    Every,
    File,
    Image,
    Package,
    Resolved,
)
from tundravm.backends import InProcessBackend
from tundravm.declarative import Repository, Task, Unit
from tundravm.declarative.utils import ConfigFile, Run

SYSCTL = "kernel.kptr_restrict=2\nkernel.dmesg_restrict=1\nnet.ipv4.conf.all.rp_filter=1\n"


class Hardening(Component):
    """Security updates and a few extra sysctl knobs, on top of the implied baseline."""

    def compose(self) -> Iterator[object]:
        yield Repository(
            "debian-security",
            "https://deb.debian.org/debian-security",
            suite="trixie-security",
            priority=10,
        )
        yield File.text("/etc/sysctl.d/90-hardening.conf", SYSCTL)
        # a command run inside the image as it is built (stage="postinst" by default)
        yield Run("update-ca-certificates --fresh", name="ca-certificates")


class NodeExporter(Component):
    """Debian's Prometheus node exporter, enabled at boot."""

    def compose(self) -> Iterator[object]:
        yield Package("prometheus-node-exporter")
        yield Unit("prometheus-node-exporter.service", enabled=True)


class Metrics(Component, requires=("node-exporter",)):
    """Prometheus scraping the node exporter; *listen* is where its web UI and API bind.

    Fragment name: ``metrics``. Requires ``node-exporter``. Exports :attr:`health`.
    """

    listen: str = "127.0.0.1"
    port: int = 9090
    scrape_interval: str = "15s"

    @property
    def health(self) -> Endpoint:
        """Prometheus's health endpoint, for a component that probes it."""
        return Endpoint(self.listen, self.port, "/-/healthy", provider=self)

    def compose(self) -> Iterator[object]:
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
        yield ConfigFile.env(
            "/etc/default/prometheus", {"ARGS": f"--web.listen-address={self.health.address}"}
        )
        yield Unit("prometheus.service", enabled=True)

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: a cloud image must not serve metrics on a public address."""
        if self.listen.startswith("127.") or not {"azure", "gcp"} & set(resolved.outputs):
            return ()
        message = f"prometheus listens on {self.listen} in a cloud image"
        return (Diagnostic("metrics-exposed", "warning", message),)


class HealthCheck(Component):
    """A scheduled task probing *target* once a minute; requires the component exporting it."""

    target: Endpoint

    def compose(self) -> Iterator[object]:
        yield Package("curl")
        yield Task(
            "healthcheck",
            f"/usr/bin/curl -fsS -o /dev/null {self.target.url}",
            description="Probe the service",
            schedule=Every("1min", first="1min"),
        )


metrics = Metrics()  # Metrics(listen="0.0.0.0") makes lint warn for the cloud targets

image = Image(
    "fragments",
    packages=["ca-certificates"],
    components=[Hardening(), NodeExporter(), metrics, HealthCheck(target=metrics.health)],
    targets=("qemu", "gcp"),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
