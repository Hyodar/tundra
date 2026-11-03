"""The mkosi-native tree (gate 1): the fixture tree file by file and as mkosi resolves it.

``tests/fixtures/native-tree/fixture.py`` compiles, with each root layout, to the committed
``tree-initramfs/`` and ``tree-verity/``; ``mkosi --force summary --json`` of each, for the
default selection and for every node (``--profile=dev --dependency=azure
--dependency=gcp``), normalised (tree paths relative, host-dependent keys left out), is the
committed ``summary-<root>.json``. ``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites both after an
intended change; the diff is the review.
"""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest

import tundravm
from tests.helpers import ATTESTED_NODE, EXAMPLE_RECIPES, REPO_ROOT, write_new_image
from tundravm import Image
from tundravm._image import recipe_of
from tundravm.compiler.emit_mkosi import (
    DEFAULT_SEED,
    ESP_MKFS_ENVIRONMENT,
    MAIN_IMAGE_SETTINGS,
    MKOSI_VERSION,
    UKI_MAX_SIZE,
    esp_bytes,
    uki_capacity,
    verity_uuids,
)
from tundravm.declarative import Recipe, compile, lock, lower, mkosi_argv
from tundravm.recipe import load_file
from tundravm.testing import UPDATE_GOLDEN_ENV
from tundravm.testing._model import assert_tree_matches
from tundravm.toolchain import ToolsTree

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "native-tree"
ROOTS = ("initramfs", "verity")
SELECTIONS: dict[str, tuple[str, ...]] = {
    "default": (),
    "every-node": ("--profile=dev", "--dependency=azure", "--dependency=gcp"),
}
"""The ``mkosi`` selections whose summaries are evidence: what ``default`` builds, and every
image of the tree (the dev profile, both cloud wrappers, the payload and the QEMU disk)."""

image_with: Callable[..., Image] = runpy.run_path(str(FIXTURE / "fixture.py"))["image_with"]


def recipe_with(root: str = "initramfs") -> Recipe:
    """The fixture image with *root*, as the internal model it lowers to."""
    return recipe_of(image_with(root))


IMAGE_KEYS = (
    "Image",
    "Format",
    "Output",
    "ImageId",
    "Dependencies",
    "Profiles",
    "MinimumVersion",
    "Distribution",
    "Release",
    "Architecture",
    "Mirror",
    "Snapshot",
    "SourceDateEpoch",
    "Seed",
    "History",
    "WithNetwork",
    "MachineId",
    "Environment",
    "OutputDirectory",
    "BuildSources",
    "SandboxTrees",
    "Bootable",
    "Bootloader",
    "SplitArtifacts",
    "Verity",
    "RepartDirectories",
    "ExtraTrees",
    "SkeletonTrees",
    "KernelCommandLine",
    "KernelModules",
    "Packages",
    "BuildPackages",
    "ManifestFormat",
    "CleanPackageMetadata",
    "ConfigureScripts",
    "SyncScripts",
    "PrepareScripts",
    "BuildScripts",
    "PostInstallationScripts",
    "FinalizeScripts",
    "PostOutputScripts",
    "CleanScripts",
    "Firmware",
)
"""What the evidence keeps of each image the tree defines: identity, composition and every
reproducibility knob; the rest is mkosi's defaults or the host's (CPU count, machine name)."""
FOREIGN_KEYS = ("Image", "Format", "Output", "Dependencies", "Distribution", "Release", "Snapshot")
"""What it keeps of an image mkosi adds itself (``default-initrd`` under the verity layout),
whose seed and resource paths are per run."""
TOOLS_KEYS = ("Image", "Format", "Output", "Distribution", "Release", "Mirror", "Snapshot")
"""What it keeps of the tools tree: where its packages come from."""
OWN_IMAGES = frozenset({"main", "payload", "azure", "gcp"})
_RESOURCES = re.compile(r"/[^\s\"']*/resources/")


