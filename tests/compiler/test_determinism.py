"""What the current dialect normalizes for reproducible images (docs/reproducibility.md).

Finalize ends with the deterministic accounts step; the azure and gcp postoutput scripts
build their ESP, disk and wrapper reproducibly. ``nethermind-v1`` keeps its pinned scripts.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tundravm.compiler.emit_mkosi import (
    ACCOUNTS_FINALIZE,
    AZURE_POSTOUTPUT_SCRIPT,
    AZURE_POSTOUTPUT_SCRIPT_CURRENT,
    GCP_POSTOUTPUT_SCRIPT,
    GCP_POSTOUTPUT_SCRIPT_CURRENT,
    cloud_postoutput_script,
)
from tundravm.declarative import Debian, Package, Recipe, Variant, compile

PINNED = {
    "accounts": "9175aea9c4328f7e086be2ebaadc30fc167993f0d4db5292ab410f2115121e41",
    "azure": "e5ba67a792382a9f4484ff13a1cb30aefaa01cf8baa6c940b8e553de0c998344",
    "gcp": "8f077eea746bf296f268b208c8530f0def8b14dd4a9ea4a6a69b0c647a8cf6cc",
}
"""sha256 of each emitted script: a change to one is deliberate (re-record it here)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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


def test_the_emitted_scripts_are_pinned() -> None:
    assert _sha(ACCOUNTS_FINALIZE) == PINNED["accounts"]
    assert _sha(AZURE_POSTOUTPUT_SCRIPT_CURRENT) == PINNED["azure"]
    assert _sha(GCP_POSTOUTPUT_SCRIPT_CURRENT) == PINNED["gcp"]


def test_current_dialect_finalize_ends_with_the_accounts_step(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    for variant in ("default", "azure", "gcp"):
        finalize = (tree / variant / "scripts/07-finalize.sh").read_text()
        assert finalize.endswith("\n\n" + ACCOUNTS_FINALIZE), variant
        assert finalize.count("tundravm_sort_by_field passwd 3") == 1
    assert (tree / "azure/scripts/azure-postoutput.sh").read_text() == (
        AZURE_POSTOUTPUT_SCRIPT_CURRENT
    )
    assert (tree / "gcp/scripts/gcp-postoutput.sh").read_text() == GCP_POSTOUTPUT_SCRIPT_CURRENT


def test_a_non_reproducible_build_skips_the_accounts_step(tmp_path: Path) -> None:
    tree = _tree(tmp_path, epoch=None)
    finalize = (tree / "default/scripts/07-finalize.sh").read_text()
    assert "tundravm_sort" not in finalize


def test_nethermind_v1_keeps_its_pinned_cloud_scripts() -> None:
    assert cloud_postoutput_script(AZURE_POSTOUTPUT_SCRIPT, "nethermind-v1") == (
        AZURE_POSTOUTPUT_SCRIPT
    )
    assert cloud_postoutput_script(GCP_POSTOUTPUT_SCRIPT, "nethermind-v1") == GCP_POSTOUTPUT_SCRIPT
    assert cloud_postoutput_script(AZURE_POSTOUTPUT_SCRIPT) == AZURE_POSTOUTPUT_SCRIPT_CURRENT
    assert cloud_postoutput_script(GCP_POSTOUTPUT_SCRIPT) == GCP_POSTOUTPUT_SCRIPT_CURRENT


def test_the_cloud_scripts_spell_their_normalizations() -> None:
    for script in (AZURE_POSTOUTPUT_SCRIPT_CURRENT, GCP_POSTOUTPUT_SCRIPT_CURRENT):
        assert "${IMAGE_ID}_${IMAGE_VERSION}" not in script  # nethermind-v1's name only
        assert 'mkfs.vfat -F 32 -n ESP -i "${UKI_SHA:0:8}" --invariant' in script
        assert 'mcopy -b -s -p -Q -m -i "$TMP/esp.img" "$TMP/esp/EFI" ::' in script
        assert 'touch -h -d "@$SOURCE_DATE_EPOCH"' in script
        assert "export TZ=UTC" in script
        assert 'sgdisk --disk-guid="$DISK_GUID"' in script
        assert "parted" not in script
    assert "--sort=name" in GCP_POSTOUTPUT_SCRIPT_CURRENT
    assert "--owner=0 --group=0 --numeric-owner" in GCP_POSTOUTPUT_SCRIPT_CURRENT
    assert "--hole-detection=raw" in GCP_POSTOUTPUT_SCRIPT_CURRENT
    assert '| gzip -n >"$TAR"' in GCP_POSTOUTPUT_SCRIPT_CURRENT
    assert "footer[68:84] = unique" in AZURE_POSTOUTPUT_SCRIPT_CURRENT


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


FAKE_MCOPY = "#!/bin/bash\nexit 0\n"
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


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("mkfs.vfat", "sgdisk", "python3")),
    reason="needs mkfs.vfat and sgdisk",
)
def test_the_cloud_scripts_reproduce_their_outputs(tmp_path: Path) -> None:
    """mtools and qemu-img are faked: the footer patch, mkfs, GPT and tar.gz are real."""
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, text in (("mcopy", FAKE_MCOPY), ("qemu-img", FAKE_QEMU_IMG)):
        (tools / name).write_text(text)
        (tools / name).chmod(0o755)
    uki = os.urandom(1 << 20)
    digests = []
    for run in ("one", "two"):
        out = tmp_path / run
        out.mkdir()
        (out / "img.efi").write_bytes(uki)
        env = {
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "OUTPUTDIR": str(out),
            "IMAGE_ID": "img",
            "IMAGE_VERSION": "",
            "SOURCE_DATE_EPOCH": "0",
        }
        for script in (AZURE_POSTOUTPUT_SCRIPT_CURRENT, GCP_POSTOUTPUT_SCRIPT_CURRENT):
            subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
        digests.append([_sha_file(out / name) for name in ("img.vhd", "img.tar.gz")])
        assert not (out / "azure-tmp").exists() and not (out / "gcp-tmp").exists()
    assert digests[0] == digests[1]


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
