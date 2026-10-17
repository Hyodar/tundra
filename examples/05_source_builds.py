"""Source builds: compile software from Git or an HTTP tarball and install the results.

Teaches ``GoBinary``: one Go program built from source and installed at its
``executable`` path, which the ``Service`` command names, so the build output, the
install path and the command cannot drift apart (``CargoBinary`` is the Rust one).
Teaches ``Build`` for everything else: a ``Git`` or ``Http`` source; either the
toolchain it is ``using`` (``Go``, ``Cargo``, ``Dotnet``) or a shell ``script``;
``Install`` steps for files and, with ``directory=True``, whole trees;
``cache_namespace`` naming the build-cache entry (its key is fingerprinted from the
build) so a rebuild reuses it. Refs and tarballs are unpinned until ``tundravm lock``
records their commit and sha256.

    tundravm inspect examples/05_source_builds.py
    tundravm lock examples/05_source_builds.py        # pin both sources (network)
    tundravm compile examples/05_source_builds.py --tree build/source-builds/mkosi
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Build,
    Debian,
    Git,
    Http,
    Install,
    Package,
    Recipe,
    Service,
    User,
)
from tundravm.declarative.utils import GoBinary

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

# A Git tag built with a script; the console templates are copied as a directory tree.
prometheus = Build(
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

recipe = Recipe(
    name="source-builds",
    platform=Debian("bookworm"),
    items=(
        Package("ca-certificates"),
        node_exporter,
        prometheus,
        User("prometheus", home="/var/lib/prometheus"),
        Service(
            "node-exporter",
            f"{node_exporter.executable} --web.listen-address=127.0.0.1:9100",
            user="prometheus",
            restart="on-failure",
            wanted_by="minimal.target",
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() runs the builds
