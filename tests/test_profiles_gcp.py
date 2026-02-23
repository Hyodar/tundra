"""Tests for GCP platform profile helper."""

from tdx import Image
from tdx.platforms.gcp import (
    GCE_DISK_NAMING_RULES,
    GCP_HOSTS,
    GCP_RESOLV_CONF,
    GOOGLE_NVME_ID,
    GcpPlatform,
)


def test_gcp_profile_adds_udev_package() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    profile = image.state.profiles["gcp"]
    assert "udev" in profile.packages


def test_gcp_profile_emits_hosts_file() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    profile = image.state.profiles["gcp"]
    file_paths = {f.path for f in profile.files}
    assert "/etc/hosts" in file_paths

    hosts_entry = next(f for f in profile.files if f.path == "/etc/hosts")
    assert hosts_entry.content == GCP_HOSTS


def test_gcp_hosts_content() -> None:
    """Verify hosts file contains localhost and GCP metadata entries."""
    assert "127.0.0.1 localhost" in GCP_HOSTS
    assert "169.254.169.254 metadata.google.internal metadata" in GCP_HOSTS


def test_gcp_profile_emits_resolv_conf() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    profile = image.state.profiles["gcp"]
    file_paths = {f.path for f in profile.files}
    assert "/etc/resolv.conf" in file_paths

    resolv_entry = next(f for f in profile.files if f.path == "/etc/resolv.conf")
    assert resolv_entry.content == GCP_RESOLV_CONF


def test_gcp_resolv_conf_content() -> None:
    """Verify resolv.conf has GCP DNS settings."""
    assert "nameserver 169.254.169.254" in GCP_RESOLV_CONF
    assert "options edns0 trust-ad" in GCP_RESOLV_CONF


def test_gcp_profile_emits_udev_rules() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    profile = image.state.profiles["gcp"]
    file_paths = {f.path for f in profile.files}
    assert "/usr/lib/udev/rules.d/65-gce-disk-naming.rules" in file_paths

    rules_entry = next(
        f
        for f in profile.files
        if f.path == "/usr/lib/udev/rules.d/65-gce-disk-naming.rules"
    )
    assert rules_entry.content == GCE_DISK_NAMING_RULES


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


def test_gcp_profile_emits_nvme_id_script() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    profile = image.state.profiles["gcp"]
    file_paths = {f.path for f in profile.files}
    assert "/usr/lib/udev/google_nvme_id" in file_paths

    nvme_entry = next(
        f for f in profile.files if f.path == "/usr/lib/udev/google_nvme_id"
    )
    assert nvme_entry.mode == "0755"
    assert nvme_entry.content == GOOGLE_NVME_ID


def test_google_nvme_id_content() -> None:
    """Verify NVMe ID helper reads serial from sysfs."""
    assert "#!/bin/bash" in GOOGLE_NVME_ID
    assert "/sys/class/block/" in GOOGLE_NVME_ID
    assert "serial" in GOOGLE_NVME_ID


def test_gcp_profile_sets_output_target() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    profile = image.state.profiles["gcp"]
    assert "gcp" in profile.output_targets


def test_gcp_profile_does_not_affect_default_profile() -> None:
    image = Image(reproducible=False)

    with image.profile("gcp"):
        GcpPlatform().apply(image)

    default_profile = image.state.profiles["default"]
    assert "udev" not in default_profile.packages
    assert not any(f.path == "/etc/hosts" for f in default_profile.files)
    assert not any(f.path == "/etc/resolv.conf" for f in default_profile.files)
