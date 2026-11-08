# Metric rules

Date: 2026-11-03. Status: design, then landed in the commits that follow it. Section 7 of
`flashbots-images-gaps-2026-11-02.md`: flashbox-l1 reduces its raw series to `flashbox:*`
booleans inside the TEE and ships only those, because raw series from a TEE are a side
channel. `PrometheusRemoteWrite(export=)` kept raw series by name and nothing else.

## The declaration

```python
PrometheusRemoteWrite(
    url,
    credential=token,
    rules=[
        RecordingRule("local:root_free", 'node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}'),
        RecordingRule("node:disk_free_over_10_percent", "local:root_free > bool 0.1"),
    ],
    export=["node:disk_free_over_10_percent"],
)
```

`rules=` computes; `export=` stays the one keep-list, now naming rule outputs beside raw
series and presets. Chosen over `export=[Rule(...)]` because flashbox's rules record
intermediates (`local:container_cpu_percent`) that never leave: with rules inside the
keep-list those would need a second spelling. One list says what leaves, one says what is
computed, as Prometheus itself splits `rule_files` from `write_relabel_configs`.

The class is `RecordingRule`, not `Rule`: `tundravm.components.Rule` is already the lint
rule a component declares.

## Refusals

- A record name is a series name with a colon (`level:metric`), Prometheus' convention for
  recorded series and one no exporter uses, so a rule output never shadows a raw series.
- Two rules recording one name.
- An `export=` name with a colon no rule records: nothing would ever carry it.
- An empty expression, or one that is not a string.

