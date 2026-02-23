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

    conf_text = conf_path.read_text(encoding="utf-8")

    # Verify key sections exist in the mkosi.conf
    assert "[Distribution]" in conf_text
    assert "Distribution=debian" in conf_text
    assert "Release=bookworm" in conf_text
    assert "[Output]" in conf_text
    assert "Format=uki" in conf_text
    assert "ImageId=default" in conf_text
    assert "[Content]" in conf_text
    assert "curl" in conf_text
    assert "jq" in conf_text
    # Script references are in [Content] section (no separate [Scripts] section in mkosi v20+)
    assert "PrepareScripts=scripts/03-prepare.sh" in conf_text
    assert "BuildScripts=scripts/04-build.sh" in conf_text

    # Verify reproducibility settings
    assert "SourceDateEpoch=0" in conf_text
    assert "Seed=" in conf_text

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


def test_emit_mkosi_generates_extra_tree(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.file("/etc/motd", content="TDX VM\n")
    image.template(
        "/etc/app/config.toml",
        template="network={network}\n",
        vars={"network": "mainnet"},
    )

    output_dir = image.emit_mkosi(tmp_path / "mkosi")

    extra_dir = output_dir / "default" / "mkosi.extra"
    assert (extra_dir / "etc" / "motd").read_text(encoding="utf-8") == "TDX VM\n"
    assert (extra_dir / "etc" / "app" / "config.toml").read_text(
        encoding="utf-8"
    ) == "network=mainnet\n"


def test_emit_mkosi_generates_service_units(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.service(
        "app",
        exec=["/usr/bin/app", "--config", "/etc/app.toml"],
        user="app",
        after=["network-online.target"],
        restart="always",
        security_profile="strict",
    )

    output_dir = image.emit_mkosi(tmp_path / "mkosi")

    unit_path = (
        output_dir / "default" / "mkosi.extra"
        / "usr" / "lib" / "systemd" / "system" / "app.service"
    )
    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/app --config /etc/app.toml" in content
    assert "User=app" in content
    assert "After=network-online.target" in content
    assert "Restart=always" in content
    assert "ProtectSystem=strict" in content
    assert "WantedBy=minimal.target" in content


def test_emit_mkosi_generates_postinst_with_users(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.user("app", system=True, home="/var/lib/app", uid=1000, groups=["tdx"])

    output_dir = image.emit_mkosi(tmp_path / "mkosi")

    # Check that postinst script exists and has user creation
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"
    assert postinst.exists()
    content = postinst.read_text(encoding="utf-8")
    assert "useradd" in content
    assert "--system" in content
    assert "--home-dir" in content
    assert "/var/lib/app" in content


def test_emit_mkosi_generates_debloat_finalize(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.debloat(enabled=True, paths_remove_extra=["/usr/share/fonts"])

    output_dir = image.emit_mkosi(tmp_path / "mkosi")

    finalize = output_dir / "default" / "scripts" / "07-finalize.sh"
    assert finalize.exists()
    content = finalize.read_text(encoding="utf-8")
    assert "rm -rf" in content
    assert "/usr/share/doc" in content
    assert "/usr/share/fonts" in content


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return snapshot
