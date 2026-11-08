"""The searcher, BoB's bot in a container of its own, and the two channels flashbox gives it.

``delay-pipe`` copies the bot's log into ``output.log`` *delay* seconds late, which is what
the operator's menu shows, so nothing the searcher logs reaches anyone while the trade it
describes is still live. ``input-proxy`` carries bytes from TCP 27018 into the container's
``input.sock``, one way. Both are built from ``channels/`` beside the image file.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from tundravm import Service
from tundravm.components import Component, Endpoint
from tundravm.components.container import Bind, Container, ContainerSsh
from tundravm.components.observability import UnitMetrics
from tundravm.ports import TCP, UDP
from tundravm.sources import GoBinary, Local
from tundravm.storage import VolumeDirectory

from .orderflow import EGRESS, MODES

INPUT_UDP = UDP(27017)
INPUT_TCP = TCP(27018)


class Searcher(Component):
    """The searcher's sandbox: its container, the delayed log and the input channel.

    *rpc* is the execution client's JSON-RPC, which the container reaches at the same
    address. *state* is the bot's directory, *input* holds the socket ``input-proxy`` writes
    and *delayed* the log ``delay-pipe`` writes, *delay* seconds behind ``bob.log``. Both
    services join the container's group, so they read and write what it shares without
    running as its account. Each of *logs_from* is a host service's log directory, which
    the container reads at the same path: ``lighthouse.logs``.
    """

    rpc: Endpoint
    state: VolumeDirectory
    input: VolumeDirectory
    delayed: VolumeDirectory
    delay: int
    logs_from: Sequence[VolumeDirectory] = ()
    ssh_port: int = 10022

    @property
    def container(self) -> Container:
        """The container, built from the fields, as ``compose()`` yields it."""
        return Container(
            "bob",
            # socat stands in for BoB's listener on input.sock in the workflow's input check.
            packages=["ca-certificates", "curl", "socat"],
            state=self.state,
            ssh=self.ssh,
            expose=[INPUT_UDP],
            egress=EGRESS,
            modes=MODES,
            requires=[self.rpc],
            binds=[
                Bind(self.input, writable=True),
                *(Bind(directory) for directory in self.logs_from),
            ],
        )

    @property
    def ssh(self) -> ContainerSsh:
        """The container's sshd on *ssh_port*, letting in the instance owner's key: what a
        firewall mode's ``ingress=`` opens."""
        return ContainerSsh(port=self.ssh_port, owner=True)

    @property
    def unit(self) -> str:
        """The container's unit."""
        return self.container.unit

    @property
    def metrics(self) -> UnitMetrics:
        """The container's series as PromQL selectors, which the health rules read."""
        return self.container.metrics

    @property
    def logs(self) -> str:
        """The delayed log, the only one the operator reads."""
        return self.delayed / "output.log"

    def compose(self) -> Iterator[object]:
        container = self.container
        channels = Local("channels")
        yield container
        yield Service(
            "delay-pipe",
            [
                GoBinary(channels, package="./cmd/delay-pipe"),
                self.state / "bob.log",
                self.logs,
                self.delay,
            ],
            groups=[container.group],
            state=self.delayed,
            description="Copy the searcher's log to output.log, "
            f"each line {self.delay} seconds late",
        )
        yield Service(
            "input-proxy",
            [
                GoBinary(channels, package="./cmd/input-proxy"),
                INPUT_TCP.address,
                self.input / "input.sock",
            ],
            groups=[container.group],
            state=self.input,
            expose=[INPUT_TCP],
            description=f"Carry input on {INPUT_TCP} into the searcher's socket, one way",
        )
