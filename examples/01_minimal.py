"""The smallest useful image: one package, one file, one service.

Teaches: an ``Image`` is a name and what it runs. Everything a confidential image
needs is implied — Debian trixie from the locked snapshot, a measured UKI, NTS time, a
RAM-only journal, a firewall that opens only what a service exposes, one hardened
system user per service, and the evidence service. ``Image``'s docstring lists each
implied item with its one escape hatch. With no ``profiles`` it builds one
``production`` profile for QEMU.

    tundravm show examples/01_minimal.py
    tundravm build examples/01_minimal.py --tree-only build/minimal/mkosi
    tundravm build examples/01_minimal.py
"""

from tundravm import File, Image, Service
from tundravm.ports import TCP

image = Image(
    "minimal",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>minimal</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
