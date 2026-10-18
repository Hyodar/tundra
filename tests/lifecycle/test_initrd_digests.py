"""``installed_digests``: the sha256 of files inside a UKI's cpio initrd, any compression."""

from __future__ import annotations

import gzip
import hashlib
import lzma
import shutil
import subprocess
from pathlib import Path

import pytest

from tundravm.declarative._initrd import installed_digests

from .test_measure import build_pe

APP = b"\x7fELF the installed app"
LINK_MODE = 0o120777


def newc(entries: list[tuple[str, bytes, int]]) -> bytes:
    """A cpio ``newc`` archive of *entries* (name, data, mode), with its trailer."""
    out = b""
    for ino, (name, data, mode) in enumerate([*entries, ("TRAILER!!!", b"", 0)], start=1):
        encoded = name.encode() + b"\0"
        fields = (ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0)
        header = b"070701" + b"".join(b"%08x" % f for f in fields)
        out += header + encoded + b"\0" * (-(110 + len(encoded)) % 4)
        out += data + b"\0" * (-len(data) % 4)
    return out


def root(app: bytes = APP) -> bytes:
    return newc(
        [
            ("usr", b"", 0o040755),
            ("usr/bin/app", app, 0o100755),
            ("usr/bin/link", b"app", LINK_MODE),
            ("etc/other", b"x" * 5000, 0o100644),
        ]
    )


def uki(tmp_path: Path, initrd: bytes) -> Path:
    path = tmp_path / "default.efi"
    path.write_bytes(build_pe([(".text", b"stub"), (".initrd", initrd), (".linux", b"k")]))
    return path


def zstd(data: bytes) -> bytes:
    tool = shutil.which("zstd")
    if tool is None:
        pytest.skip("zstd is not installed")
    return subprocess.run([tool, "-q", "-c"], input=data, capture_output=True, check=True).stdout


WANTED = ("/usr/bin/app", "/usr/bin/link", "/usr/bin/absent")
EXPECTED = {"/usr/bin/app": hashlib.sha256(APP).hexdigest()}


@pytest.mark.parametrize(
    "pack",
    [lambda d: d, gzip.compress, lzma.compress, zstd],
    ids=["none", "gzip", "xz", "zstd"],
)
def test_hashes_the_regular_files_asked_for(tmp_path: Path, pack: object) -> None:
    assert callable(pack)
    assert installed_digests(uki(tmp_path, pack(root())), WANTED) == EXPECTED


def test_an_early_cpio_then_a_compressed_root(tmp_path: Path) -> None:
    early = newc([("kernel/x86/microcode/GenuineIntel.bin", b"ucode", 0o100644)])
    padded = early + b"\0" * (-len(early) % 512)
    assert installed_digests(uki(tmp_path, padded + gzip.compress(root())), WANTED) == EXPECTED


def test_a_later_archive_overrides_an_earlier_path(tmp_path: Path) -> None:
    later = newc([("./usr/bin/app", b"replaced", 0o100755)])
    found = installed_digests(uki(tmp_path, root() + later), WANTED)
    assert found == {"/usr/bin/app": hashlib.sha256(b"replaced").hexdigest()}


def test_unreadable_initrds_record_nothing(tmp_path: Path) -> None:
    text = tmp_path / "simulated.efi"
    text.write_text("tdx-uki: profile=default\n", encoding="utf-8")
    assert installed_digests(text, WANTED) is None
    assert installed_digests(uki(tmp_path, b"not a cpio archive"), WANTED) is None
    assert installed_digests(uki(tmp_path, root()[:300]), WANTED) is None
    no_initrd = tmp_path / "bare.efi"
    no_initrd.write_bytes(build_pe([(".text", b"stub")]))
    assert installed_digests(no_initrd, WANTED) is None


def test_zstd_without_the_tool_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packed = zstd(root())
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert installed_digests(uki(tmp_path, packed), WANTED) is None
