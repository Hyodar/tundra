"""Observability: the authenticated export path for a VM with no console and a volatile
journal (plan section 6.4).

``Observability(logs=JournalExport(url, credential=...), metrics=PrometheusRemoteWrite(
url, credential=...))`` ships the journal with ``systemd-journal-upload`` and node metrics
with Prometheus in agent mode (remote write only, no query API). Both run as their own
users in the preset sandbox, read their credentials as systemd credentials, keep bounded
buffers, retry on their own, and are required by nothing, so an exporter that cannot
reach its sink never holds an application back. A development profile ships both to
receivers in the guest, and so does either without a ``url``: a dev boot shows the export
working, and nothing leaves the VM. The metrics always carry the image's identity, which
``image-metrics`` (``tools/cmd/image-metrics``) writes for the node exporter. ``rules=`` computes
:class:`RecordingRule` series in the guest, so the image can ship derived booleans in place
of raw series; Prometheus then runs in server mode, which evaluates rules and agent mode
does not. ``proxy="IP:PORT"`` sends both through one fixed-address proxy, the only
destination a filtered egress then needs, and ``proxy=Proxy(address, port, tls_name=...)``
through one that speaks TLS itself.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator, Sequence
from dataclasses import KW_ONLY, dataclass, replace
from urllib.parse import urlsplit

from tundravm._image._component import Component
from tundravm._image._metrics import Selector, UnitMetrics, VolumeMetrics
from tundravm._image._secrets import Secret
from tundravm._image._service import Service
from tundravm._image._values import Egress, File, Package, TcpProbe, egress_host
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.declarative.config import ConfigFile
from tundravm.declarative.systemd import Hardening, Root
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError
from tundravm.models import HostsEntry

from .._keywords import Keywords

__all__ = [
    "EXPORT_PRESETS",
    "JournalExport",
    "Observability",
    "PrometheusRemoteWrite",
    "Proxy",
    "RecordingRule",
    "Selector",
    "SigV4",
    "UnitMetrics",
    "VolumeMetrics",
]

LOGS_SERVICE = "journal-export"
"""The journal uploader's service."""

METRICS_SERVICE = "metrics-export"
"""The Prometheus agent's service."""

METRICS_CONFIG = "/etc/tundravm/metrics-export.yml"
"""The Prometheus agent's configuration."""

METRICS_RULES = "/etc/tundravm/metrics-rules.yml"
"""The recording rules Prometheus evaluates when ``rules=`` declares some."""

SINK_SERVICE = "journal-sink"
"""The in-guest receiver a :class:`JournalExport` without a URL uploads to."""

SINK_PORT = 19532
"""The loopback port :data:`SINK_SERVICE` listens on: ``systemd-journal-remote``'s own."""

SINK_URL = f"http://127.0.0.1:{SINK_PORT}"
"""Where the uploader reaches the in-guest receiver."""

METRICS_SINK_SERVICE = "metrics-sink"
"""The in-guest receiver a :class:`PrometheusRemoteWrite` without a URL writes to."""

METRICS_SINK_PORT = 9091
"""The loopback port :data:`METRICS_SINK_SERVICE` listens on, beside the agent's 9090."""

METRICS_SINK_URL = f"http://127.0.0.1:{METRICS_SINK_PORT}/api/v1/write"
"""Where the agent reaches the in-guest receiver."""

METRICS_SINK_CONFIG = "/etc/tundravm/metrics-sink.yml"
"""The in-guest receiver's configuration: it scrapes nothing, so it is empty."""

_NODE_EXPORTER_DEFAULTS = "/etc/default/prometheus-node-exporter"

EXPORT_PRESETS: dict[str, tuple[str, ...]] = {
    "health": (
        "up",
        "node_boot_time_seconds",
        "node_filesystem_avail_bytes",
        "node_filesystem_size_bytes",
        "node_memory_MemAvailable_bytes",
        "node_memory_MemTotal_bytes",
        "node_vmstat_oom_kill",
        "tundravm_system_state",
        "tundravm_units_failed",
    ),
}
"""The named keep-lists :class:`PrometheusRemoteWrite` takes in ``export=``. ``health`` is
what an operator watches a node by, after flashbots' recording rules: each scrape's
``up``, the boot time, free and total disk and memory, the OOM kills, and the node's own
readiness, ``systemctl is-system-running`` and the failed units, from ``image-metrics``."""

