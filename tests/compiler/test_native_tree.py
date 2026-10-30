"""The mkosi-native tree (gate 1): the fixture tree file by file and as mkosi resolves it.

``tests/fixtures/native-tree/recipe.py`` compiles, with each root layout, to the committed
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
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import EXAMPLE_RECIPES, REPO_ROOT, SURGE_EXAMPLE
from tundravm.check import declaration_check
from tundravm.compiler.emit_mkosi import MAIN_IMAGE_SETTINGS, MKOSI_VERSION, verity_uuids
from tundravm.declarative import (
    Recipe,
    Setting,
    compile,
    load,
    lower,
    mkosi_argv,
)
from tundravm.errors import ValidationError
from tundravm.testing import UPDATE_GOLDEN_ENV, assert_tree_matches

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "native-tree"
ROOTS = ("initramfs", "verity")
SELECTIONS: dict[str, tuple[str, ...]] = {
    "default": (),
    "every-node": ("--profile=dev", "--dependency=azure", "--dependency=gcp"),
}
"""The ``mkosi`` selections whose summaries are evidence: what ``default`` builds, and every
image of the tree (the dev profile, both cloud wrappers, the payload and the QEMU disk)."""

recipe_with: Callable[..., Recipe] = runpy.run_path(str(FIXTURE / "recipe.py"))["recipe_with"]

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
        "mkosi.images/gcp/mkosi.conf",
        "mkosi.images/gcp/mkosi.postoutput.d/tar",
        "mkosi.images/gcp/mkosi.repart/00-esp.conf",
        "mkosi.images/payload/mkosi.conf",
        "mkosi.repart/00-esp.conf",
        "payload/mkosi.conf",
        "payload/mkosi.conf.d/10-platform.conf",
        "payload/mkosi.conf.d/20-greeter.conf",
        "payload/mkosi.conf.d/30-cloud-glue.conf",
        "payload/mkosi.finalize.d/50-finalize.sh",
        "payload/mkosi.postinst.d/50-postinst.sh",
        "payload/mkosi.profiles/dev/mkosi.conf",
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
        "Snapshot=20251113T083151Z\n"
        "\n"
        "[Output]\n"
        "Format=disk\n"
        "Output=qemu\n"
        "ImageId=fixture\n"
        "OutputDirectory=mkosi.output\n"
        "Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70\n"
        "\n"
        "[Build]\n"
        "ToolsTree=default\n"
        "ToolsTreeDistribution=debian\n"
        "ToolsTreeRelease=trixie\n"
        "ToolsTreeSnapshot=20251113T083151Z\n"
        "ToolsTreePackages=qemu-utils\n"
        "History=no\n"
        "WithNetwork=no\n"
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
    assert payload.startswith(
        "[Output]\nFormat=uki\nOutput=payload\nSeed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70\n"
    )
    assert "Packages=\n    passwd\n" in payload  # the account tools: no concern declared them
    assert (tree / "payload/mkosi.conf.d/20-greeter.conf").read_text(encoding="utf-8") == (
        "[Content]\nPackages=\n    curl\n"
    )
    assert (tree / "payload/mkosi.conf.d/30-cloud-glue.conf").read_text(encoding="utf-8") == (
        "[Content]\nPackages=\n    dmidecode\n"
    )
    machine_id = tree / "payload/mkosi.extra/etc/machine-id"
    assert machine_id.read_text(encoding="utf-8") == "uninitialized\n"


def test_cloud_glue_is_in_the_payload_and_gated_at_boot() -> None:
    units = FIXTURE / "tree-initramfs/payload/mkosi.extra/usr/lib/systemd/system"
    assert "ConditionVirtualization=microsoft" in (
        units / "azure-complete-provisioning.service"
    ).read_text(encoding="utf-8")
    assert "ConditionVirtualization=google" in (units / "gcp-metadata-dns.service").read_text(
        encoding="utf-8"
    )
    for conf in (FIXTURE / "tree-initramfs").rglob("*.conf"):
        assert "[Match]" not in conf.read_text(encoding="utf-8"), conf


def test_wrappers_depend_on_the_payload_and_convert_it() -> None:
    tree = FIXTURE / "tree-initramfs"
    for target, script in (("azure", "vhd"), ("gcp", "tar")):
        assert (tree / f"mkosi.images/{target}/mkosi.conf").read_text(encoding="utf-8") == (
            "[Config]\nDependencies=payload\n\n"
            f"[Output]\nFormat=disk\nOutput={target}\nSeed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70\n\n"
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
    dev = FIXTURE / "tree-initramfs/payload/mkosi.profiles/dev"
    assert (dev / "mkosi.conf").read_text(encoding="utf-8") == (
        "[Output]\nImageId=%i-dev\n\n[Content]\nPackages=\n    openssh-server\n"
    )
    keys = (dev / "mkosi.extra/etc/ssh/authorized_keys.d/root").read_text(encoding="utf-8")
    assert keys.startswith("ssh-ed25519 ")
    private = re.compile(r"PRIVATE KEY")
    assert not any(
        private.search(p.read_text(encoding="utf-8", errors="replace"))
        for p in (FIXTURE / "tree-initramfs").rglob("*")
        if p.is_file()
    )


def test_verity_layout_splits_the_payload_and_copies_its_partitions() -> None:
    tree = FIXTURE / "tree-verity"
    data, hashes = verity_uuids("6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70")
    payload = (tree / "payload/mkosi.conf").read_text(encoding="utf-8")
    assert "Format=disk\n" in payload
    assert "SplitArtifacts=uki,partitions\n" in payload
    assert "[Validation]\nVerity=hash\n" in payload
    assert f"systemd.verity_root_data=/dev/disk/by-partuuid/{data}" in payload
    assert f"systemd.verity_root_hash=/dev/disk/by-partuuid/{hashes}" in payload
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
        ("default", ["payload", "main"], []),
        ("azure", ["payload", "azure", "main"], []),
        ("gcp", ["payload", "gcp", "main"], []),
        ("dev", ["payload", "main"], ["dev"]),
    ):
        summary = mkosi_summary(tree, *mkosi_argv(lowered, variant))
        assert [image["Image"] for image in summary["Images"]] == images, variant
        payload = by_image(summary)["payload"]
        assert payload["Profiles"] == profiles
        assert payload["ImageId"] == ("fixture-dev" if profiles else "fixture")


def test_mkosi_argv_names_the_profile_and_dependencies() -> None:
    lowered = lower(recipe_with())
    assert mkosi_argv(lowered, "default") == []
    assert mkosi_argv(lowered, "azure") == ["--dependency=azure"]
    assert mkosi_argv(lowered, "gcp") == ["--dependency=gcp"]
    assert mkosi_argv(lowered, "dev") == ["--profile=dev"]


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

_RECIPES = [*EXAMPLE_RECIPES, SURGE_EXAMPLE / "image.py", FIXTURE / "recipe.py"]


@pytest.mark.parametrize("path", _RECIPES, ids=lambda p: Path(p).stem)
def test_every_tree_sets_the_reproducibility_knobs(path: Path, tmp_path: Path) -> None:
    recipe = load(Path(path))
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


def test_surge_tree_resolves_every_node() -> None:
    tree = SURGE_EXAMPLE / "mkosi"
    summary = mkosi_summary(tree, "--profile=devtools", "--dependency=azure", "--dependency=gcp")
    images = by_image(summary)
    assert list(images) == ["payload", "azure", "gcp", "main"]
    assert images["payload"]["Format"] == "uki"
    assert images["payload"]["ImageId"] == "surge-tdx-prover-devtools"
    assert "openssh-server" in images["payload"]["Packages"]
    assert images["main"]["Dependencies"] == ["payload", "azure", "gcp"]
    for name in ("azure", "gcp"):
        assert images[name]["Dependencies"] == ["payload"]
    assert summary["Tools"]["Snapshot"] == images["payload"]["Snapshot"] == "20251113T083151Z"


# ── what the tree refuses ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("setting", "code"),
    [
        (Setting("Config", "ConfigureScripts", ("configure.sh",)), "mkosi-configure-script"),
        (Setting("Build", "Environment", ("HOME",)), "mkosi-environment-passthrough"),
        (Setting("Config", "PassEnvironment", ("PATH",)), "mkosi-environment-passthrough"),
    ],
)
def test_settings_that_break_the_summary_or_leak_the_host_are_refused(
    setting: Setting, code: str
) -> None:
    recipe = Recipe("refused", items=(setting,))
    with pytest.raises(ValidationError, match=code):
        compile(recipe)
    assert code in {d.code for d in declaration_check(recipe)}


def test_selected_variants_do_not_change_the_payload() -> None:
    whole = compile(recipe_with())
    alone = compile(recipe_with(), variants=["default"])
    payload = {e.path: e for e in whole.entries if e.path.startswith("payload/")}
    own = {e.path: e for e in alone.entries if e.path.startswith("payload/")}
    shared = {p: e for p, e in payload.items() if not p.startswith("payload/mkosi.profiles/")}
    assert own == shared
