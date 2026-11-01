"""The public spellings of a packaged unit, a second apt archive and a build-time command:
what each refuses, and the files one image using all three compiles to, compared with
``spellings.golden`` beside this file (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

import tundravm
from tundravm import Image, Service
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.systemd import DropIn, Hardening, Notify, TcpProbe

GOLDEN = Path(__file__).with_name("spellings.golden")
SHOWN = ("prometheus-node-exporter", "ssh.service")
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
    image = Image(
        "spellings",
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
    tundravm.compile(image).write(tmp_path)
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
