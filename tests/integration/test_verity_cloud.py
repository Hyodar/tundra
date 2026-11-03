"""The verity layout's root and hash partitions survive both cloud converters.

CI's ``verity-cloud`` job builds ``tests/fixtures/native-tree/fixture.py``'s ``verity``, whose
``targets=`` name ``azure`` and ``gcp``, and points ``TUNDRAVM_VERITY_CLOUD_BUILD`` at the build
directory: its ``azure.vhd`` (a fixed VHD: the raw disk and a footer) and ``gcp.tar.gz`` (a
sparse ``disk.raw``) must each hold the ESP, then the root and its hash partition under the
UUIDs the UKI names, with exactly the bytes of the payload's split partitions. Skipped
without that build (the GCE import itself is not run here).
Run with: TUNDRAVM_VERITY_CLOUD_BUILD=BUILD uv run pytest tests/integration/test_verity_cloud.py
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path
from typing import IO

import pytest

from tundravm.compiler.emit_mkosi import DEFAULT_SEED, verity_uuids

from .support import ESP_TYPE, ROOT_X86_64_TYPE, ROOT_X86_64_VERITY_TYPE, read_gpt

pytestmark = [pytest.mark.integration]

BUILD = os.environ.get("TUNDRAVM_VERITY_CLOUD_BUILD")
CHUNK = 1 << 20


@pytest.fixture
def build() -> Path:
    if not BUILD:
        pytest.skip("TUNDRAVM_VERITY_CLOUD_BUILD names no verity build with cloud targets")
    return Path(BUILD)


def _digest(stream: IO[bytes], size: int) -> str:
    """The sha256 of the next *size* bytes of *stream*, read a chunk at a time."""
    digest = hashlib.sha256()
    while size > 0:
        block = stream.read(min(CHUNK, size))
        assert block, "the disk ends inside a partition"
        digest.update(block)
        size -= len(block)
    return digest.hexdigest()


def _assert_carries_the_payload(disk: IO[bytes], output: Path) -> None:
    """*disk* (positioned at LBA 0, forward-readable) holds the ESP, root and root-verity,
    the last two byte for byte the payload's ``payload.root.raw``/``payload.root-verity.raw``."""
    data, hashes = verity_uuids(DEFAULT_SEED)
    table = read_gpt(disk)
    assert [p.type for p in table] == [ESP_TYPE, ROOT_X86_64_TYPE, ROOT_X86_64_VERITY_TYPE]
    position = 34 * 512  # what read_gpt consumed
    for part, split, identifier in zip(
        table[1:], ("payload.root.raw", "payload.root-verity.raw"), (data, hashes), strict=True
    ):
        assert str(part.uuid) == identifier
        want = output / split
        assert part.size >= want.stat().st_size
        _digest(disk, part.offset - position)  # skip forward: a gzip stream seeks slowly
        with want.open("rb") as source:
            assert _digest(disk, want.stat().st_size) == _digest(source, want.stat().st_size)
        position = part.offset + want.stat().st_size


def test_azure_vhd_carries_the_verity_partitions(build: Path) -> None:
    with (build / "azure.vhd").open("rb") as disk:
        _assert_carries_the_payload(disk, build)


def test_gcp_archive_carries_the_verity_partitions(build: Path) -> None:
    with tarfile.open(build / "gcp.tar.gz", "r:gz") as archive:
        member = archive.getmember("disk.raw")
        disk = archive.extractfile(member)
        assert disk is not None
        _assert_carries_the_payload(disk, build)
