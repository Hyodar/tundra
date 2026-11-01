import importlib
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CORE_MODULES = [
    "tundravm.declarative._lowered",
    "tundravm.compiler",
    "tundravm.backends",
    "tundravm.lockfile",
    "tundravm.measure",
    "tundravm.deploy",
    "tundravm.policy",
    "tundravm._reporting",
    "tundravm._modules",
    "tundravm.cli",
    "tundravm.recipe",
    "tundravm.check",
    "tundravm.diff",
    "tundravm._describe",
    "tundravm.builds",
    "tundravm.manifest",
    "tundravm.instances",
    "tundravm.bootstrap",
    "tundravm.deployments",
    "tundravm.show",
    "tundravm.convert",
    "tundravm.declarative",
    "tundravm.testing",
]


def test_core_package_layout_modules_importable() -> None:
    for module_name in CORE_MODULES:
        module = importlib.import_module(module_name)
        assert module is not None


@pytest.mark.boundary
def test_the_sdist_and_the_wheel_hold_no_go_module(tmp_path: Path) -> None:
    """tools/ is the Go module images build from this repository, never package data."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv builds the distributions")
    subprocess.run([uv, "build", "--quiet", "--out-dir", str(tmp_path)], cwd=ROOT, check=True)
    (sdist,) = tmp_path.glob("*.tar.gz")
    (wheel,) = tmp_path.glob("*.whl")
    with tarfile.open(sdist) as archive:
        names = [name.split("/", 1)[1] for name in archive.getnames() if "/" in name]
    names += zipfile.ZipFile(wheel).namelist()
    assert "src/tundravm/__init__.py" in names
    assert not [name for name in names if name.startswith("tools/")]
