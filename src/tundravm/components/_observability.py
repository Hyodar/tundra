"""Observability: the authenticated export path for a VM with no console and a volatile
journal (plan section 6.4).

``Observability(logs=JournalExport(url, credential=...), metrics=PrometheusRemoteWrite(
url, credential=...))`` ships the journal with ``systemd-journal-upload`` and node metrics
with Prometheus in agent mode (remote write only, no query API). Both run as their own
users in the preset sandbox, read their credentials as systemd credentials, keep bounded
buffers, retry on their own, and are required by nothing, so an exporter that cannot
reach its sink never holds an application back.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from tundravm.declarative import model as dm
from tundravm.declarative.config import ConfigFile
from tundravm.errors import ValidationError
from tundravm.image._component import Component
from tundravm.image._secrets import Secret
from tundravm.image._service import Service
from tundravm.image._values import Package

__all__ = ["JournalExport", "Observability", "PrometheusRemoteWrite"]

METRICS_CONFIG = "/etc/tundravm/metrics-export.yml"
"""The Prometheus agent's configuration."""

_NODE_EXPORTER_DEFAULTS = "/etc/default/prometheus-node-exporter"


def _check_url(owner: object, url: object) -> None:
    kind = type(owner).__name__
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValidationError(
            f"{kind}: url {url!r} must be an https:// URL.",
            hint="Pass the receiver's URL: 'https://logs.example:19532'.",
        )
    if any(c.isspace() or c in "'\"" for c in url):
        raise ValidationError(
            f"{kind}: url {url!r} must be one word without quotes.",
            hint="Pass the URL alone, without spaces or quotes.",
        )


def _check_credential(owner: object, credential: object) -> None:
    if credential is not None and not isinstance(credential, Secret):
        raise ValidationError(
            f"{type(owner).__name__}: credential {credential!r} is not a Secret.",
            hint="Pass credential=Secret('logs-key'): it is delivered, never built in.",
        )


@dataclass(frozen=True, slots=True)
class JournalExport:
    """Ship the journal to *url* (a ``systemd-journal-remote`` receiver) over HTTPS.

    *credential* is a :class:`~tundravm.image.Secret` holding the client's PEM key and
    certificate, which the receiver authenticates; without it the upload is anonymous.
    The upload's cursor lives in its state directory, so a restart resumes rather than
    resends, and journald's volatile, capped storage is the buffer.
    """

    url: str
    credential: Secret | None = None

    def __post_init__(self) -> None:
        _check_url(self, self.url)
        _check_credential(self, self.credential)

    def service(self) -> Service:
        """The ``journal-export`` service."""
        argv = [
            "/usr/lib/systemd/systemd-journal-upload",
            f"--url={self.url}",
            "--save-state=/var/lib/journal-export/state",
        ]
        if self.credential is not None:
            argv += [f"--key={self.credential.path}", f"--cert={self.credential.path}"]
        return Service(
            "journal-export",
            argv,
            credentials=[] if self.credential is None else [self.credential],
            state="journal-export",
            groups=["systemd-journal"],
        )