IDENTITY_SERIES = ("tundravm_image_digest", "tundravm_image_rtmr", "tundravm_mode")
"""What every keep-list holds, from ``image-metrics``: the UKI's Authenticode digest RTMR1's
event log records (``digest="unmeasured"`` without TDX) with the image's name, each RTMR
the TD reports, and the execution gate's mode."""

IMAGE_METRICS = "image-metrics"
"""The service writing the image's identity and the system's state for the node exporter, each
loaded service's state, CPU time, memory and OOM kills among it (``tundravm_unit_active``,
``tundravm_unit_cpu_seconds_total``, ``tundravm_unit_memory_bytes``,
``tundravm_unit_oom_kills_total``): what rules derive a container's liveness and load from,
through its :class:`UnitMetrics`."""

IMAGE_METRICS_DIRECTORY = f"/run/{IMAGE_METRICS}"
"""Its runtime directory: the node exporter's textfile collector reads it."""

_TDX_GUEST = "/dev/tdx_guest"

_SERIES = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")

JOURNAL_ACCESS = "/usr/lib/tundravm/journal-access"
"""What ``journal-export`` runs as root before the uploader: the journal's directories and
files to the ``systemd-journal`` group the uploader runs in, the directories setgid so the
files journald writes later stay in it. systemd's tmpfiles.d, which does this elsewhere, is
not in the image, and ``systemd-tmpfiles-setup.service`` is masked."""

_JOURNAL_ACCESS_SCRIPT = """\
#!/bin/sh
# tundravm Observability: the journal readable by the systemd-journal group.
for directory in /run/log/journal /var/log/journal; do
    [ -d "$directory" ] || continue
    chgrp -R systemd-journal "$directory"
    find "$directory" -type d -exec chmod g+rxs {} +
done
"""


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


def _leaves(url: str | None) -> bool:
    """Whether *url* names a receiver outside the guest: one not on the loopback."""
    if url is None:
        return False
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def _destination(url: str) -> str:
    """*url*'s ``HOST:PORT``, the port its scheme implies when it names none."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


PROXY_HOSTS = "observability-proxy"
"""The ``Hosts`` declaration mapping the proxy's names (its TLS name, or each sink's name
for a passthrough) to its address in the image's ``/etc/hosts``."""


@dataclass(frozen=True, slots=True)
class Proxy(Keywords):
    """The fixed-address proxy both exporters reach their sinks through:
    ``Proxy("10.0.0.9", 3128)``, which ``proxy="10.0.0.9:3128"`` also spells.

    Without *tls_name* the exporters speak plain HTTP CONNECT to it, and only each sink's
    own TLS, inside the tunnel, is encrypted. With *tls_name* the proxy speaks TLS itself:
    the exporters reach ``https://<tls_name>:<port>``, send *tls_name* as the SNI and check
    the proxy's certificate for it against the image's CA store, and the image maps the
    name to *address* in ``/etc/hosts`` (:data:`PROXY_HOSTS`), so the name never reaches a
    resolver and the firewall still allows the address alone. Not verified without a boot:
    that ``systemd-journal-upload``'s
    libcurl and Prometheus negotiate TLS with a given proxy, and that its certificate
    chains to a CA the image trusts (a private CA needs its certificate in the image).

    With *passthrough* it is no proxy protocol at all but a TCP relay to the sink, as
    flashbox's ``10.88.0.100:443``: the exporters reach each sink's own ``https://`` URL,
    whose name the image maps to *address* in ``/etc/hosts``, so the SNI, the certificate
    check and the ``Host`` header are the sink's, the connection goes to the proxy, and no
    resolver is asked. Each sink's URL then names a host, not an address, on *port*.
    """

    address: str
    port: int
    _: KW_ONLY
    tls_name: str | None = None
    passthrough: bool = False

    def __post_init__(self) -> None:
        hint = "Pass Proxy('10.0.0.9', 3128), the proxy's address and port."
        try:
            ipaddress.ip_address(self.address)
        except (TypeError, ValueError):
            raise ValidationError(
                f"Proxy: address {self.address!r} is not an IP address; the firewall matches "
                "the proxy by its address.",
                hint=hint,
            ) from None
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValidationError(f"Proxy: port {self.port!r} is not a number.", hint=hint)
        egress_host(self, self.endpoint)
        if self.tls_name is not None:
            if not isinstance(self.tls_name, str):
                raise ValidationError(
                    f"Proxy: tls_name {self.tls_name!r} is not a name.",
                    hint="Pass tls_name='proxy.example'.",
                )
            _, _, named = egress_host(self, f"{self.tls_name}:{self.port}")
            if not named:
                raise ValidationError(
                    f"Proxy: tls_name {self.tls_name!r} is an address, not the name the "
                    "proxy's certificate is for.",
                    hint="Pass tls_name='proxy.example'.",
                )
        if not isinstance(self.passthrough, bool):
            raise ValidationError(
                f"Proxy: passthrough {self.passthrough!r} is not True or False.",
                hint="Pass passthrough=True for a TCP relay to the sinks.",
            )
        if self.passthrough and self.tls_name is not None:
            raise ValidationError(
                "Proxy: a passthrough relays the sinks' own TLS, so it has no tls_name.",
                hint="Drop tls_name=: the sinks' names are the TLS names.",
            )

    def check_sink(self, url: str) -> None:
        """Refuse a sink *url* a passthrough cannot carry: not ``https://``, an address for
        a host, or another port than *port*."""
        if not self.passthrough:
            return
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        _, _, named = egress_host(self, f"{parts.hostname}:{port}")
        if parts.scheme != "https" or not named or port != self.port:
            raise ValidationError(
                f"Observability: sink {url!r} cannot go through the passthrough at "
                f"{self.endpoint}: it needs an https:// URL whose host is a name, on port "
                f"{self.port}.",
                hint="Pass the sink's own URL, 'https://metrics.example/api/v1/write': the "
                "image maps its name to the passthrough's address.",
            )

    @property
    def endpoint(self) -> str:
        """``IP:PORT`` (``[IPv6]:PORT``), what the firewall allows."""
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"{host}:{self.port}"

    @property
    def url(self) -> str | None:
        """The URL the exporters reach the proxy at: ``https://<tls_name>:<port>`` or
        ``http://<endpoint>``; ``None`` for a passthrough, which the exporters do not know of."""
        if self.passthrough:
            return None
        if self.tls_name is None:
            return f"http://{self.endpoint}"
        return f"https://{self.tls_name}:{self.port}"

    def hosts(self, urls: Sequence[str]) -> tuple[HostsEntry, ...]:
        """The ``/etc/hosts`` entries mapping *address* to the TLS name, or to each of the
        sinks' *urls*' names through a passthrough; none without either."""
        names = [self.tls_name] if self.tls_name is not None else []
        if self.passthrough:
            names = [urlsplit(url).hostname or "" for url in urls]
        return tuple(dict.fromkeys((self.address, (name,)) for name in names))