def mkosi_summary(tree: Path, *selection: str) -> dict[str, Any]:
    """``mkosi --force summary --json`` of *tree* with *selection*, unprivileged."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkosi",
            "-C",
            str(tree),
            "--force",
            *selection,
            "summary",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    found: dict[str, Any] = json.loads(done.stdout)
    return found


def normalised(summary: Mapping[str, Any], tree: Path) -> dict[str, Any]:
    """*summary* without the host: tree paths as ``<tree>/...``, kept keys only."""
    root = str(tree.resolve())

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return _RESOURCES.sub("<mkosi resources>/", value.replace(root, "<tree>"))
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    images = []
    for image in summary["Images"]:
        keys = IMAGE_KEYS if image["Image"] in OWN_IMAGES else FOREIGN_KEYS
        images.append({key: clean(image.get(key)) for key in keys})
    tools = summary.get("Tools") or {}
    return {"Images": images, "Tools": {key: clean(tools.get(key)) for key in TOOLS_KEYS}}


def by_image(summary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {image["Image"]: image for image in summary["Images"]}


# ── the tree, file by file ──────────────────────────────────────────────


@pytest.mark.parametrize("root", ROOTS)
def test_fixture_compiles_to_the_committed_tree(root: str) -> None:
    assert_tree_matches(recipe_with(root), FIXTURE / f"tree-{root}")


def test_tree_has_the_native_shape() -> None:
    tree = FIXTURE / "tree-initramfs"
    files = sorted(
        p.relative_to(tree).as_posix()
        for p in tree.rglob("*")
        if p.is_file() and "mkosi.extra" not in p.parts and "mkosi.skeleton" not in p.parts
    )
    assert files == [
        "mkosi.conf",
        "mkosi.images/azure/mkosi.conf",
        "mkosi.images/azure/mkosi.postoutput.d/vhd",
        "mkosi.images/azure/mkosi.repart/00-esp.conf",
        "mkosi.images/box/mkosi.conf",
        "mkosi.images/box/mkosi.postinst.d/10-mount-points.sh",
        "mkosi.images/gcp/mkosi.conf",
        "mkosi.images/gcp/mkosi.postoutput.d/tar",
        "mkosi.images/gcp/mkosi.repart/00-esp.conf",
        "mkosi.images/payload/mkosi.conf",
        "mkosi.repart/00-esp.conf",
        "payload/mkosi.build.d/50-build.sh",
        "payload/mkosi.conf",
        "payload/mkosi.conf.d/10-platform.conf",
        "payload/mkosi.conf.d/20-tee-base.conf",
        "payload/mkosi.conf.d/30-greeter.conf",
        "payload/mkosi.conf.d/40-web.conf",
        "payload/mkosi.conf.d/50-tundravm-runtime.conf",
        "payload/mkosi.conf.d/60-firewall.conf",
        "payload/mkosi.conf.d/70-cloud-glue.conf",
        "payload/mkosi.finalize.d/50-finalize.sh",
        "payload/mkosi.postinst.d/50-postinst.sh",
        "payload/mkosi.profiles/dev/mkosi.build.d/50-build.sh",
        "payload/mkosi.profiles/dev/mkosi.conf",
        "payload/mkosi.profiles/dev/mkosi.finalize.d/50-finalize.sh",
        "payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh",
    ]
    for script in (
        "mkosi.images/azure/mkosi.postoutput.d/vhd",
        "payload/mkosi.postinst.d/50-postinst.sh",
    ):
        assert (tree / script).stat().st_mode & 0o111, script


def test_main_image_is_the_qemu_disk_with_every_reproducibility_setting() -> None:
    text = (FIXTURE / "tree-initramfs" / "mkosi.conf").read_text(encoding="utf-8")
    assert text == (
        "[Config]\n"
        f"MinimumVersion={MKOSI_VERSION}\n"
        "Dependencies=payload\n"
        "\n"
        "[Distribution]\n"
        "Distribution=debian\n"
        "Release=trixie\n"
        "Architecture=x86-64\n"
        "Snapshot=20261018T082309Z\n"
        "\n"
        "[Output]\n"
        "Format=disk\n"
        "Output=qemu\n"
        "ImageId=fixture\n"
        "OutputDirectory=mkosi.output\n"
        f"Seed={DEFAULT_SEED}\n"
        "\n"
        "[Build]\n"
        "ToolsTree=default\n"
        "ToolsTreeDistribution=debian\n"
        "ToolsTreeRelease=trixie\n"
        "ToolsTreeSnapshot=20261018T082309Z\n"
        "ToolsTreePackages=qemu-utils\n"
        "History=no\n"
        "WithNetwork=no\n"
        'Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"\n'
        "\n"
        "[Content]\n"
        "SourceDateEpoch=0\n"
        "Bootable=no\n"
        "ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI\n"
        "\n"
        "[Runtime]\n"
        "Firmware=uefi\n"
    )


def test_payload_subimage_includes_the_payload_directory() -> None:
    tree = FIXTURE / "tree-initramfs"
    assert (tree / "mkosi.images/payload/mkosi.conf").read_text(encoding="utf-8") == (
        "[Include]\nInclude=../../payload\n"
    )
    payload = (tree / "payload/mkosi.conf").read_text(encoding="utf-8")
    assert payload.startswith(f"[Output]\nFormat=uki\nOutput=payload\nSeed={DEFAULT_SEED}\n")
    # the account tools: no concern declared them (the ingress's runtime declares openssl)
    assert "Packages=\n    passwd\n" in payload
    assert (tree / "payload/mkosi.conf.d/30-greeter.conf").read_text(encoding="utf-8") == (
        "[Content]\nPackages=\n    curl\n"
    )
    assert (tree / "payload/mkosi.conf.d/40-web.conf").read_text(encoding="utf-8") == (
        "[Content]\nPackages=\n    busybox\n"
    )
    assert (tree / "payload/mkosi.conf.d/70-cloud-glue.conf").read_text(encoding="utf-8") == (
        "[Content]\nPackages=\n    azure-vm-utils\n    dmidecode\n    iproute2\n    nvme-cli\n"
    )
    machine_id = tree / "payload/mkosi.extra/etc/machine-id"
    assert machine_id.read_text(encoding="utf-8") == "uninitialized\n"


def test_cloud_glue_is_in_the_payload_and_gated_at_boot() -> None:
    units = FIXTURE / "tree-initramfs/payload/mkosi.extra/usr/lib/systemd/system"
    assert "ConditionVirtualization=microsoft" in (
        units / "azure-complete-provisioning.service"
    ).read_text(encoding="utf-8")
    assert "ConditionVirtualization=google" in (units / "gcp-hostname.service").read_text(
        encoding="utf-8"
    )
    for conf in (FIXTURE / "tree-initramfs").rglob("*.conf"):
        assert "[Match]" not in conf.read_text(encoding="utf-8"), conf


DEV_UKI = 138881024
"""The fixture's dev UKI (132.4 MiB) when the fixed 128 MiB ESP stopped holding it."""