@dataclass(frozen=True, slots=True)
class PrometheusRemoteWrite:
    """Send node metrics to *url*, a Prometheus remote-write receiver.

    *credential* is a :class:`~tundravm.image.Secret` holding a bearer token. The agent
    scrapes the node exporter on the loopback every *interval* and keeps at most *buffer*
    of samples it could not send.
    """

    url: str
    credential: Secret | None = None
    interval: str = "30s"
    buffer: str = "1h"

    def __post_init__(self) -> None:
        _check_url(self, self.url)
        _check_credential(self, self.credential)
        for what in ("interval", "buffer"):
            value = getattr(self, what)
            if not isinstance(value, str) or not value[:-1].isdigit() or value[-1] not in "smh":
                raise ValidationError(
                    f"PrometheusRemoteWrite: {what} {value!r} is not a duration such as '30s'.",
                    hint="Use a number with s, m or h: '30s', '1h'.",
                )

    def config(self) -> dict[str, object]:
        """The agent's configuration: scrape the node exporter, write to *url*."""
        remote: dict[str, object] = {"url": self.url}
        if self.credential is not None:
            remote["authorization"] = {
                "type": "Bearer",
                "credentials_file": f"/run/credentials/metrics-export.service/"
                f"{self.credential.name}",
            }
        return {
            "global": {"scrape_interval": self.interval},
            "scrape_configs": [
                {"job_name": "node", "static_configs": [{"targets": ["127.0.0.1:9100"]}]}
            ],
            "remote_write": [remote],
        }

    def service(self) -> Service:
        """The ``metrics-export`` service: Prometheus in agent mode."""
        return Service(
            "metrics-export",
            [
                "/usr/bin/prometheus",
                "--enable-feature=agent",
                f"--config.file={METRICS_CONFIG}",
                "--storage.agent.path=/var/lib/metrics-export",
                f"--storage.agent.retention.max-time={self.buffer}",
                "--web.listen-address=127.0.0.1:9090",
            ],
            credentials=[] if self.credential is None else [self.credential],
            state="metrics-export",
        )


class Observability(Component):
    """The export path for logs and metrics: ``image.observability`` takes it.

    *logs* is a :class:`JournalExport`, *metrics* a :class:`PrometheusRemoteWrite`; each
    is optional. Their credentials are secrets the image declares in ``secrets=``.
    Exports :meth:`service` refs ``journal-export`` and ``metrics-export``. Fragment name:
    ``observability``.
    """

    logs: JournalExport | None = None
    metrics: PrometheusRemoteWrite | None = None

    def __post_init__(self) -> None:
        if self.logs is not None and not isinstance(self.logs, JournalExport):
            raise ValidationError(
                f"Observability: logs= holds {self.logs!r}, not JournalExport.",
                hint="Pass logs=JournalExport(url, credential=...).",
            )
        if self.metrics is not None and not isinstance(self.metrics, PrometheusRemoteWrite):
            raise ValidationError(
                f"Observability: metrics= holds {self.metrics!r}, not PrometheusRemoteWrite.",
                hint="Pass metrics=PrometheusRemoteWrite(url, credential=...).",
            )
        if self.logs is None and self.metrics is None:
            raise ValidationError(
                "Observability exports nothing.",
                hint="Pass logs=JournalExport(url) and/or metrics=PrometheusRemoteWrite(url).",
            )

    def compose(self) -> Iterator[object]:
        if self.logs is not None:
            yield Package("systemd-journal-remote")
            # The packaged uploader reads /etc; ours runs from its own unit and arguments.
            yield dm.Unit("systemd-journal-upload.service", masked=True)
            yield self.logs.service()
        if self.metrics is not None:
            yield Package("prometheus")
            yield Package("prometheus-node-exporter")
            # Debian's prometheus.service would serve a query API on every interface.
            yield dm.Unit("prometheus.service", masked=True)
            yield ConfigFile.yaml(METRICS_CONFIG, self.metrics.config(), sort_keys=False)
            yield ConfigFile.env(
                _NODE_EXPORTER_DEFAULTS, {"ARGS": "--web.listen-address=127.0.0.1:9100"}
            )
            yield dm.Unit("prometheus-node-exporter.service", enabled=True)
            yield self.metrics.service()

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``observability-plaintext``: an http:// sink sees what the TD logs in the clear."""
        found: list[dm.Diagnostic] = []
        for name, sink in (("logs", self.logs), ("metrics", self.metrics)):
            if sink is not None and sink.url.startswith("http://"):
                found.append(
                    dm.Diagnostic(
                        "observability-plaintext",
                        "warning",
                        f"Observability {name} go to {sink.url} in the clear: the host and "
                        "every hop read them",
                    )
                )
        return tuple(found)
