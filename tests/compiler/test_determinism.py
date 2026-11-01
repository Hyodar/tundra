"""What the tree normalizes for reproducible images (docs/guide.md, "The mkosi tree").

The payload's finalize ends with the deterministic accounts step; repart builds every
disk from the tree's fixed ``Seed=``; the azure and gcp wrappers' postoutput converters
grow the GPT disk to the size the cloud wants and pin what their tools stamp (the VHD
footer's clock and id, the tarball's owners, dates and gzip header).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import textwrap
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tundravm.backends.base import collect_artifacts, drop_intermediates
from tundravm.compiler.emit_mkosi import ACCOUNTS_FINALIZE, AZURE_CONVERTER, GCP_CONVERTER
from tundravm.declarative import Debian, Package, Recipe, Variant, compile

SECTOR = 512


def _tree(tmp_path: Path, **platform: object) -> Path:
    recipe = Recipe(
        "det",
        platform=Debian("trixie", **platform),  # type: ignore[arg-type]
        items=(Package("curl"),),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", inherits="default", outputs=("azure",)),
            Variant("gcp", inherits="default", outputs=("gcp",)),
        ),
    )
    compile(recipe).write(tmp_path / "mkosi")
    return tmp_path / "mkosi"


def test_a_non_reproducible_build_skips_the_accounts_step(tmp_path: Path) -> None:
    tree = _tree(tmp_path, epoch=None)
    finalize = (tree / "payload/mkosi.finalize.d/50-finalize.sh").read_text()
    assert "tundravm_sort" not in finalize
    assert "\nSourceDateEpoch=0\n" in (tree / "mkosi.conf").read_text()


@pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")
def test_the_accounts_step_sorts_and_drops_backups(tmp_path: Path) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "passwd").write_text(
        "root:x:0:0::/root:/bin/sh\napp:x:1000:1000::/:/bin/sh\n"
        "daemon:x:1:1::/:/bin/sh\nsvc:x:999:999::/:/bin/sh\n"
    )
    (etc / "shadow").write_text("app:!:1::::::\nzz:!:1::::::\nroot:*:1::::::\nsvc:!:1::::::\n")
    (etc / "shadow").chmod(0o640)
    (etc / "group").write_text("app:x:1000:\nroot:x:0:\n")
    (etc / "gshadow").write_text("app:!::\nroot:*::\n")
    (etc / "subuid").write_text("app:165536:65536\nadm:100000:65536\n")
    for backup in ("passwd-", "group-", "shadow-", "gshadow-"):
        (etc / backup).write_text("old\n")
    (etc / "keep-").mkdir()
    script = "set -euo pipefail\n" + ACCOUNTS_FINALIZE
    env = {**os.environ, "BUILDROOT": str(tmp_path)}
    subprocess.run(["bash", "-c", script], env=env, check=True)

    assert (etc / "passwd").read_text().split("\n")[:4] == [
        "root:x:0:0::/root:/bin/sh",
        "daemon:x:1:1::/:/bin/sh",
        "svc:x:999:999::/:/bin/sh",
        "app:x:1000:1000::/:/bin/sh",
    ]
    # shadow follows passwd; entries without an account go last, by name.
    assert [line.split(":")[0] for line in (etc / "shadow").read_text().split()] == [
        "root",
        "svc",
        "app",
        "zz",
    ]
    assert (etc / "shadow").stat().st_mode & 0o777 == 0o640
    assert (etc / "group").read_text() == "root:x:0:\napp:x:1000:\n"
    assert (etc / "gshadow").read_text() == "root:*::\napp:!::\n"
    assert (etc / "subuid").read_text() == "adm:100000:65536\napp:165536:65536\n"
    assert sorted(p.name for p in etc.iterdir()) == [
        "group",
        "gshadow",
        "keep-",
        "passwd",
        "shadow",
        "subuid",
    ]


FAKE_QEMU_IMG = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Like qemu-img convert -O vpc: the raw disk plus a footer with a clock and a random id.
    import os, shutil, struct, sys, time
    src, dst = sys.argv[-2:]
    shutil.copyfile(src, dst)
    footer = bytearray(512)
    footer[:8] = b"conectix"
    footer[24:28] = struct.pack(">I", int(time.time()) - 946684800)
    footer[68:84] = os.urandom(16)
    footer[64:68] = struct.pack(">I", ~sum(footer) & 0xFFFFFFFF)
    open(dst, "ab").write(footer)
""")


