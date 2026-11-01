"""Observability: the authenticated export path for a VM with no console and a volatile
journal (plan section 6.4).

``Observability(logs=JournalExport(url, credential=...), metrics=PrometheusRemoteWrite(
url, credential=...))`` ships the journal with ``systemd-journal-upload`` and node metrics
with Prometheus in agent mode (remote write only, no query API). Both run as their own
users in the preset sandbox, read their credentials as systemd credentials, keep bounded
buffers, retry on their own, and are required by nothing, so an exporter that cannot
reach its sink never holds an application back. ``proxy="IP:PORT"`` sends both through
one fixed-address proxy, the only destination a filtered egress then needs.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

from tundravm._image._component import Component
from tundravm._image._secrets import Secret
from tundravm._image._service import Service
from tundravm._image._values import Egress, Package, egress_host
from tundravm.declarative import model as dm
from tundravm.declarative.config import ConfigFile
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

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
    try:
        named = bool(urlsplit(url).hostname) and urlsplit(url).port != 0
    except ValueError:
        named = False
    if not named:
        raise ValidationError(
            f"{kind}: url {url!r} names no host, or a port outside 1..65535.",
            hint="Pass the receiver's URL: 'https://logs.example:19532'.",
        )


def _destination(url: str) -> str:
    """*url*'s ``HOST:PORT``, the port its scheme implies when it names none."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _proxy_url(proxy: str | None) -> str | None:
    """The ``http://`` URL the exporters reach *proxy* (``IP:PORT``) at."""
    return None if proxy is None else f"http://{proxy}"


def _check_proxy(owner: object, proxy: object) -> None:
    hint = "Pass proxy='10.0.0.9:3128', the proxy's address and port: '[fd00::9]:3128' for IPv6."
    try:
        _, port, named = egress_host(owner, proxy)
    except ValidationError:
        port, named = None, True
    if port is None or named:
        raise ValidationError(
            f"Observability: proxy {proxy!r} is not IP:PORT; the firewall matches the "
            "proxy by its address.",
            hint=hint,
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

    *credential* is a :class:`~tundravm.secrets.Secret` holding the client's PEM key and
    certificate, which the receiver authenticates; without it the upload is anonymous.
    The upload's cursor lives in its state directory, so a restart resumes rather than
    resends, and journald's volatile, capped storage is the buffer.
    """

    url: str
    credential: Secret | None = None

    def __post_init__(self) -> None:
        _check_url(self, self.url)
        _check_credential(self, self.credential)

    def service(self, proxy: str | None = None) -> Service:
        """The ``journal-export`` service, through *proxy* (``IP:PORT``) when given."""
        argv = [
            "/usr/lib/systemd/systemd-journal-upload",
            f"--url={self.url}",
            "--save-state=/var/lib/journal-export/state",
        ]
        if self.credential is not None:
            argv += [f"--key={self.credential.path}", f"--cert={self.credential.path}"]
        url = _proxy_url(proxy)
        return Service(
            "journal-export",
            argv,
            credentials=[] if self.credential is None else [self.credential],
            state="journal-export",
            groups=["systemd-journal"],
            environment={} if url is None else {"https_proxy": url, "http_proxy": url},
        )


@dataclass(frozen=True, slots=True)
class PrometheusRemoteWrite:
    """Send node metrics to *url*, a Prometheus remote-write receiver.

    *credential* is a :class:`~tundravm.secrets.Secret` holding a bearer token. The agent
    scrapes the node exporter on the loopback every *interval* and keeps at most *buffer*
    of samples it could not send; in memory, at most four shards of 10000 samples wait,
    retried from one second to one minute apart, a 429 included.
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

    def config(self, proxy: str | None = None) -> dict[str, object]:
        """The agent's configuration: scrape the node exporter, write to *url*, through
        *proxy* (``IP:PORT``) when given."""
        remote: dict[str, object] = {"url": self.url}
        url = _proxy_url(proxy)
        if url is not None:
            remote["proxy_url"] = url
        if self.credential is not None:
            remote["authorization"] = {
                "type": "Bearer",
                "credentials_file": f"/run/credentials/metrics-export.service/"
                f"{self.credential.name}",
            }
        remote["queue_config"] = {
            "capacity": 10000,
            "max_shards": 4,
            "min_backoff": "1s",
            "max_backoff": "1m",
            "retry_on_http_429": True,
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
    :meth:`outbound` is each sink's host and port (a host name stands for its port).

    *proxy* (``"IP:PORT"``, ``"[IPv6]:PORT"``) is an HTTP CONNECT proxy at a fixed address
    through which both reach their sinks: :meth:`outbound` is then the proxy alone, so a
    filtered egress opens one address instead of a port to any. The TLS session to each
    sink runs end to end inside the tunnel, with the sink's name as its SNI and its
    certificate checked, so the proxy can allow by name what the firewall can only allow by
    address, and the guest resolves no sink. ``systemd-journal-upload`` reads it from
    ``https_proxy``, Prometheus from ``proxy_url``. A proxy that itself speaks TLS is not
    supported: neither exporter can be given its name apart from its URL, which would then
    have to resolve to the fixed address through an ``/etc/hosts`` entry the image does
    not write.

    Exports :meth:`service` refs ``journal-export`` and ``metrics-export``. Component name:
    ``observability``.
    """

    logs: JournalExport | None = None
    metrics: PrometheusRemoteWrite | None = None
    proxy: str | None = None

    def __post_init__(self) -> None:
        if self.proxy is not None:
            _check_proxy(self, self.proxy)
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

    def outbound(self) -> Egress:
        """*proxy*, or each sink's ``HOST:PORT`` without one."""
        if self.proxy is not None:
            return Egress(hosts=[self.proxy])
        sinks = (sink for sink in (self.logs, self.metrics) if sink is not None)
        return Egress(hosts=list(dict.fromkeys(_destination(sink.url) for sink in sinks)))

    def compose(self) -> Iterator[object]:
        if self.logs is not None:
            yield Package("systemd-journal-remote")
            # The packaged uploader reads /etc; ours runs from its own unit and arguments.
            yield dm.Unit("systemd-journal-upload.service", masked=True)
            yield self.logs.service(self.proxy)
        if self.metrics is not None:
            yield Package("prometheus")
            yield Package("prometheus-node-exporter")
            # Debian's prometheus.service would serve a query API on every interface.
            yield dm.Unit("prometheus.service", masked=True)
            config = self.metrics.config(self.proxy)
            yield ConfigFile.yaml(METRICS_CONFIG, config, sort_keys=False)
            yield ConfigFile.env(
                _NODE_EXPORTER_DEFAULTS, {"ARGS": "--web.listen-address=127.0.0.1:9100"}
            )
            yield dm.Unit("prometheus-node-exporter.service", enabled=True)
            yield self.metrics.service()

    rules = (
        Rule(
            "observability-plaintext",
            "warning",
            "An Observability sink is http://, so the host and every hop read what the guest logs.",
            "Point the sink at an https:// URL.",
        ),
    )

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
