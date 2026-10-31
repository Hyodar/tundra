"""A ``gcp`` target: the GCP glue in the payload, inert off GCE, and the tar.gz wrapper."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm._platforms.gcp import (
    GCE_DISK_NAMING_RULES,
    GCP_HOSTS,
    GCP_RESOLV_CONF,
    GCP_RESOLV_CONF_PATH,
    GOOGLE_NVME_ID,
)
from tundravm.compiler import GCP_CONVERTER
from tundravm.declarative import Recipe, Variant, compile, resolve
from tundravm.testing import CompiledTree, compile_tree

HOSTS = "mkosi.extra/etc/hosts"
RESOLV_CONF = "mkosi.extra/etc/resolv.conf"
SHIPPED_RESOLV_CONF = f"mkosi.extra{GCP_RESOLV_CONF_PATH}"
DNS_UNIT = "mkosi.extra/usr/lib/systemd/system/gcp-metadata-dns.service"
UDEV_RULES = "mkosi.extra/usr/lib/udev/rules.d/65-gce-disk-naming.rules"
NVME_ID = "mkosi.extra/usr/lib/udev/google_nvme_id"
RECIPE = Recipe(
    "cloud",
    variants=(Variant("default", outputs=("qemu",)), Variant("gcp", outputs=("gcp",))),
)


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(RECIPE, path=tmp_path / "tree")


def test_gcp_variant_adds_udev_package(tree: CompiledTree) -> None:
    assert "udev" in conf_list(tree.conf(variant="gcp"), "Packages")


def test_gcp_variant_emits_hosts_file(tree: CompiledTree) -> None:
    assert HOSTS in tree.files(variant="gcp")
    assert tree.read(HOSTS, variant="gcp") == GCP_HOSTS


def test_gcp_hosts_content() -> None:
    """Verify hosts file contains localhost and GCP metadata entries."""
    assert "127.0.0.1 localhost" in GCP_HOSTS
    assert "169.254.169.254 metadata.google.internal metadata" in GCP_HOSTS


def test_gcp_variant_applies_metadata_dns_at_boot_on_gcp_only(tree: CompiledTree) -> None:
    """No static /etc/resolv.conf: it would break DNS where the same UKI boots off GCP."""
    assert RESOLV_CONF not in tree.files(variant="gcp")
    assert tree.read(SHIPPED_RESOLV_CONF, variant="gcp") == GCP_RESOLV_CONF
    unit = tree.read(DNS_UNIT, variant="gcp")
    assert "ConditionVirtualization=google" in unit
    assert f"ExecStart=/usr/bin/cp {GCP_RESOLV_CONF_PATH} /etc/resolv.conf" in unit
    assert "Before=network-pre.target network-setup.service" in unit


def test_gcp_resolv_conf_content() -> None:
    """Verify resolv.conf has GCP DNS settings."""
    assert "nameserver 169.254.169.254" in GCP_RESOLV_CONF
    assert "options edns0 trust-ad" in GCP_RESOLV_CONF


def test_gcp_variant_emits_udev_rules(tree: CompiledTree) -> None:
    assert UDEV_RULES in tree.files(variant="gcp")
    assert tree.read(UDEV_RULES, variant="gcp") == GCE_DISK_NAMING_RULES


def test_gce_disk_naming_rules_content() -> None:
    """Verify udev rules cover SCSI and NVMe disk types."""
    # SCSI persistent disks
    assert 'KERNEL=="sd*[!0-9]"' in GCE_DISK_NAMING_RULES
    assert "scsi_id" in GCE_DISK_NAMING_RULES
    # NVMe persistent disks
    assert 'KERNEL=="nvme*n*"' in GCE_DISK_NAMING_RULES
    assert "google_nvme_id" in GCE_DISK_NAMING_RULES
    # Partition rules
    assert "part%n" in GCE_DISK_NAMING_RULES
    # Local SSDs
    assert "google-local-ssd" in GCE_DISK_NAMING_RULES
    assert "google-local-nvme-ssd" in GCE_DISK_NAMING_RULES


def test_gcp_variant_emits_nvme_id_script(tree: CompiledTree) -> None:
    assert NVME_ID in tree.files(variant="gcp")
    modes = {entry.path: entry.mode for entry in compile(RECIPE, variants=["gcp"]).entries}
    assert modes[f"payload/{NVME_ID}"] == 0o755
    assert tree.read(NVME_ID, variant="gcp") == GOOGLE_NVME_ID


def test_google_nvme_id_content() -> None:
    """Verify NVMe ID helper reads serial from sysfs."""
    assert "#!/bin/bash" in GOOGLE_NVME_ID
    assert "/sys/class/block/" in GOOGLE_NVME_ID
    assert "serial" in GOOGLE_NVME_ID


def test_gcp_variant_builds_the_tar_wrapper(tree: CompiledTree) -> None:
    assert resolve(RECIPE, variant="gcp").outputs == ("gcp",)
    wrapper = tree.root / "mkosi.images/gcp"
    conf = (wrapper / "mkosi.conf").read_text()
    assert "Dependencies=payload\n" in conf and "Output=gcp\n" in conf
    assert (wrapper / "mkosi.postoutput.d/tar").read_text() == GCP_CONVERTER
    assert "--format=oldgnu" in GCP_CONVERTER and "gzip -n" in GCP_CONVERTER
    # gcp only adds the wrapper: no profile overlay
    assert tree.layers("gcp") == tree.layers("default")


def test_the_default_variant_boots_the_same_payload_with_the_glue(tree: CompiledTree) -> None:
    """The glue is inert off GCE, so the QEMU disk carries the same UKI as the GCP image."""
    assert "udev" in conf_list(tree.conf(variant="default"), "Packages")
    default_files = tree.files(variant="default")
    assert HOSTS in default_files and DNS_UNIT in default_files
    assert RESOLV_CONF not in default_files


def test_a_recipe_without_the_gcp_target_carries_no_gcp_glue(tmp_path: Path) -> None:
    tree = compile_tree(Recipe("plain"), path=tmp_path / "tree")
    assert DNS_UNIT not in tree.files() and HOSTS not in tree.files()
    assert not (tree.root / "mkosi.images/gcp").exists()
