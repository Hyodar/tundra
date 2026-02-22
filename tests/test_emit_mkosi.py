from pathlib import Path
from typing import cast

import pytest

from tdx import Image
from tdx.errors import ValidationError
from tdx.models import Phase


def test_emit_mkosi_golden_output(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.install("jq", "curl")
    image.run("echo", "prep", phase="prepare", env={"B": "2", "A": "1"}, cwd="/work")
    image.run("echo", "build", phase="build")

    output_dir = image.emit_mkosi(tmp_path / "mkosi")

    conf_path = output_dir / "default" / "mkosi.conf"
    prepare_script = output_dir / "default" / "scripts" / "03-prepare.sh"
    build_script = output_dir / "default" / "scripts" / "04-build.sh"

    assert conf_path.read_text(encoding="utf-8") == (
        "[Distribution]\n"
        "Base=debian/bookworm\n"
        "\n"
        "[Output]\n"
        "ImageId=default\n"
        "\n"
        "[Content]\n"
        "Packages=curl jq\n"
        "BuildPackages=\n"
        "\n"
        "[Scripts]\n"
        "PrepareScripts=scripts/03-prepare.sh\n"
        "BuildScripts=scripts/04-build.sh\n"
    )
    assert prepare_script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "\n"
        "(cd /work && A=1 B=2 echo prep)\n"
    )
    assert build_script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "\n"
        "echo build\n"
    )


def test_emit_mkosi_is_deterministic(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.install("curl")
    image.run("echo", "hello", phase="prepare")

    output_a = image.emit_mkosi(tmp_path / "mkosi-a")
    output_b = image.emit_mkosi(tmp_path / "mkosi-b")

    assert _snapshot_tree(output_a) == _snapshot_tree(output_b)


def test_emit_mkosi_rejects_invalid_phase(tmp_path: Path) -> None:
    image = Image()
    image.state.profiles["default"].phases[cast(Phase, "invalid-phase")] = []

    with pytest.raises(ValidationError) as excinfo:
        image.emit_mkosi(tmp_path / "mkosi")

    assert excinfo.value.code == "E_VALIDATION"


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return snapshot
