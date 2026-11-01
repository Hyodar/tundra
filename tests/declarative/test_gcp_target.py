"""A ``gcp`` target: the GCP glue in the payload, inert off GCE, and the tar.gz wrapper."""

from pathlib import Path

import pytest

from tundravm._platforms.gcp import (
    GCP_RESOLV_CONF,
    GCP_RESOLV_CONF_PATH,
)
from tundravm.declarative import Recipe, Variant
from tundravm.testing._model import CompiledTree, compile_tree

RESOLV_CONF = "mkosi.extra/etc/resolv.conf"
SHIPPED_RESOLV_CONF = f"mkosi.extra{GCP_RESOLV_CONF_PATH}"
DNS_UNIT = "mkosi.extra/usr/lib/systemd/system/gcp-metadata-dns.service"
RECIPE = Recipe(
    "cloud",
    variants=(Variant("default", outputs=("qemu",)), Variant("gcp", outputs=("gcp",))),
)


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(RECIPE, path=tmp_path / "tree")


def test_gcp_variant_applies_metadata_dns_at_boot_on_gcp_only(tree: CompiledTree) -> None:
    """No static /etc/resolv.conf: it would break DNS where the same UKI boots off GCP."""
    assert RESOLV_CONF not in tree.files(profile="gcp")
    assert tree.read(SHIPPED_RESOLV_CONF, profile="gcp") == GCP_RESOLV_CONF
    unit = tree.read(DNS_UNIT, profile="gcp")
    assert "ConditionVirtualization=google" in unit
    assert f"ExecStart=/usr/bin/cp {GCP_RESOLV_CONF_PATH} /etc/resolv.conf" in unit
    assert "Before=network-pre.target network-setup.service" in unit