def _as_proxy(owner: object, proxy: object) -> Proxy | None:
    """*proxy* as a :class:`Proxy`: ``IP:PORT`` (``[IPv6]:PORT``) or a ``Proxy`` itself."""
    if proxy is None or isinstance(proxy, Proxy):
        return proxy
    hint = "Pass proxy='10.0.0.9:3128', the proxy's address and port: '[fd00::9]:3128' for IPv6."
    try:
        address, port, named = egress_host(owner, proxy)
    except ValidationError:
        address, port, named = "", None, True
    if port is None or named:
        raise ValidationError(
            f"Observability: proxy {proxy!r} is not IP:PORT; the firewall matches the "
            "proxy by its address.",
            hint=hint,
        )
    return Proxy(address, port)


def _check_credential(owner: object, credential: object) -> None:
    if credential is not None and not isinstance(credential, Secret):
        raise ValidationError(
            f"{type(owner).__name__}: credential {credential!r} is not a Secret.",
            hint="Pass credential=Secret('logs-key'): it is delivered, never built in.",
        )


@dataclass(frozen=True, slots=True)
class JournalExport(Keywords):
    """Ship the journal to *url* (a ``systemd-journal-remote`` receiver) over HTTPS.

    *credential* is a :class:`~tundravm.secrets.Secret` holding the client's PEM key and
    certificate, which the receiver authenticates; without it the upload is anonymous.
    The upload's cursor lives in its state directory, so a restart resumes rather than
    resends, and journald's volatile, capped storage is the buffer.

    Without *url* the receiver is in the guest: ``journal-sink``, Debian's
    ``systemd-journal-remote`` on the loopback, writing under ``/var/lib/journal-sink``. A
    development profile uses it too when *url* is outside the guest. There a dev boot shows
    the export working, nothing leaves the VM, and *credential* stays unbound. A production
    profile reports ``observability-sink-unset`` until *url* names the deployment's
    receiver.
    """

    url: str | None = None
    credential: Secret | None = None

    def __post_init__(self) -> None:
        if self.url is not None:
            _check_url(self, self.url)
        _check_credential(self, self.credential)

    def service(self, proxy: Proxy | None = None) -> Service:
        """The ``journal-export`` service, through *proxy* when given; without *url*, to
        the in-guest :meth:`sink`, never through a proxy."""
        local = self.url is None
        argv = [
            "/usr/lib/systemd/systemd-journal-upload",
            f"--url={SINK_URL if local else self.url}",
            "--save-state=/var/lib/journal-export/state",
        ]
        credential = None if local else self.credential
        if credential is not None:
            argv += [f"--key={credential.path}", f"--cert={credential.path}"]
        url = None if proxy is None or local else proxy.url
        return Service(
            LOGS_SERVICE,
            argv,
            requires=[self.sink().endpoint(SINK_PORT)] if local else [],
            credentials=[] if credential is None else [credential],
            state="journal-export",
            groups=["systemd-journal"],
            environment={} if url is None else {"https_proxy": url, "http_proxy": url},
            unit=dm.DropIn(
                exec_start_pre=(f"+/bin/sh {JOURNAL_ACCESS}",),
                after=() if local else ("time-sync.target",),
                wants=() if local else ("time-sync.target",),
            ),
        )

    def sink(self) -> Service:
        """``journal-sink``: the in-guest receiver, plain HTTP on the loopback."""
        return Service(
            SINK_SERVICE,
            [
                "/usr/lib/systemd/systemd-journal-remote",
                f"--listen-http=127.0.0.1:{SINK_PORT}",
                f"--output=/var/lib/{SINK_SERVICE}/",
            ],
            state=SINK_SERVICE,
            readiness=TcpProbe(SINK_PORT),
        )