def _bytes(size: str) -> int:
    return int(size[:-1]) << (20 if size[-1] == "M" else 30)


@pytest.mark.parametrize(
    ("root", "uki_max_size", "esp"),
    [
        ("verity", None, 128),
        ("initramfs", "64M", 128),
        ("initramfs", "126M", 128),
        ("initramfs", "127M", 144),
        ("initramfs", None, 272),
        ("initramfs", "1G", 1056),
    ],
)
def test_the_esp_holds_the_declared_uki_as_a_fat32_by_cluster_count(
    tmp_path: Path, root: Literal["initramfs", "verity"], uki_max_size: str | None, esp: int
) -> None:
    """The ESP is the verity size, or the smallest 16 MiB step from it holding the declared
    UKI, formatted as repart does offline (``mkfs.vfat -F 32 -S 4096`` then the tree's
    options, the last ``-S`` winning): 512-byte sectors and clusters, 65525 clusters or more
    (UEFI's FAT32), and every cluster but three free for the UKI."""
    assert esp_bytes(root, uki_max_size) == esp << 20
    mkfs = shutil.which("mkfs.vfat") or shutil.which("mkfs.fat")
    if mkfs is None:
        pytest.skip("mkfs.vfat is not installed")
    image = tmp_path / "esp.img"
    image.write_bytes(b"")
    os.truncate(image, esp << 20)
    options = ESP_MKFS_ENVIRONMENT.partition("=")[2].partition("=")[2].strip('"').split()
    subprocess.run(
        [mkfs, "-i", "12345678", "-n", "ESP", "-F", "32", "-S", "4096", *options, str(image)],
        check=True,
        capture_output=True,
    )
    boot = image.read_bytes()[:512]
    sector, per_cluster, reserved, fats = struct.unpack_from("<HBHB", boot, 11)
    total, fat_sectors = struct.unpack_from("<II", boot, 32)
    clusters = (total - reserved - fats * fat_sectors) // per_cluster
    assert (sector, per_cluster) == (512, 1)
    assert clusters >= 65525
    assert (clusters - 3) * sector == uki_capacity(esp << 20)
    if root == "initramfs":
        assert uki_capacity(esp << 20) >= _bytes(uki_max_size or UKI_MAX_SIZE)


