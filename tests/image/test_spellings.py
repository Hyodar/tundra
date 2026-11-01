"""The public spellings of a packaged unit, a second apt archive and a build-time command:
what each refuses, and the files one image using all three compiles to, compared with
``spellings.golden`` beside this file (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

import tundravm
from tundravm import AptRepository, Debian, Image, Service
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.systemd import DropIn, Hardening, Notify, TcpProbe

GOLDEN = Path(__file__).with_name("spellings.golden")
SHOWN = ("prometheus-node-exporter", "ssh.service", "sources.list.d", "keyrings", "sync")
DEBIAN = "https://snapshot.debian.org/archive/debian-security"
KEYRING = "/usr/share/keyrings/debian-archive-keyring.gpg"
PIN = "0" * 64
"""The compiled paths the golden records: whatever names one of these."""


@pytest.mark.parametrize(
    ("make", "refusal"),
    [
        (lambda: Service("chrony", ["/usr/bin/x"], package="chrony"), "its own ExecStart="),
        (lambda: Service.packaged("chrony.service", package="X Y"), "is not a package name"),
        (lambda: Service("chrony", (), package="chrony", user="root"), "user= does not apply"),
        (lambda: Service("chrony", (), package="chrony", quotes=True), "quotes= does not apply"),
        (lambda: Service.packaged("chrony", readiness=Notify()), "own Type="),  # type: ignore[arg-type]
        (lambda: Service.packaged("chrony", hardening=Hardening.none()), "a sandbox or not at all"),
        (
            lambda: Service.packaged("chrony", unit=DropIn(hardening=Hardening.relaxed())),
            "Pass it as hardening=",
        ),
        (lambda: AptRepository(DEBIAN, "trixie-security", key="key.gpg"), "neither a host"),
        (lambda: AptRepository("http://x.org", "s", key=KEYRING, sha256=PIN), "https://"),
        (lambda: AptRepository("https://x.org [trusted=yes]", "s", key=KEYRING), "options"),
        (lambda: AptRepository("https://x.org", "s", key=KEYRING), "is not pinned"),
        (
            lambda: AptRepository("https://x.org", "s", key=KEYRING, snapshot="20261018T082309Z"),
            "dates a snapshot",
        ),
        (lambda: AptRepository(DEBIAN, "s", key=KEYRING, sha256=PIN), "pin it twice"),
        (lambda: AptRepository(DEBIAN, "s", key=KEYRING, snapshot="today"), "not a date"),
        (
            lambda: AptRepository(
                f"{DEBIAN}/20261018T082309Z", "s", key=KEYRING, snapshot="20261018T082309Z"
            ),
            "names its date",
        ),
        (lambda: Debian(repositories=[AptRepository(DEBIAN, "s", key=KEYRING)] * 2), "twice"),
    ],
)
def test_each_spelling_refuses_what_it_cannot_keep(
    make: Callable[[], object], refusal: str
) -> None:
    with pytest.raises(ValidationError, match=refusal):
        make()


def test_one_image_using_every_spelling_compiles_to_the_golden(tmp_path: Path) -> None:
    exporter = Service.packaged(
        "prometheus-node-exporter.service", expose=[TCP(9100)], readiness=TcpProbe(9100)
    )
    (tmp_path / "vendor.asc").write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
    vendor = AptRepository(
        "https://apt.example.com/debian", "stable", key=tmp_path / "vendor.asc", sha256=PIN
    )
    image = Image(
        "spellings",
        base=Debian(repositories=[AptRepository(DEBIAN, "trixie-security", key=KEYRING), vendor]),
        services=[
            exporter,
            Service("app", ["/usr/bin/app"], requires=[exporter.ready]),
            Service.packaged(
                "ssh.service",
                package="openssh-server",
                hardening="relaxed",
                unit=DropIn(env={"SSHD_OPTS": "-e"}),
            ),
        ],
    )
    tundravm.compile(image).write(tmp_path / "tree")
    tmp_path = tmp_path / "tree"
    shown = [
        f"== {path.relative_to(tmp_path)}\n{path.read_text(encoding='utf-8')}"
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and any(name in str(path.relative_to(tmp_path)) for name in SHOWN)
    ]
    postinst = (tmp_path / "payload/mkosi.postinst.d/50-postinst.sh").read_text(encoding="utf-8")
    shown.append(
        "== enabled\n"
        + "".join(
            f"{line}\n" for line in postinst.splitlines() if any(name in line for name in SHOWN)
        )
    )
    text = "".join(shown)
    if os.environ.get("TUNDRAVM_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")
