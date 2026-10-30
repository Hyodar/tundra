"""Source builds: compile software from Git or an HTTP tarball and install the results.

Teaches ``GoBinary``: one Go program built from source and installed at its
``executable`` path. A built binary is a valid first element of a ``Service`` argv, so
the build output, the install path and the command cannot drift apart (``CargoBinary``
is the Rust one). Teaches ``Build`` for everything else: a ``Git`` or ``Http`` source;
either the toolchain it is ``using`` or a shell ``script``; ``Install`` steps for files
and, with ``directory=True``, whole trees; ``cache_namespace`` naming the build-cache
entry (its key is fingerprinted from the build) so a rebuild reuses it. Build
dependencies belong to the build, never to the image's ``packages``; a ``Build`` that is
not a service's first argument reaches the image through the component that owns it.
Refs and tarballs are unpinned until ``tundravm lock`` records their commit and sha256.

    tundravm inspect examples/05_source_builds.py
    tundravm lock examples/05_source_builds.py        # pin both sources (network)
    tundravm compile examples/05_source_builds.py --tree build/source-builds/mkosi
"""

from __future__ import annotations

from collections.abc import Iterator

from tundravm import (
    TCP,
    Build,
    Component,
    Endpoint,
    GoBinary,
    Http,
    Image,
    Install,
    Service,
    TcpProbe,
)
from tundravm.declarative import Git

NODE_EXPORTER = "1.8.2"
PROMETHEUS = "v2.53.0"

# A release tarball: `go build -trimpath` of the root package, installed at
# node_exporter.executable (/usr/bin/node_exporter).
node_exporter = GoBinary(
    "node-exporter",
    Http(f"https://github.com/prometheus/node_exporter/archive/refs/tags/v{NODE_EXPORTER}.tar.gz"),
    package=".",
    install="/usr/bin/node_exporter",
    env={"CGO_ENABLED": "0"},
    cache_namespace=f"node-exporter-{NODE_EXPORTER}",
)


class Prometheus(Component):
    """Prometheus built from a Git tag with a script, and the service that runs it."""

    port: int = 9090

    @property
    def http(self) -> Endpoint:
        """Where it answers, with its readiness."""
        return Endpoint("127.0.0.1", self.port, ready=self.service("prometheus").ready)

    def compose(self) -> Iterator[object]:
        yield Build(
            "prometheus",
            Git("https://github.com/prometheus/prometheus", PROMETHEUS),
            script="go build -trimpath -ldflags '-s -w -buildid=' -o prometheus ./cmd/prometheus",
            packages=("golang",),
            env={"CGO_ENABLED": "0"},
            install=(
                Install("prometheus", "/usr/bin/prometheus"),
                Install("consoles", "/usr/share/prometheus/consoles", mode=None, directory=True),
                Install(
                    "console_libraries",
                    "/usr/share/prometheus/console_libraries",
                    mode=None,
                    directory=True,
                ),
            ),
            cache_namespace=f"prometheus-{PROMETHEUS}",
        )
        yield Service(
            "prometheus",
            ["/usr/bin/prometheus", f"--web.listen-address=127.0.0.1:{self.port}"],
            state="prometheus",
            expose=[TCP(self.port)],
            readiness=TcpProbe(self.port),
        )


image = Image(
    "source-builds",
    packages=["ca-certificates"],
    services=[
        # The built binary as the first argv element pulls its build and install in.
        Service(
            "node-exporter",
            [node_exporter, "--web.listen-address=127.0.0.1:9100"],
            readiness=TcpProbe(9100),
        ),
    ],
    components=[Prometheus()],
)
