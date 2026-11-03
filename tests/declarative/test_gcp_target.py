"""A ``gcp`` target: the GCP glue in the payload, inert off GCE, and the tar.gz wrapper."""

import hashlib
from pathlib import Path

import pytest

from tundravm._platforms.gcp import (
    DISK_RULES_PATH,
    NVME_HOST_IDS,
    NVME_ID_PATH,
    NVME_MASKED_UNITS,
)
from tundravm.declarative import Recipe, Variant
from tundravm.testing._model import CompiledTree, compile_tree

DNS_UNIT = "mkosi.extra/usr/lib/systemd/system/gcp-metadata-dns.service"
RECIPE = Recipe(
    "cloud",
    variants=(Variant("default", outputs=("qemu",)), Variant("gcp", outputs=("gcp",))),
)


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(RECIPE, path=tmp_path / "tree")


def test_gcp_variant_leaves_dns_to_tee_base(tree: CompiledTree) -> None:
    """The metadata resolver speaks plain DNS only, which the host reads and forges."""
    assert DNS_UNIT not in tree.files(profile="gcp")


@pytest.mark.parametrize(
    "unit",
    [
        "gcp-hostname.service",
        "gcp-forwarded-ips.service",
        "gcp-forwarded-ips.timer",
    ],
)
def test_every_gcp_unit_is_inert_off_gce(tree: CompiledTree, unit: str) -> None:
    assert "ConditionVirtualization=google" in tree.unit(unit, profile="gcp")


def test_disk_naming_is_googles_own_at_the_pinned_commit(tree: CompiledTree) -> None:
    """Byte for byte guest-configs at GUEST_CONFIGS_COMMIT (by blob id), with the tool its
    NVMe helper runs and without the random NVMe host ids nvme-cli writes."""
    blobs = {
        DISK_RULES_PATH: "884ec2199930a8850c6d6281b85b0a05ccfd7e06",
        NVME_ID_PATH: "960e795d6e7ca8c33ea2e8ed8bff2cc8cca7b566",
    }
    for path, blob in blobs.items():
        data = tree.read(f"mkosi.extra{path}", profile="gcp").encode()
        assert hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == blob
    assert "nvme-cli" in tree.conf(profile="gcp")
    postinst = tree.read("mkosi.postinst.d/50-postinst.sh", profile="gcp")
    assert NVME_HOST_IDS in postinst
    assert " ".join(NVME_MASKED_UNITS) in postinst.partition("systemctl mask ")[2]
