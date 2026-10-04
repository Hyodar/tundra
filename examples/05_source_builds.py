"""Source builds: compile software from Git or an HTTP tarball and install the results.

Teaches ``Build``: a ``Git`` or ``Http`` source; either a toolchain ``recipe``
(``Go``, ``Cargo``, ``Dotnet``: the deterministic build command and its packages) or a
shell ``script``; ``Install`` steps for files and, with ``directory=True``, whole
trees; ``cache_key`` naming the build-cache entry so a rebuild reuses it. Refs and
tarballs are unpinned until ``tundravm lock`` records their commit and sha256.

    tundravm inspect examples/05_source_builds.py
    tundravm lock examples/05_source_builds.py        # pin both sources (network)
    tundravm compile examples/05_source_builds.py --out build/source-builds/mkosi
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Go,
    Http,
    Install,
    Package,
    Recipe,
    Service,
    User,
)

BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

NODE_EXPORTER = "1.8.2"
PROMETHEUS = "v2.53.0"

# A release tarball built with the Go recipe: `go build -trimpath` into build/.
node_exporter = Build(
    "node-exporter",
    Http(f"https://github.com/prometheus/node_exporter/archive/refs/tags/v{NODE_EXPORTER}.tar.gz"),
    recipe=Go(output="node_exporter", package=".", env={"CGO_ENABLED": "0"}),
    install=(Install("build/node_exporter", "/usr/bin/node_exporter"),),
    cache_key=f"node-exporter-{NODE_EXPORTER}",
)

# A Git tag built with a script; the console templates are copied as a directory tree.
prometheus = Build(
    "prometheus",
    Git("https://github.com/prometheus/prometheus", PROMETHEUS),
    script="go build -trimpath -ldflags '-s -w -buildid=' -o prometheus ./cmd/prometheus",
    packages=("golang",),
    env=(("CGO_ENABLED", "0"),),
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
    cache_key=f"prometheus-{PROMETHEUS}",
)

recipe = Recipe(
    name="source-builds",
    base="debian/bookworm",
    common=Fragment(
        "base",
        items=(
            *(Package(name) for name in (*BOOT, "ca-certificates")),
            node_exporter,
            prometheus,
            User("prometheus", home="/var/lib/prometheus"),
            Service(
                "node-exporter",
                "/usr/bin/node_exporter --web.listen-address=127.0.0.1:9100",
                user="prometheus",
                restart="on-failure",
                wanted_by="minimal.target",
            ),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() runs the builds
