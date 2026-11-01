"""Components: named, reusable groups of declarations, written as classes.

Teaches:

- a ``Component`` subclass is a frozen keyword-only dataclass: its fields are
  configuration and ``compose()`` returns what it stands for;
- exports: ``Endpoint(..., provider=self)`` is a typed fact another component consumes
  without parsing this one's files, and holding it in a field requires this component;
- ``requires=`` on the class names components the same profile must also hold (lint
  reports ``component-requires-missing`` otherwise);
- ``check()`` is a lint rule that sees each resolved profile;
- ``Service.packaged(unit)`` runs the unit a Debian package ships, installing the
  package, under the same sandbox a ``Service`` gets;
- ``Debian(repositories=[AptRepository(...)])`` adds an archive, pinned and signed: a
  snapshot.debian.org one moves with the image's snapshot;
- no command runs inside the image as it is built: a build step is a
  ``tundravm.sources.Build(..., command=[...], install=[...])`` with declared outputs;
- ``compose()`` may still yield an internal declaration (``Task``, ``ConfigFile``)
  while those concerns have no public spelling.

    tundravm show examples/03_components.py
    tundravm check examples/03_components.py
    tundravm build examples/03_components.py --tree-only build/components/mkosi
"""

from __future__ import annotations

from collections.abc import Iterator

from tundravm import AptRepository, Debian, File, Image, Package, Service
from tundravm.components import Component, Diagnostic, Egress, Endpoint, Network, Resolved, Rule
from tundravm.declarative import Task
from tundravm.declarative.utils import ConfigFile
from tundravm.systemd import Every

SYSCTL = "kernel.kptr_restrict=2\nkernel.dmesg_restrict=1\nnet.ipv4.conf.all.rp_filter=1\n"

# Debian's security archive at the image's own snapshot, signed by Debian's keyring.
SECURITY = AptRepository(
    "https://snapshot.debian.org/archive/debian-security",
    "trixie-security",
    key="/usr/share/keyrings/debian-archive-keyring.gpg",
)


class Hardening(Component):
    """A few extra sysctl knobs, on top of the implied baseline."""

    def compose(self) -> Iterator[object]:
        yield File.text("/etc/sysctl.d/90-hardening.conf", SYSCTL)


class NodeExporter(Component):
    """Debian's Prometheus node exporter, enabled at boot."""

    def compose(self) -> Iterator[object]:
        yield Service.packaged("prometheus-node-exporter.service")


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
        yield Service.packaged("prometheus.service")

    rules = (
        Rule(
            "metrics-exposed",
            "warning",
            "Prometheus serves metrics beyond the loopback in a cloud image.",
            "Keep Prometheus(listen='127.0.0.1'), the default.",
        ),
    )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: a cloud image must not serve metrics on a public address."""
        if self.listen.startswith("127.") or not {"azure", "gcp"} & set(resolved.targets):
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
    "components",
    base=Debian(repositories=[SECURITY]),
    packages=["ca-certificates"],
    components=[Hardening(), NodeExporter(), metrics, HealthCheck(target=metrics.health)],
    # Outbound: only what the baseline sends (NTS time, DNS, cloud metadata).
    network=Network(egress=Egress()),
    targets=("qemu", "gcp"),
)