def test_the_fixture_dev_esp_is_larger_than_the_uki_ceiling_it_declares() -> None:
    """Lowered, the fixture's dev profile (the whole OS in its UKI) gets an ESP whose FAT32
    holds the UKI ceiling it declares, which is above the dev UKI that outgrew 128 MiB."""
    image = image_with()
    tree = tundravm.compile(image, profiles=("dev",))
    conf = next(e.content for e in tree.entries if e.path == "mkosi.repart/00-esp.conf")
    found = re.search(rb"SizeMaxBytes=([0-9]+)M", conf or b"")
    assert found is not None
    esp = int(found.group(1)) << 20
    ceiling = _bytes(image.rootfs.uki_max_size or UKI_MAX_SIZE)
    assert DEV_UKI < ceiling <= uki_capacity(esp) < esp


def test_wrappers_depend_on_the_payload_and_convert_it() -> None:
    tree = FIXTURE / "tree-initramfs"
    for target, script in (("azure", "vhd"), ("gcp", "tar")):
        assert (tree / f"mkosi.images/{target}/mkosi.conf").read_text(encoding="utf-8") == (
            "[Config]\nDependencies=payload\n\n"
            f"[Output]\nFormat=disk\nOutput={target}\nSeed={DEFAULT_SEED}\n\n"
            '[Build]\nEnvironment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"\n\n'
            "[Content]\nBootable=no\nExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI\n"
        )
        esp = (tree / f"mkosi.images/{target}/mkosi.repart/00-esp.conf").read_text(encoding="utf-8")
        assert esp == (tree / "mkosi.repart/00-esp.conf").read_text(encoding="utf-8")
        assert "%O" not in esp
        converter = (tree / f"mkosi.images/{target}/mkosi.postoutput.d/{script}").read_text(
            encoding="utf-8"
        )
        assert f'"$OUTPUTDIR/{target}.raw"' in converter
    assert "qemu-img convert -O vpc -o subformat=fixed,force_size" in (
        tree / "mkosi.images/azure/mkosi.postoutput.d/vhd"
    ).read_text(encoding="utf-8")
    assert "tar --format=oldgnu --sparse --hole-detection=raw" in (
        tree / "mkosi.images/gcp/mkosi.postoutput.d/tar"
    ).read_text(encoding="utf-8")


