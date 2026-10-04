"""Taiko client: built from source with Go, run as ``taiko-client`` in group ``eth``."""

from __future__ import annotations

from dataclasses import dataclass

from tundravm.declarative import Build, File, Fragment, Git, Install, Package, Unit, User
from tundravm.declarative.utils import Composite

TAIKO_CLIENT_SOURCE = Git(
    "https://github.com/NethermindEth/surge-taiko-mono",
    "feat/tdx-proving",
    subdir="packages/taiko-client",
)

TAIKO_CLIENT_BUILD_PACKAGES = ("golang", "git", "build-essential")

# The CGO flags are a command prefix (not exported) so the portable blst build
# sees them only for ``go build``.
TAIKO_CLIENT_BUILD = (
    'GO111MODULE=on CGO_CFLAGS="-O -D__BLST_PORTABLE__" '
    'CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" '
    'go build -trimpath -ldflags "-s -w -buildid=" -o bin/taiko-client cmd/main.go'
)

TAIKO_CLIENT_UNIT = """\
[Unit]
Description=Taiko Client

[Service]
User=taiko-client
Group=eth
Restart=on-failure
ExecStart=/usr/bin/taiko-client

[Install]
WantedBy=default.target
"""

TAIKO_CLIENT_ENV = """\
TAIKO_CLIENT_CONFIG=/etc/taiko-client/config.json"""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaikoClient(Composite):
    """The taiko client from *source* (``cmd/main.go`` of its subdirectory).

    The ``eth`` group the user joins is the recipe's to declare.
    """

    source: Git = TAIKO_CLIENT_SOURCE

    def compose(self) -> Fragment:
        return Fragment(
            "taiko-client",
            items=(
                *(Package(name, role="build") for name in TAIKO_CLIENT_BUILD_PACKAGES),
                Build(
                    "taiko-client",
                    self.source,
                    script=TAIKO_CLIENT_BUILD,
                    install=(Install("bin/taiko-client", "/usr/bin/taiko-client"),),
                    cache_key=f"taiko-client-{self.source.ref}",
                ),
                User("taiko-client", home="/home/taiko-client", groups=("eth",)),
                Unit("taiko-client.service", TAIKO_CLIENT_UNIT, enabled=True, after_init=True),
                File("/etc/taiko-client/env", TAIKO_CLIENT_ENV),
            ),
        )