_AWS_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")

METRICS_CREDENTIALS = "/run/credentials/metrics-export.service"
"""Where ``metrics-export`` reads its delivered credential."""


@dataclass(frozen=True, slots=True)
class SigV4(Keywords):
    """AWS Signature V4 for a remote-write sink such as Amazon Managed Prometheus:
    ``credential=SigV4("us-east-2", Secret("metrics-aws"))``.

    *secret* holds an AWS shared credentials file (``[default]`` with
    ``aws_access_key_id`` and ``aws_secret_access_key``), which Prometheus reads through
    ``AWS_SHARED_CREDENTIALS_FILE`` from the delivered credential, so no key is in the
    image or its configuration. The instance metadata server is never asked for one.
    """

    region: str
    secret: Secret

    def __post_init__(self) -> None:
        if not isinstance(self.region, str) or not _AWS_REGION.fullmatch(self.region):
            raise ValidationError(
                f"SigV4: region {self.region!r} is not an AWS region.",
                hint="Pass the sink's region: SigV4('us-east-2', Secret('metrics-aws')).",
            )
        if not isinstance(self.secret, Secret):
            raise ValidationError(
                f"SigV4: secret {self.secret!r} is not a Secret.",
                hint="Pass SigV4('us-east-2', Secret('metrics-aws')): it is delivered.",
            )


@dataclass(frozen=True, slots=True)
class RecordingRule(Keywords):
    """A series the guest computes from others: ``RecordingRule("node:up", "min(up)")``.

    *record* is the new series' name, with a colon (``level:metric``) as Prometheus names
    recorded series and no exporter does, so it never shadows a raw one. *expr* is PromQL,
    checked by ``promtool check rules`` in the image at build time. A rule reads the series
    the scrape collects and the rules declared before it. A service's, a container's and a
    volume's ``.metrics`` are those series as selectors with the labels filled in
    (:class:`UnitMetrics`, :class:`VolumeMetrics`): ``f"max({searcher.metrics.active})"``,
    ``data.metrics.available.over("128Gi")``.

    *export* says whether the series leaves the guest. ``export=False`` keeps an
    intermediate series, which later rules read, inside the TEE.
    """

    record: str
    expr: str
    _: KW_ONLY
    export: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record, str)
            or not _SERIES.fullmatch(self.record)
            or ":" not in self.record
        ):
            raise ValidationError(
                f"RecordingRule: record {self.record!r} is not a series name with a colon.",
                hint="Name it level:metric, as Prometheus names recorded series: "
                "'node:disk_free_over_10_percent'.",
            )
        if not isinstance(self.expr, str) or not self.expr.strip() or "\0" in self.expr:
            raise ValidationError(
                f"RecordingRule {self.record}: expr {self.expr!r} is not a PromQL expression.",
                hint="Pass the expression as text: 'min(up{job=\"node\"})'.",
            )
        object.__setattr__(self, "expr", str(self.expr))
        if not isinstance(self.export, bool):
            raise ValidationError(
                f"RecordingRule {self.record}: export {self.export!r} is not True or False.",
                hint="Pass export=False to keep the series in the guest.",
            )