def _disk(path: Path) -> None:
    """A small GPT disk with one ESP, sized like repart leaves it (not a whole MiB)."""
    path.write_bytes(bytes(3 * 1024 * 1024 + 17 * SECTOR))
    subprocess.run(
        ["sgdisk", "--disk-guid=12345678-1234-5678-1234-567812345678", "-n", "1:2048:+1M",
         "-t", "1:ef00", "-u", "1:87654321-4321-8765-4321-876543218765", str(path)],
        check=True,
        capture_output=True,
    )  # fmt: skip


def _check_gpt(path: Path) -> None:
    """Both GPT headers are where their fields say, with valid CRCs, at the new end."""
    data = path.read_bytes()
    last = len(data) // SECTOR - 1
    for lba, other in ((1, last), (last, 1)):
        header = bytearray(data[lba * SECTOR : lba * SECTOR + 92])
        assert header[:8] == b"EFI PART"
        mine, alternate = struct.unpack_from("<QQ", header, 24)
        assert (mine, alternate) == (lba, other)
        crc = struct.unpack_from("<I", header, 16)[0]
        header[16:20] = bytes(4)
        assert zlib.crc32(header) == crc
        entries = struct.unpack_from("<Q", header, 72)[0]
        count, size = struct.unpack_from("<II", header, 80)
        table = data[entries * SECTOR : entries * SECTOR + count * size]
        assert zlib.crc32(table) == struct.unpack_from("<I", header, 88)[0]
    verified = subprocess.run(["sgdisk", "-v", str(path)], capture_output=True, text=True)
    assert "No problems found" in verified.stdout


@pytest.mark.skipif(not shutil.which("sgdisk"), reason="needs sgdisk to make the test disk")
def test_the_converters_reproduce_their_outputs(tmp_path: Path) -> None:
    """qemu-img is faked (a random footer): the GPT grow, footer patch and tar.gz are real.
    mkosi stats its own ``azure.raw``/``gcp.raw`` after the postoutput scripts, so both stay
    in place, byte for byte; the bake removes them once it collected the artifacts."""
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "qemu-img").write_text(FAKE_QEMU_IMG)
    (tools / "qemu-img").chmod(0o755)
    _disk(tmp_path / "disk.raw")
    disk = (tmp_path / "disk.raw").read_bytes()
    runs = {run: tmp_path / run for run in ("one", "two")}
    for out in runs.values():
        out.mkdir()
        (out / "azure.raw").write_bytes(disk)
        (out / "gcp.raw").write_bytes(disk)
    raws = {name: hashlib.sha256(disk).hexdigest() for name in ("azure.raw", "gcp.raw")}

    def convert(out: Path, script: str) -> None:
        env = {
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "OUTPUTDIR": str(out),
            "SOURCE_DATE_EPOCH": "0",
        }
        subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)

    with ThreadPoolExecutor() as pool:
        jobs = [
            pool.submit(convert, out, script)
            for out in runs.values()
            for script in (AZURE_CONVERTER, GCP_CONVERTER)
        ]
        for job in jobs:
            job.result()
    digests = []
    for run, out in runs.items():
        assert sorted(p.name for p in out.iterdir()) == [
            "azure.raw",
            "azure.vhd",
            "gcp.raw",
            "gcp.tar.gz",
        ]
        assert {name: _sha_file(out / name) for name in raws} == raws
        digests.append([_sha_file(out / name) for name in ("azure.vhd", "gcp.tar.gz")])
        vhd = (out / "azure.vhd").read_bytes()
        assert (len(vhd) - 512) % (1024 * 1024) == 0
        raw = tmp_path / f"{run}.raw"
        raw.write_bytes(vhd[:-512])
        _check_gpt(raw)
        listing = subprocess.run(
            ["tar", "-tvzf", str(out / "gcp.tar.gz")], capture_output=True, text=True, check=True
        )
        assert listing.stdout.split()[:3] == ["-rw-r--r--", "0/0", "1073741824"]
    assert digests[0] == digests[1]


def test_the_bake_drops_the_wrappers_raw_disks(tmp_path: Path) -> None:
    """After collecting, the published output keeps the artifacts and the UKI only."""
    for name in ("payload.efi", "qemu.raw", "azure.raw", "azure.vhd", "gcp.raw", "gcp.tar.gz"):
        (tmp_path / name).write_bytes(b"x")
    artifacts = collect_artifacts(tmp_path, ("qemu", "azure", "gcp"))
    assert drop_intermediates(artifacts) == [tmp_path / "azure.raw", tmp_path / "gcp.raw"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "azure.vhd",
        "gcp.tar.gz",
        "payload.efi",
        "qemu.raw",
    ]


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