def test_dev_profile_is_an_overlay_with_public_keys_only() -> None:
    """The overlay adds the export path's packages and the container's subimage, root and
    modules; sshd, the firewall and its modules are the image's, in every profile, and no
    profile holds a key for root."""
    dev = FIXTURE / "tree-initramfs/payload/mkosi.profiles/dev"
    assert (dev / "mkosi.conf").read_text(encoding="utf-8") == (
        "[Config]\nDependencies=box\n\n[Output]\nImageId=%i-dev\n\n[Content]\n"
        + "KernelModules=\n    erofs\n    loop\n"
        + "Packages=\n    prometheus\n    prometheus-node-exporter\n    systemd-journal-remote\n"
        + "ExtraTrees=%O/box.raw:/usr/lib/tundravm/containers/box.raw\n"
    )
    assert not list((FIXTURE / "tree-initramfs").rglob("authorized_keys.d/root"))
    private = re.compile(r"PRIVATE KEY")
    assert not any(
        private.search(p.read_text(encoding="utf-8", errors="replace"))
        for p in (FIXTURE / "tree-initramfs").rglob("*")
        if p.is_file()
    )


@pytest.mark.parametrize(("root", "unit"), [("initramfs", True), ("verity", False)])
def test_the_root_is_root_owned_0755_and_the_probe_says_so(root: str, unit: bool) -> None:
    """The initramfs root is tmpfs's 1777 until its unit runs; a verity root is 0755 already."""
    payload = FIXTURE / f"tree-{root}/payload"
    service = payload / "mkosi.extra/usr/lib/systemd/system/tundravm-root-mode.service"
    assert service.is_file() is unit
    if unit:
        assert "ExecStart=/usr/bin/chmod 0755 /\n" in service.read_text(encoding="utf-8")
    probe = payload / "mkosi.profiles/dev/mkosi.extra/usr/lib/tundravm/probe"
    assert "check=root\nprobe_root\n" in probe.read_text(encoding="utf-8")


def test_verity_layout_splits_the_payload_and_copies_its_partitions() -> None:
    tree = FIXTURE / "tree-verity"
    data, hashes = verity_uuids(DEFAULT_SEED)
    payload = (tree / "payload/mkosi.conf").read_text(encoding="utf-8")
    assert "Format=disk\n" in payload
    assert "SplitArtifacts=uki,partitions\n" in payload
    assert "[Validation]\nVerity=hash\n" in payload
    assert f"systemd.verity_root_data=/dev/disk/by-partuuid/{data}" in payload
    assert f"systemd.verity_root_hash=/dev/disk/by-partuuid/{hashes}" in payload
    assert "KernelCommandLine=root=/dev/mapper/root " in payload
    for module in ("dm_verity", "erofs", "overlay", "crc32c_generic"):
        assert f"\n    {module}\n" in payload, module  # the initrd mounts the root with them
    root = (tree / "payload/mkosi.repart/10-root.conf").read_text(encoding="utf-8")
    assert "Format=erofs\n" in root and "Verity=data\n" in root and "SplitName=root\n" in root
    for disk in (
        "mkosi.repart",
        "mkosi.images/azure/mkosi.repart",
        "mkosi.images/gcp/mkosi.repart",
    ):
        copied = (tree / disk / "10-root.conf").read_text(encoding="utf-8")
        assert copied == f"[Partition]\nType=root\nCopyBlocks=/payload/root.raw\nUUID={data}\n"
        verity = (tree / disk / "11-root-verity.conf").read_text(encoding="utf-8")
        assert verity == (
            f"[Partition]\nType=root-verity\nCopyBlocks=/payload/root-verity.raw\nUUID={hashes}\n"
        )


# ── the tree as mkosi resolves it ───────────────────────────────────────


@pytest.mark.parametrize("root", ROOTS)
def test_summary_of_every_node_is_the_committed_evidence(root: str) -> None:
    tree = FIXTURE / f"tree-{root}"
    found = {name: normalised(mkosi_summary(tree, *sel), tree) for name, sel in SELECTIONS.items()}
    golden = FIXTURE / f"summary-{root}.json"
    text = json.dumps(found, indent=2, sort_keys=True) + "\n"
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        golden.write_text(text, encoding="utf-8")
    assert json.loads(golden.read_text(encoding="utf-8")) == found