@dataclass(frozen=True, slots=True)
class PrometheusRemoteWrite(Keywords):
    """Send node metrics to *url*, a Prometheus remote-write receiver.

    *credential* is a :class:`~tundravm.secrets.Secret` holding a bearer token, or a
    :class:`SigV4` that signs each write with an AWS key, as Amazon Managed Prometheus
    requires. The agent
    scrapes the node exporter on the loopback every *interval* and keeps at most *buffer*
    of samples it could not send; in memory, at most four shards of 10000 samples wait,
    retried from one second to one minute apart, a 429 included.

    *rules* are :class:`RecordingRule` values, evaluated every *interval* in their order.
    Each rule says itself whether its series leaves. Agent mode evaluates no rules, so with
    *rules* Prometheus runs in server mode. A local store under ``/var/lib/metrics-export``
    then keeps *buffer* of samples, and its query API listens on the loopback only.

    *export* names the raw series that leave, and the presets of :data:`EXPORT_PRESETS`
    (``"health"``). The agent drops every other series before the write
    (``write_relabel_configs``), since raw series from a TEE are a side channel. The
    image's :data:`IDENTITY_SERIES` always leave, so an operator tells which image a series
    came from. ``export=[]`` sends no raw series. Without *export* and without *rules* the
    agent keeps ``health``, and ``observability-export-unset`` reports it. That finding is
    an error in a production profile that writes to *url*, and a warning where nothing
    leaves the VM.

    Without *url* the receiver is in the guest: ``metrics-sink``, Prometheus with its
    remote-write receiver on the loopback, writing under ``/var/lib/metrics-sink``. A
    development profile uses it too when *url* is outside the guest. There a dev boot shows
    the export working, nothing leaves the VM, and *credential* stays unbound. A production
    profile reports ``observability-sink-unset`` until *url* names the deployment's
    receiver.
    """

    url: str | None = None
    credential: Secret | SigV4 | None = None
    interval: str = "30s"
    buffer: str = "1h"
    export: str | Sequence[str] | None = None
    rules: Sequence[RecordingRule] = ()

    def __post_init__(self) -> None:
        if self.url is not None:
            _check_url(self, self.url)
        if not isinstance(self.credential, SigV4):
            _check_credential(self, self.credential)
        for what in ("interval", "buffer"):
            value = getattr(self, what)
            if not isinstance(value, str) or not value[:-1].isdigit() or value[-1] not in "smh":
                raise ValidationError(
                    f"PrometheusRemoteWrite: {what} {value!r} is not a duration such as '30s'.",
                    hint="Use a number with s, m or h: '30s', '1h'.",
                )
        rules = self.rules
        hint = 'Pass rules=[RecordingRule("node:up", "min(up)")].'
        if isinstance(rules, (str, RecordingRule)) or not isinstance(rules, Sequence):
            raise ValidationError(
                f"PrometheusRemoteWrite: rules= holds {rules!r}, not a list of RecordingRule.",
                hint=hint,
            )
        object.__setattr__(self, "rules", tuple(rules))
        recorded: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, RecordingRule):
                raise ValidationError(
                    f"PrometheusRemoteWrite: rules= entry {rule!r} is not a RecordingRule.",
                    hint=hint,
                )
            if rule.record in recorded:
                raise ValidationError(
                    f"PrometheusRemoteWrite: two rules record {rule.record!r}.",
                    hint="Give each rule its own record name.",
                )
            recorded.add(rule.record)
        self.kept()

    def kept(self) -> tuple[str, ...]:
        """The series names that leave, in order, once each: the records of the rules with
        ``export=True``, then *export* with its presets expanded, then
        :data:`IDENTITY_SERIES`. Without *export*, ``health`` leaves when there are no
        rules. A rule's record in *export* is refused: the rule says that itself."""
        default = () if self.rules else "health"
        export = default if self.export is None else self.export
        entries: Sequence[object] = (
            [export] if isinstance(export, str) or not isinstance(export, Sequence) else export
        )
        found = [rule.record for rule in self.rules if rule.export]
        recorded = {rule.record for rule in self.rules}
        for entry in entries:
            if isinstance(entry, str) and entry in recorded:
                raise ValidationError(
                    f"PrometheusRemoteWrite: export entry {entry!r} is a rule's record; "
                    "rules say export= themselves.",
                    hint=f'Drop it from export=: RecordingRule("{entry}", ...) leaves unless '
                    "it says export=False.",
                )
            if isinstance(entry, str) and ":" in entry:
                raise ValidationError(
                    f"PrometheusRemoteWrite: export entry {entry!r} is a recorded series no "
                    "rule records, so nothing would carry it.",
                    hint=f'Declare it: rules=[RecordingRule("{entry}", "...")], or drop it '
                    "from export=.",
                )
            if isinstance(entry, str) and entry in EXPORT_PRESETS:
                found.extend(EXPORT_PRESETS[entry])
            elif isinstance(entry, str) and _SERIES.fullmatch(entry):
                found.append(entry)
            else:
                presets = ", ".join(repr(name) for name in EXPORT_PRESETS)
                raise ValidationError(
                    f"PrometheusRemoteWrite: export entry {entry!r} is neither a preset "
                    f"({presets}) nor a series name.",
                    hint='Pass export=["health", "node_load1"]: presets and series names.',
                )
        return tuple(dict.fromkeys((*found, *IDENTITY_SERIES)))

    def config(self, proxy: Proxy | None = None) -> dict[str, object]:
        """The agent's configuration: scrape the node exporter, keep :meth:`kept`, write to
        *url*, through *proxy* when given; without *url*, to the in-guest :meth:`sink`."""
        local = self.url is None
        remote: dict[str, object] = {"url": METRICS_SINK_URL if local else self.url}
        if proxy is not None and proxy.url is not None and not local:
            remote["proxy_url"] = proxy.url
        credential = None if local else self.credential
        if isinstance(credential, SigV4):
            remote["sigv4"] = {"region": credential.region}
        elif credential is not None:
            remote["authorization"] = {
                "type": "Bearer",
                "credentials_file": f"{METRICS_CREDENTIALS}/{credential.name}",
            }
        kept = self.kept()
        remote["write_relabel_configs"] = [
            {
                "source_labels": ["__name__"],
                "regex": "|".join(kept),
                "action": "keep",
            }
        ]
        remote["queue_config"] = {
            "capacity": 10000,
            "max_shards": 4,
            "min_backoff": "1s",
            "max_backoff": "1m",
            "retry_on_http_429": True,
        }
        timing = {"scrape_interval": self.interval}
        if self.rules:
            timing["evaluation_interval"] = self.interval
        config: dict[str, object] = {"global": timing}
        if self.rules:
            config["rule_files"] = [METRICS_RULES]
        config["scrape_configs"] = [
            {"job_name": "node", "static_configs": [{"targets": ["127.0.0.1:9100"]}]}
        ]
        config["remote_write"] = [remote]
        return config

    def rules_file(self) -> dict[str, object]:
        """:data:`METRICS_RULES`: *rules* as one group, in their order."""
        rules = [{"record": rule.record, "expr": rule.expr} for rule in self.rules]
        return {"groups": [{"name": "tundravm", "rules": rules}]}

    def service(self, proxy: Proxy | None = None) -> Service:
        """The ``metrics-export`` service: Prometheus in agent mode, in server mode with
        *rules*, through *proxy*; without *url*, to the in-guest :meth:`sink`, never through a
        proxy."""
        local = self.url is None
        credential = None if local else self.credential
        environment = {}
        if isinstance(credential, SigV4):
            environment = {
                "AWS_SHARED_CREDENTIALS_FILE": f"{METRICS_CREDENTIALS}/{credential.secret.name}",
                "AWS_EC2_METADATA_DISABLED": "true",
            }
            credential = credential.secret
        mode, store = ([], "tsdb") if self.rules else (["--enable-feature=agent"], "agent")
        retention = "retention.time" if self.rules else "retention.max-time"
        return Service(
            METRICS_SERVICE,
            [
                "/usr/bin/prometheus",
                *mode,
                f"--config.file={METRICS_CONFIG}",
                f"--storage.{store}.path=/var/lib/metrics-export",
                f"--storage.{store}.{retention}={self.buffer}",
                "--web.listen-address=127.0.0.1:9090",
            ],
            requires=[self.sink().endpoint(METRICS_SINK_PORT)] if local else [],
            credentials=[] if credential is None else [credential],
            state="metrics-export",
            environment=environment,
            unit=None
            if local
            else dm.DropIn(after=("time-sync.target",), wants=("time-sync.target",)),
        )

    def identity(self) -> tuple[dm.Build, dm.Service]:
        """``image-metrics`` from ``tools/`` and its service: as root without capabilities
        (the event log and the TDX device are root's), with the TDX device and AF_UNIX
        alone, rewriting its file in :data:`IMAGE_METRICS_DIRECTORY` every *interval*."""
        go = dm.Go(package=f"./cmd/{IMAGE_METRICS}", output=IMAGE_METRICS)
        program = f"/usr/bin/{IMAGE_METRICS}"
        build = dm.Build(
            IMAGE_METRICS,
            du.tools_source(),
            using=go,
            install=(dm.Install(go.artifact, program),),
        )
        service = dm.Service(
            IMAGE_METRICS,
            (program, f"-interval={self.interval}", f"{IMAGE_METRICS_DIRECTORY}/image.prom"),
            description="Write the image's identity for the node exporter",
            user=Root(),
            runtime=IMAGE_METRICS,
            needs=(),
            hardening=Hardening.strict(devices=(_TDX_GUEST,), network="unix"),
        )
        return build, service

    def sink(self) -> Service:
        """``metrics-sink``: the in-guest receiver, plain HTTP on the loopback."""
        return Service(
            METRICS_SINK_SERVICE,
            [
                "/usr/bin/prometheus",
                f"--config.file={METRICS_SINK_CONFIG}",
                "--web.enable-remote-write-receiver",
                f"--web.listen-address=127.0.0.1:{METRICS_SINK_PORT}",
                f"--storage.tsdb.path=/var/lib/{METRICS_SINK_SERVICE}",
            ],
            state=METRICS_SINK_SERVICE,
            readiness=TcpProbe(METRICS_SINK_PORT),
        )


