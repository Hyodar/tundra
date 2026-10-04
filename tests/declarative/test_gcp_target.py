"""A ``gcp`` target variant gets the GCP platform integration."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm.declarative import Fragment, Recipe, Variant, compile, resolve
from tundravm.platforms.gcp import (
    GCE_DISK_NAMING_RULES,
    GCP_HOSTS,
    GCP_RESOLV_CONF,
    GOOGLE_NVME_ID,
)
from tundravm.testing import CompiledTree, compile_tree

HOSTS = "mkosi.extra/etc/hosts"
RESOLV_CONF = "mkosi.extra/etc/resolv.conf"
UDEV_RULES = "mkosi.extra/usr/lib/udev/rules.d/65-gce-disk-naming.rules"
NVME_ID = "mkosi.extra/usr/lib/udev/google_nvme_id"
RECIPE = Recipe(
    "cloud",
    Fragment("common"),
    variants=(Variant("default", target="qemu"), Variant("gcp", target="gcp")),
)


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(RECIPE, path=tmp_path / "tree")


def test_gcp_variant_adds_udev_package(tree: CompiledTree) -> None:
    assert "udev" in conf_list(tree.conf(profile="gcp"), "Packages")


def test_gcp_variant_emits_hosts_file(tree: CompiledTree) -> None:
    assert HOSTS in tree.files(profile="gcp")
    assert tree.read(HOSTS, profile="gcp") == GCP_HOSTS


def test_gcp_hosts_content() -> None:
    """Verify hosts file contains localhost and GCP metadata entries."""
    assert "127.0.0.1 localhost" in GCP_HOSTS
    assert "169.254.169.254 metadata.google.internal metadata" in GCP_HOSTS


def test_gcp_variant_emits_resolv_conf(tree: CompiledTree) -> None:
    assert RESOLV_CONF in tree.files(profile="gcp")
    assert tree.read(RESOLV_CONF, profile="gcp") == GCP_RESOLV_CONF


def test_gcp_resolv_conf_content() -> None:
    """Verify resolv.conf has GCP DNS settings."""
    assert "nameserver 169.254.169.254" in GCP_RESOLV_CONF
    assert "options edns0 trust-ad" in GCP_RESOLV_CONF


def test_gcp_variant_emits_udev_rules(tree: CompiledTree) -> None:
    assert UDEV_RULES in tree.files(profile="gcp")
    assert tree.read(UDEV_RULES, profile="gcp") == GCE_DISK_NAMING_RULES


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
    assert NVME_ID in tree.files(profile="gcp")
    modes = {entry.path: entry.mode for entry in compile(RECIPE, variants=["gcp"]).entries}
    assert modes[f"gcp/{NVME_ID}"] == 0o755
    assert tree.read(NVME_ID, profile="gcp") == GOOGLE_NVME_ID


def test_google_nvme_id_content() -> None:
    """Verify NVMe ID helper reads serial from sysfs."""
    assert "#!/bin/bash" in GOOGLE_NVME_ID
    assert "/sys/class/block/" in GOOGLE_NVME_ID
    assert "serial" in GOOGLE_NVME_ID


def test_gcp_variant_sets_output_target(tree: CompiledTree) -> None:
    assert resolve(RECIPE, variant="gcp").target == "gcp"
    assert "PostOutputScripts=scripts/gcp-postoutput.sh" in tree.conf(profile="gcp").splitlines()
    assert tree.exists("scripts/gcp-postoutput.sh", profile="gcp")


def test_gcp_variant_does_not_affect_default_variant(tree: CompiledTree) -> None:
    assert "udev" not in conf_list(tree.conf(profile="default"), "Packages")
    default_files = tree.files(profile="default")
    assert HOSTS not in default_files
    assert RESOLV_CONF not in default_files