def test_summary_resolves_the_selection_mkosi_argv_names() -> None:
    tree = FIXTURE / "tree-initramfs"
    lowered = lower(recipe_with())
    for variant, images, profiles in (
        ("production", ["payload", "azure", "gcp", "main"], []),
        ("dev", ["box", "payload", "azure", "gcp", "main"], ["dev"]),
    ):
        summary = mkosi_summary(tree, *mkosi_argv(lowered, variant))
        assert [image["Image"] for image in summary["Images"]] == images, variant
        payload = by_image(summary)["payload"]
        assert payload["Profiles"] == profiles
        assert payload["ImageId"] == ("fixture-dev" if profiles else "fixture")


def test_mkosi_argv_names_the_profile_and_dependencies() -> None:
    lowered = lower(recipe_with())
    clouds = ["--dependency=azure", "--dependency=gcp"]
    assert mkosi_argv(lowered, "production") == clouds
    assert mkosi_argv(lowered, "dev") == ["--profile=dev", *clouds]


def test_stale_history_is_never_replayed(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    compile(recipe_with()).write(tree)
    before = normalised(mkosi_summary(tree), tree)
    history = tree / ".mkosi-private" / "history"
    history.mkdir(parents=True)
    (history / "latest.json").write_text(
        json.dumps({"Distribution": "fedora", "Release": "40", "Profiles": ["dev"]}),
        encoding="utf-8",
    )
    assert normalised(mkosi_summary(tree), tree) == before
    assert "History=no\n" in (tree / "mkosi.conf").read_text(encoding="utf-8")


def test_main_image_settings_are_mkosis_main_scopes() -> None:
    from mkosi.config import SETTINGS, SettingScope

    scopes = {
        SettingScope.universal,
        SettingScope.multiversal,
        SettingScope.main,
        SettingScope.tools,
        SettingScope.initrd,
    }
    assert {s.name for s in SETTINGS if s.scope in scopes} == MAIN_IMAGE_SETTINGS


def test_every_emitted_key_is_an_mkosi_setting_in_its_section() -> None:
    from mkosi.config import SETTINGS_LOOKUP_BY_NAME

    for root in ROOTS:
        for conf in (FIXTURE / f"tree-{root}").rglob("*.conf"):
            if {"mkosi.repart", "mkosi.extra", "mkosi.skeleton"} & set(conf.parts):
                continue
            section = ""
            for line in conf.read_text(encoding="utf-8").splitlines():
                if line.startswith("["):
                    section = line.strip("[]")
                elif "=" in line and not line.startswith(" "):
                    key = line.partition("=")[0]
                    setting = SETTINGS_LOOKUP_BY_NAME[key]
                    assert setting.section == section, f"{conf}: {key} in [{section}]"


# ── every tree tundravm writes ──────────────────────────────────────────

_IMAGES = [*EXAMPLE_RECIPES, ATTESTED_NODE / "image.py", FIXTURE / "fixture.py"]


@pytest.mark.parametrize("path", _IMAGES, ids=lambda p: Path(p).stem)
def test_every_tree_sets_the_reproducibility_knobs(path: Path, tmp_path: Path) -> None:
    recipe = load_file(path).recipe
    compile(recipe).write(tmp_path)
    main = (tmp_path / "mkosi.conf").read_text(encoding="utf-8")
    for line in (
        f"MinimumVersion={MKOSI_VERSION}",
        "Dependencies=payload",
        "ToolsTree=default",
        "ToolsTreeDistribution=debian",
        f"ToolsTreeRelease={recipe.platform.release}",
        "History=no",
        f"SourceDateEpoch={recipe.platform.epoch or 0}",
    ):
        assert f"\n{line}\n" in main, line
    assert re.search(r"\nSeed=[0-9a-f-]{36}\n", main)
    assert re.search(r"\nWithNetwork=(yes|no)\n", main)
    if recipe.platform.snapshot:
        assert f"\nSnapshot={recipe.platform.snapshot}\n" in main
        assert f"\nToolsTreeSnapshot={recipe.platform.snapshot}\n" in main
    payload = (tmp_path / "payload/mkosi.conf").read_text(encoding="utf-8")
    assert re.search(r"\nSeed=[0-9a-f-]{36}\n", payload)
    machine_id = tmp_path / "payload/mkosi.extra/etc/machine-id"
    assert machine_id.read_text(encoding="utf-8") == "uninitialized\n"
    for conf in tmp_path.rglob("*.conf"):
        for line in conf.read_text(encoding="utf-8").splitlines():
            assert not line.startswith("ConfigureScripts="), conf
            assert not line.startswith("PassEnvironment="), conf
            if line.startswith("Environment="):
                assert "=" in line.removeprefix("Environment="), f"{conf}: {line}"


def test_the_new_image_pins_the_tools_tree_to_its_release_and_snapshot(tmp_path: Path) -> None:
    """``ToolsTreeRelease`` and ``ToolsTreeSnapshot`` equal the image's: mkosi's default tools
    release is ``testing``, whose package-manager profile installs a dnf5 a trixie snapshot
    lacks, so an unpinned release breaks the tools tree."""
    path = write_new_image(tmp_path / "node.py")
    recipe = load_file(path).recipe
    compile(recipe).write(tmp_path / "tree")
    main = (tmp_path / "tree" / "mkosi.conf").read_text(encoding="utf-8")
    assert f"\nRelease={recipe.platform.release}\n" in main
    assert f"\nToolsTreeRelease={recipe.platform.release}\n" in main
    snapshot = recipe.platform.snapshot
    if snapshot is not None:
        assert f"\nSnapshot={snapshot}\n" in main
        assert f"\nToolsTreeSnapshot={snapshot}\n" in main
    else:
        assert "Snapshot=" not in main


def test_the_lock_records_the_tools_tree_mkosi_resolves() -> None:
    """``lock`` records the tools tree the tree pins, and ``summary --json``'s ``Tools`` (what
    a bake records) resolves to the same distribution, release and snapshot."""
    tree = FIXTURE / "tree-initramfs"
    resolved = ToolsTree.from_summary(mkosi_summary(tree)["Tools"])
    lowered = lower(recipe_with())
    assert resolved == lowered.tools_tree == ToolsTree("debian", "trixie", "20261018T082309Z")
    locked = lock(recipe_with())
    assert locked.lockfile.toolchain is not None
    assert locked.lockfile.toolchain.tools_tree == resolved


def test_the_attested_node_tree_resolves_every_node() -> None:
    tree = ATTESTED_NODE / "mkosi"
    summary = mkosi_summary(tree, "--profile=dev", "--dependency=azure", "--dependency=gcp")
    images = by_image(summary)
    assert list(images) == ["default-initrd", "payload", "azure", "gcp", "main"]
    # RootFS.verity(): the payload is a disk with an erofs root, booted by mkosi's initrd.
    assert images["payload"]["Format"] == "disk"
    assert images["payload"]["Dependencies"] == ["default-initrd"]
    assert images["payload"]["ImageId"] == "attested-node-dev"
    assert "openssh-server" in images["payload"]["Packages"]
    assert images["main"]["Dependencies"] == ["payload", "azure", "gcp"]
    for name in ("azure", "gcp"):
        assert images[name]["Dependencies"] == ["payload"]
    assert summary["Tools"]["Snapshot"] == images["payload"]["Snapshot"] == "20261018T082309Z"


def test_selected_variants_do_not_change_the_payload() -> None:
    whole = compile(recipe_with())
    alone = compile(recipe_with(), variants=["production"])
    payload = {e.path: e for e in whole.entries if e.path.startswith("payload/")}
    own = {e.path: e for e in alone.entries if e.path.startswith("payload/")}
    shared = {p: e for p, e in payload.items() if not p.startswith("payload/mkosi.profiles/")}
    assert own == shared