class Observability(Component):
    """The export path for logs and metrics: ``image.observability`` takes it.

    *logs* is a :class:`JournalExport`, *metrics* a :class:`PrometheusRemoteWrite`; each
    is optional. Their credentials are secrets, which the image collects from them.
    :meth:`outbound` is each sink's host and port (a host name stands for its port).

    *proxy* (``"IP:PORT"``, ``"[IPv6]:PORT"`` or a :class:`Proxy`) is an HTTP CONNECT
    proxy at a fixed address through which both reach their sinks: :meth:`outbound` is
    then the proxy alone, so a filtered egress opens one address instead of a port to any.
    The TLS session to each sink runs end to end inside the tunnel, with the sink's name as
    its SNI and its certificate checked, so the proxy can allow by name what the firewall
    can only allow by address, and the guest resolves no sink. ``systemd-journal-upload``
    reads it from ``https_proxy``, Prometheus from ``proxy_url``. A proxy that speaks TLS
    itself is ``Proxy(address, port, tls_name=...)``: neither exporter takes the proxy's
    TLS name apart from its URL, so the component pins the name to the address in
    ``/etc/hosts``. ``Proxy(address, port, passthrough=True)`` is a TCP relay instead:
    each sink's name is pinned to *address*, and the exporters know of no proxy.

    A development profile builds :meth:`development` in its place, so nothing leaves a
    dev VM.

    Exports :meth:`service` refs ``journal-export`` and ``metrics-export``. Component name:
    ``observability``.
    """

    logs: JournalExport | None = None
    metrics: PrometheusRemoteWrite | None = None
    proxy: str | Proxy | None = None

    def __post_init__(self) -> None:
        proxy = _as_proxy(self, self.proxy)
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
                hint="Pass logs=JournalExport() and/or metrics=PrometheusRemoteWrite().",
            )
        if proxy is not None:
            for url in self._urls():
                proxy.check_sink(url)

    def _urls(self) -> list[str]:
        """The sinks' URLs, the in-guest receivers' left out."""
        return [s.url for s in (self.logs, self.metrics) if s is not None and s.url is not None]

    def development(self) -> Observability:
        """What a development profile builds: each sink whose *url* is outside the guest
        goes to its in-guest receiver, and its credential stays unbound. The proxy goes too
        when no sink outside the guest is left. A sink on the loopback stays as it is."""
        logs, metrics = self.logs, self.metrics
        if logs is not None and _leaves(logs.url):
            logs = replace(logs, url=None)
        if metrics is not None and _leaves(metrics.url):
            metrics = replace(metrics, url=None)
        found = replace(self, logs=logs, metrics=metrics)
        return found if found._urls() else replace(found, proxy=None)

    def outbound(self) -> Egress:
        """*proxy*, or each sink's ``HOST:PORT`` without one; nothing when every sink is
        in the guest."""
        proxy = _as_proxy(self, self.proxy)
        if proxy is not None and self._urls():
            return Egress(hosts=[proxy.endpoint])
        urls = (sink.url for sink in (self.logs, self.metrics) if sink is not None)
        return Egress(hosts=list(dict.fromkeys(_destination(url) for url in urls if url)))

    def compose(self) -> Iterator[object]:
        proxy = _as_proxy(self, self.proxy)
        hosts = () if proxy is None else proxy.hosts(self._urls())
        if hosts:
            yield dm.Hosts(PROXY_HOSTS, hosts)
        if self.logs is not None:
            yield Package("systemd-journal-remote")
            yield File(JOURNAL_ACCESS, _JOURNAL_ACCESS_SCRIPT, mode=0o755)
            # The packaged uploader reads /etc; ours runs from its own unit and arguments.
            yield dm.Unit("systemd-journal-upload.service", masked=True)
            if self.logs.url is None:
                # The packaged receiver's socket would take the port, on every address.
                yield dm.Unit("systemd-journal-remote.socket", masked=True)
                yield dm.Unit("systemd-journal-remote.service", masked=True)
                yield self.logs.sink()
            yield self.logs.service(proxy)
        if self.metrics is not None:
            yield Package("prometheus")
            yield Package("prometheus-node-exporter")
            # Debian's prometheus.service would serve a query API on every interface.
            yield dm.Unit("prometheus.service", masked=True)
            config = self.metrics.config(proxy)
            yield ConfigFile.yaml(METRICS_CONFIG, config, sort_keys=False)
            if self.metrics.rules:
                yield ConfigFile.yaml(METRICS_RULES, self.metrics.rules_file(), sort_keys=False)
                check = f"mkosi-chroot promtool check rules {METRICS_RULES}"
                yield dm.Hook("metrics-export-rules", "postinst", check)
            yield ConfigFile.env(
                _NODE_EXPORTER_DEFAULTS,
                {
                    "ARGS": "--web.listen-address=127.0.0.1:9100 "
                    f"--collector.textfile.directory={IMAGE_METRICS_DIRECTORY}"
                },
            )
            yield from self.metrics.identity()
            yield dm.Unit("prometheus-node-exporter.service", enabled=True)
            if self.metrics.url is None:
                yield ConfigFile.yaml(METRICS_SINK_CONFIG, {"global": {}}, sort_keys=False)
                yield self.metrics.sink()
            yield self.metrics.service(proxy)

    rules = (
        Rule(
            "observability-plaintext",
            "warning",
            "An Observability sink is http://, so the host and every hop read what the guest logs.",
            "Point the sink at an https:// URL.",
        ),
        Rule(
            "observability-export-unset",
            ("error", "warning"),
            "PrometheusRemoteWrite names neither an export= keep-list nor rules, so what "
            "leaves the TEE is undeclared; it keeps the health preset.",
            'Pass export="health", or the series names that may leave: '
            'export=["health", "node_load1"].',
            when=("where nothing leaves the VM: a development profile, or no url=",),
        ),
        Rule(
            "observability-sink-unset",
            "warning",
            "A production profile ships its logs or metrics to the in-guest receiver, so "
            "nothing leaves the VM.",
            "Pass url= on JournalExport (a systemd-journal-remote receiver) or "
            "PrometheusRemoteWrite (a remote-write receiver): the deployment's https:// URL.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``observability-plaintext``: an http:// sink sees what the TD logs in the clear;
        ``observability-sink-unset``: a production profile's logs or metrics never leave the
        VM; ``observability-export-unset``: the metrics name no keep-list and no rules, an error
        where a production profile writes them to *url*."""
        found: list[dm.Diagnostic] = []
        metrics = self.metrics
        if metrics is not None and metrics.export is None and not metrics.rules:
            leaves = resolved.production and metrics.url is not None
            found.append(
                dm.Diagnostic(
                    "observability-export-unset",
                    "error" if leaves else "warning",
                    f"PrometheusRemoteWrite names no export=: {metrics.url} would receive "
                    "undeclared series"
                    if leaves
                    else "PrometheusRemoteWrite names no export=: it keeps the health preset",
                )
            )
        for sink, what in ((self.logs, "the journal"), (self.metrics, "the node metrics")):
            if resolved.production and sink is not None and sink.url is None:
                found.append(
                    dm.Diagnostic(
                        "observability-sink-unset",
                        "warning",
                        f"{type(sink).__name__} names no url: the in-guest receiver takes "
                        f"{what}, and nothing leaves the VM",
                    )
                )
        for name, sink in (("logs", self.logs), ("metrics", self.metrics)):
            if sink is not None and sink.url is not None and sink.url.startswith("http://"):
                found.append(
                    dm.Diagnostic(
                        "observability-plaintext",
                        "warning",
                        f"Observability {name} go to {sink.url} in the clear: the host and "
                        "every hop read them",
                    )
                )
        return tuple(found)
