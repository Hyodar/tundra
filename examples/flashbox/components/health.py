"""flashbox's health records, computed in the guest from the node exporter's series.

Each record that leaves is a 0/1 boolean or a count, after flashbots' ``recording_rules.yml``;
the series they read stay in the TEE.
"""

from tundravm.components.observability import RecordingRule
from tundravm.storage import Volume

from .searcher import Searcher

CPUS = 'count(node_cpu_seconds_total{mode="idle"})'
"""How many CPUs the VM has: one idle series each."""


def health_rules(searcher: Searcher, volume: Volume) -> list[RecordingRule]:
    """The searcher's liveness, load and OOM kills, the volume's free space, and traffic."""
    unit = searcher.metrics
    disk = volume.metrics
    return [
        RecordingRule("flashbox:container_alive", f"max({unit.active})"),
        RecordingRule(
            "flashbox:container_cpu_ratio",
            f"sum(rate({unit.cpu_seconds}[5m])) / {CPUS}",
            export=False,
        ),
        RecordingRule(
            "flashbox:container_average_cpu_is_under_80_percent",
            "(avg_over_time(flashbox:container_cpu_ratio[15m]) < bool 0.8)"
            " * (max_over_time(flashbox:container_cpu_ratio[10m] offset 5m) < bool 0.7)",
        ),
        RecordingRule("flashbox:container_oom_kills_count", unit.oom_kills),
        RecordingRule(
            "flashbox:disk_free_space_is_over_10_percent", f"{disk.used_ratio} < bool 0.9"
        ),
        RecordingRule("flashbox:disk_free_space_is_over_128_gb", disk.available.over("128Gi")),
        # Neither lo nor a container's veth: traffic between the host and its container is
        # not the network being up.
        RecordingRule(
            "flashbox:network_is_up",
            '(sum(rate(node_network_receive_bytes_total{device!~"lo|ve-.*"}[5m]))'
            ' + sum(rate(node_network_transmit_bytes_total{device!~"lo|ve-.*"}[5m]))) > bool 0',
        ),
    ]
