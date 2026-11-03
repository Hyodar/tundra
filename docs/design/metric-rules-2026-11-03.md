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
writes only the keep-list. Its query API listens on 127.0.0.1:9090 only. Without `rules=`
nothing changes: agent mode. No package is added, so no packaged unit needs masking
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

## Proof

- Goldens: the fixture tree's dev profile carries the rules file, the server-mode argv and
  the postinst check.
- `observability:series` in the fixture's probe waits for each exported rule output at the
  in-guest sink and fails on any series outside the keep-list; its detail lists what the
  sink holds. The fixture exports only rule outputs (and the identity every keep-list
  holds), so the row shows no raw series leaving.
- `observability:metrics-export` checks the server-mode configuration with `promtool check
  config` (without `--agent`).
