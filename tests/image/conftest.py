"""The starter every test in this suite reads, and the plan of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm import File, Image, Service
from tundravm.ports import TCP
from tundravm.testing import PlannedImage, plan

STARTER_SOURCE = '''"""The starter: plan section 3.1's demo.py."""

from tundravm import File, Image, Service
from tundravm.ports import TCP

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
'''
"""The starter as a file, for the tests that load it the way the CLI does."""


def starter() -> Image:
    """The same image the starter file binds, built in memory."""
    return Image(
        "demo",
        packages=["busybox"],
        files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
        services=[
            Service(
                "app",
                ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
                expose=[TCP(8080)],
            ),
        ],
    )


@pytest.fixture
def starter_file(tmp_path: Path) -> Path:
    """:data:`STARTER_SOURCE` written to a file."""
    path = tmp_path / "demo.py"
    path.write_text(STARTER_SOURCE, encoding="utf-8")
    return path


@pytest.fixture
def planned() -> PlannedImage:
    """The starter's ``production`` profile, planned in memory."""
    return plan(starter(), "production")
