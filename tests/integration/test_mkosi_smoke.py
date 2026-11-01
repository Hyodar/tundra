"""Integration smoke tests: emit + build the mkosi-native tree via the local_linux backend.

They need a host the Linux backend builds on: x86-64 Linux with user namespaces or sudo.
Run with: uv run pytest tests/integration/test_mkosi_smoke.py -m integration
"""

import re
import struct
import tarfile
import uuid
from pathlib import Path

import pytest

from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import (
    Debian,
    File,
    Package,
    Recipe,
    Service,
    User,
    Variant,
    compile,
    lock,
    lower,
)
from tundravm.declarative.lifecycle import bake_image
from tundravm.declarative.utils import Tdxs
from tundravm.models import BakeResult

SNAPSHOT = "20261018T082309Z"
ESP_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
"""The GPT partition type GUID of an EFI system partition."""
SECTOR = 512


def _bake(recipe: Recipe, variant: str, output_dir: Path) -> BakeResult:
    result, _ = bake_image(
        lower(recipe), (variant,), locked=None, backend=LocalLinuxBackend(), build=output_dir
    )
    return result


def _gpt_partitions(head: bytes) -> list[tuple[uuid.UUID, int, int]]:
    """``(type, first LBA, last LBA)`` of each used GPT entry in a disk's first sectors,
    read the way ``measure/`` reads the GPT."""
    header = head[SECTOR : SECTOR + 92]
    assert header[:8] == b"EFI PART", "no GPT header"
    entries = struct.unpack_from("<Q", header, 72)[0]
    count, size = struct.unpack_from("<II", header, 80)
    table = head[entries * SECTOR : entries * SECTOR + count * size]
    found = []
    for offset in range(0, len(table), size):
        entry = table[offset : offset + size]
        if any(entry[:16]):
            first, last = struct.unpack_from("<QQ", entry, 32)
            found.append((uuid.UUID(bytes_le=entry[:16]), first, last))
    return found


def _recipe() -> Recipe:
    return Recipe(
        "smoke",
        items=(
            File("/etc/tdx-test", "integration-test\n"),
            Service("hello", "/bin/true", restart="no"),
            User("appuser", system=True, shell="/bin/false"),
        ),
        platform=Debian("trixie", snapshot=SNAPSHOT),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("cloud", outputs=("azure", "gcp")),
        ),
    )


@pytest.mark.integration
def test_default_selection_builds_the_payload_and_the_qemu_disk(tmp_path: Path) -> None:
    """The main image is a raw GPT disk with one ESP around ``payload.efi``."""
    result = _bake(_recipe(), "default", tmp_path / "build")

    output = tmp_path / "build" / "default" / "output"
    uki, disk = output / "payload.efi", output / "qemu.raw"
    assert uki.read_bytes()[:2] == b"MZ"
    assert not (output / "azure.vhd").exists() and not (output / "gcp.tar.gz").exists()
    artifact = result.profiles["default"].artifacts["qemu"]
    assert (artifact.path, artifact.boot_payload) == (disk, uki)
    with disk.open("rb") as handle:
        partitions = _gpt_partitions(handle.read(1 << 20))
    assert [kind for kind, _, _ in partitions] == [ESP_TYPE]
    ((_, first, last),) = partitions
    assert (last - first + 1) * SECTOR >= uki.stat().st_size  # the ESP holds the UKI


@pytest.mark.integration
def test_cloud_selection_builds_the_vhd_and_the_gcp_archive(tmp_path: Path) -> None:
    """``--dependency=azure --dependency=gcp``: the wrappers' converted disks, whole MiB and
    whole GiB, each a GPT with one ESP around the same payload."""
    result = _bake(_recipe(), "cloud", tmp_path / "build")

    output = tmp_path / "build" / "cloud" / "output"
    artifacts = result.profiles["cloud"].artifacts
    assert set(artifacts) == {"azure", "gcp"}
    assert {ref.boot_payload for ref in artifacts.values()} == {output / "payload.efi"}
    # mkosi's own raw disks stay through its postoutput step; the bake drops them after
    assert not (output / "azure.raw").exists() and not (output / "gcp.raw").exists()

    vhd = (output / "azure.vhd").read_bytes()
    assert vhd[-512:][:8] == b"conectix" and (len(vhd) - 512) % (1 << 20) == 0
    assert [kind for kind, _, _ in _gpt_partitions(vhd[: 1 << 20])] == [ESP_TYPE]

    with tarfile.open(output / "gcp.tar.gz", "r:gz") as archive:
        (member,) = archive.getmembers()
        assert member.name == "disk.raw" and member.size % (1 << 30) == 0
        assert (member.uid, member.gid, member.mode) == (0, 0, 0o644)
        stream = archive.extractfile(member)
        assert stream is not None
        head = stream.read(1 << 20)
    assert [kind for kind, _, _ in _gpt_partitions(head)] == [ESP_TYPE]


def test_tdxs_fragment_emission(tmp_path: Path) -> None:
    """Emit the tdxs fragment and verify config, units, and build script."""
    recipe = Recipe(
        "smoke",
        items=(Package("systemd"), Tdxs()),
        platform=Debian("bookworm", boot=None),
    )

    emit_dir = tmp_path / "mkosi"
    compile(recipe, lock=lock(recipe, resolver=lambda source: "a" * 40)).write(emit_dir)

    payload = emit_dir / "payload"
    conf_text = "".join(
        path.read_text()
        for path in (payload / "mkosi.conf", *sorted(payload.glob("mkosi.conf.d/*")))
    )
    assert "BuildPackages=" in conf_text
    assert "golang" in conf_text

    config_yaml = payload / "mkosi.extra" / "etc" / "tdxs" / "config.yaml"
    assert config_yaml.exists(), "config.yaml not emitted"
    config_content = config_yaml.read_text()
    assert "type: tdx" in config_content
    assert "systemd: true" in config_content

    svc_unit = payload / "mkosi.extra" / "usr" / "lib" / "systemd" / "system" / "tdxs.service"
    assert svc_unit.exists(), "tdxs.service not emitted"
    svc_text = svc_unit.read_text()
    assert "User=tdxs" in svc_text
    assert "Group=tdx" in svc_text
    assert "Type=notify" in svc_text
    assert "ExecStart=/usr/bin/tdxs" in svc_text
    assert "--log-level info" in svc_text

    sock_unit = payload / "mkosi.extra" / "usr" / "lib" / "systemd" / "system" / "tdxs.socket"
    assert sock_unit.exists(), "tdxs.socket not emitted"
    sock_text = sock_unit.read_text()
    assert "ListenStream=/var/tdxs.sock" in sock_text
    assert "SocketGroup=tdx" in sock_text

    build_scripts = sorted((payload / "mkosi.build.d").glob("*.sh"))
    assert build_scripts, "No build script emitted"
    build_text = build_scripts[0].read_text()
    assert "go build" in build_text
    assert re.search(r'build\}/tdxs-[0-9a-f]{16}"', build_text)
    assert "./cmd/tdxs" in build_text
    assert "sync-constellation" not in build_text
    assert "-trimpath" in build_text

    postinst = payload / "mkosi.postinst.d" / "50-postinst.sh"
    assert postinst.exists(), "postinst not emitted"
    postinst_text = postinst.read_text()
    assert "groupadd --system tdx" in postinst_text
    assert "useradd --system" in postinst_text
    assert "systemctl enable tdxs.socket" in postinst_text