The expression itself is checked at build time by the program that runs it: a postinst
step runs `promtool check rules` (Debian's `prometheus` package ships it) in the image's
chroot, as `visudo -c` checks a Login's sudoers. The tools tree carries no promtool.

## Lowering

Prometheus' agent mode evaluates no rules and refuses `rule_files`. vmagent does not
either (vmalert does), and neither is in trixie. Prometheus in server mode, the same
package already in the image, does: with `rules=`, `metrics-export` runs without
`--enable-feature=agent`, keeps a local TSDB under `/var/lib/metrics-export` for
`--storage.tsdb.retention.time=<buffer>`, evaluates `/etc/tundravm/metrics-rules.yml`
(one group, the declared order, so a rule reads the ones before it) every `interval`, and
writes only the keep-list. Its HTTP API listens on 127.0.0.1:9090 only, behind a web
config (see "The closed API"). Without `rules=` nothing changes: agent mode. No package is added, so no packaged unit needs masking
beyond `prometheus.service`, which already is.

`image-metrics` already exports the UKI's Authenticode digest
(`tundravm_image_digest`) and the RTMRs as series. What was missing is flashbox's
`print-measurements` journal line: `image-metrics` now logs the image, digest, mode and
RTMR0-3 once at start.

## Liveness and CPU per unit

flashbox derives the container's liveness and CPU from `prometheus-process-exporter`, which
groups processes by a command-line regex, listens on its own port and brings its own
enabled unit. The node exporter's systemd collector has unit states but no CPU, and needs
D-Bus. systemd already accounts each unit's cgroup, and a container's processes are all in
its unit's cgroup: `image-metrics`, which runs `systemctl` as root every interval anyway,
writes `tundravm_unit_active{unit=}` and `tundravm_unit_cpu_seconds_total{unit=}` for each
loaded service (`systemctl show -p Id,LoadState,ActiveState,CPUUsageNSec`). No package, no
port, no name matching. Raw, so only rules over them leave.

## Selectors

Rules spelled PromQL with doubled braces and label values the image file already held:
`f'max(tundravm_unit_active{{unit="{searcher.unit}"}})'`, and a size as `128 * 1024**3`.
`.metrics` on a `Service` and a `Container` is a `UnitMetrics` of the unit, on a `Volume` a
`VolumeMetrics` of the mount point. Each property is a `Selector`, a `str` holding the
PromQL itself with its labels filled in, so an f-string takes it as it is and a bare one is
an expression:

| Property | PromQL |
|---|---|
| `active` | `tundravm_unit_active{unit="U"}` |
| `cpu_seconds` | `tundravm_unit_cpu_seconds_total{unit="U"}` |
| `memory_bytes` | `tundravm_unit_memory_bytes{unit="U"}` |
| `oom_kills` | `tundravm_unit_oom_kills_total{unit="U"}` |
| `available` | `node_filesystem_avail_bytes{mountpoint="M"}` |
| `size` | `node_filesystem_size_bytes{mountpoint="M"}` |
| `used_ratio` | `(1 - available / size)`, both selectors of M |

`image-metrics` had liveness and CPU per unit. It now also writes memory (`MemoryCurrent`
from the same `systemctl show`) and OOM kills: `oom_kill` of the unit's cgroup's
`memory.events`, which counts the cgroup and everything below it, so a container's kills
are its own, where `node_vmstat_oom_kill` is the node's.

A size is `.over("128Gi")` on a selector of bytes (`available`, `size`, `memory_bytes`):
`SELECTOR > bool 137438953472`. Chosen over a `Bytes("128Gi")` helper: no new name beside
the selectors, and the rule reads as its record says (`disk_free_space_is_over_128_gb`).
Binary units only (`Ki`, `Mi`, `Gi`, `Ti`, with or without `B`); `"128GB"` is refused
rather than guessed. `.over` on a selector that is not in bytes is refused. A builder for
whole expressions (`Max(searcher.metrics.active)`) is not offered: PromQL stays text, with
selectors inside, and `promtool check rules` still checks it at build time.

flashbox's rules in this spelling, for its `health.py`:

```python
RULES = [
    RecordingRule("flashbox:container_alive", f"max({searcher.metrics.active})"),
    RecordingRule(
        "flashbox:container_cpu_ratio",
        f"sum(rate({searcher.metrics.cpu_seconds}[5m]))"
        ' / count(node_cpu_seconds_total{mode="idle"})',
        export=False,
    ),
    RecordingRule(
        "flashbox:container_average_cpu_is_under_80_percent",
        "(avg_over_time(flashbox:container_cpu_ratio[15m]) < bool 0.8)"
        " * (max_over_time(flashbox:container_cpu_ratio[10m] offset 5m) < bool 0.7)",
    ),
    RecordingRule("flashbox:container_oom_kills_count", searcher.metrics.oom_kills),
    RecordingRule(
        "flashbox:disk_free_space_is_over_10_percent", f"{data.metrics.used_ratio} < bool 0.9"
    ),
    RecordingRule("flashbox:disk_free_space_is_over_128_gb", data.metrics.available.over("128Gi")),
    RecordingRule(
        "flashbox:network_is_up",
        '(sum(rate(node_network_receive_bytes_total{device!~"lo"}[5m]))'
        ' + sum(rate(node_network_transmit_bytes_total{device!~"lo"}[5m]))) > bool 0',
    ),
]
```

Two rules change meaning on purpose: the OOM count is the container's, not the node's, and
the 10 percent rule reads the data volume, not `/`, which on this image is the read-only
erofs verity root, with no free space to watch. `promtool check rules` (2.53) accepts the rendered file. The group is named
after the image (`name: flashbox`), so rule-group metrics tell images apart.

## The closed API

In server mode `metrics-export` serves the query API on 127.0.0.1:9090, and any host process
could read every raw series the keep-list holds back from the sink. Prometheus takes
`--web.config.file` (the exporter toolkit's), so the server gets
`/etc/tundravm/metrics-export-web.yml`: `basic_auth_users` with one user whose bcrypt hash
is SHA-512 of a fixed label laid out as one, not the hash of any password. Every request is
refused (401), `/metrics` and `/-/ready` too, and the bytes are the same in every build, as
a random hash made at build would not be. Agent mode gets the same file: its API holds
little, and one rule is simpler to review than two.

Prometheus needs no request to its own API: it scrapes the node exporter, evaluates the
rules and remote-writes on its own. Checked against Prometheus 2.53.4 in both modes: the
API answers 401 with and without a guessed password, and the in-guest `metrics-sink` (no
web config, on 9091) receives the scrape and the rule outputs. The probe's `observability:` rows ask the sink and read
the unit's state and the config file, never port 9090.

Not chosen: a unix socket (Prometheus' `--web.listen-address` takes TCP only), a network
namespace (`PrivateNetwork=` would cut the server off from the node exporter and the sink on
the host's loopback, and from the remote receiver), a firewall rule by `meta skuid` on the
loopback (it ties the exporter to the firewall component, and a mistake there reopens the
port silently), TLS client certificates (a CA and a key the image would have to hold, for a
client that does not exist).

## Flashbots' metrics path

flashbox writes to `https://10.88.0.100/workspaces/<ws>/api/v1/remote_write` with
`tls_config.server_name` set to the AMP host and SigV4 keys in the rendered config. The
proxy there is a TCP passthrough, not HTTP CONNECT, so `Observability(proxy=)` could not
reach it. `Proxy("10.88.0.100", 443, passthrough=True)` keeps the sink's real URL and maps
its name to the address in the component's `/etc/hosts` fragment: SNI, certificate check
and `Host` are the sink's (SigV4 signs `Host`), the firewall allows the address alone, and
no resolver is asked. Refused: a passthrough with `tls_name=`, a sink by address, a sink
not `https://`, or on another port than the relay's.

SigV4, which the gaps note refused because "a proxy can sign", is needed after all: the
passthrough cannot. `credential=SigV4("us-east-2", Secret("metrics-aws"))` renders
`sigv4: {region:}` and points `AWS_SHARED_CREDENTIALS_FILE` at the delivered credential, an
AWS shared credentials file, with `AWS_EC2_METADATA_DISABLED=true`. The key never sits in
the image or the configuration. Debian's Prometheus signs, as flashbox's use of it shows.
Not verified without a boot against AMP.

## Proof

- Goldens: the fixture tree's dev profile carries the rules file, the server-mode argv and
  the postinst check.
- `observability:series` in the fixture's probe waits for each exported rule output at the
  in-guest sink and fails on any series outside the keep-list; its detail lists what the
  sink holds. The fixture exports only rule outputs (and the identity every keep-list
  holds), so the row shows no raw series leaving.
- `observability:metrics-export` checks the server-mode configuration with `promtool check
  config` (without `--agent`).
