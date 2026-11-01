"""``convert PATH --from mkosi|systemd``."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

import tundravm
from tests.cli.conftest import Cli
from tests.helpers import REPO_ROOT
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.systemd import Calendar

pytestmark = pytest.mark.boundary  # convert reads the tree through mkosi's own parser

TREE = REPO_ROOT / "tests" / "fixtures" / "native-tree" / "tree-initramfs"

SERVICE = """\
[Unit]
Description=Web

[Service]
Type=simple
ExecStart=/usr/bin/web --port 8080
User=web

[Install]
WantedBy=multi-user.target
"""


def _image_call(source: str) -> ast.Call:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Image":
            return node
    raise AssertionError("no Image(...) in the source")


PACKAGES = [
    "busybox",
    "curl",
    *(f"systemd-{part}" for part in ("cryptsetup", "repart", "resolved", "timesyncd")),
    *(f"libtss2-tcti-device{n}" for n in range(8)),
]
"""Enough hyphenated names that the list wraps, which once broke a name at a hyphen."""


def test_an_mkosi_tree_becomes_an_image_file(cli: Cli, tmp_path: Path) -> None:
    tree = tmp_path / "plain"
    for directory in ("mkosi.extra", "mkosi.skeleton"):
        (tree / directory / "etc").mkdir(parents=True)
    (tree / "mkosi.conf").write_text(
        "[Distribution]\nDistribution=debian\nRelease=trixie\n\n[Output]\nImageId=plain\n\n"
        "[Content]\nPackages=" + " ".join(PACKAGES) + "\n"
    )
    (tree / "mkosi.postinst").write_text("#!/bin/sh\ntrue\n")
    result = cli("convert", tree, "--from", "mkosi")
    assert result.code == EXIT_OK, result.err
    call = _image_call(result.out)
    assert ast.literal_eval(call.args[0]) == "plain"
    keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert keywords["base"] == "Debian('trixie')"
    assert ast.literal_eval(keywords["packages"]) == sorted(PACKAGES)
    assert keywords["files"] == "[Tree(HERE / 'mkosi.extra', '/')]"
    assert "postinst" in keywords["mkosi"]
    assert "'" not in result.out.partition("\n# ")[0]  # double quotes, as ruff format writes
    assert "# unsupported: SkeletonTrees=mkosi.skeleton: a second tree at /" in result.out
    assert str(tree) not in result.out  # paths are relative to the image file


def test_a_tree_tundravm_wrote_converts_its_payload_and_checks(cli: Cli, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    shutil.copytree(REPO_ROOT / "examples" / "surge-tdx-prover" / "mkosi", tree, symlinks=True)
    result = cli("convert", tree, "--from", "mkosi")
    assert result.code == EXIT_OK, result.err
    (tree / "image.py").write_text(result.out, encoding="utf-8")
    assert cli("check", tree / "image.py").code == EXIT_OK
    assert "systemd-boot-efi" in ast.literal_eval(
        ast.unparse(next(k.value for k in _image_call(result.out).keywords if k.arg == "packages"))
    )
    for note in ("main image (disk)", "subimage azure", "subimage gcp", "payload Bootloader"):
        assert f"# note: {note}" in result.out, note
    assert "# unsupported:" not in result.out


def test_a_tree_that_runs_scripts_while_parsing_needs_allow_scripts(
    cli: Cli, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(TREE, tree, symlinks=True)
    version = tree / "mkosi.version"
    version.write_text("#!/bin/sh\necho 1.2.3\n")
    version.chmod(0o755)
    refused = cli("convert", tree, "--from", "mkosi")
    assert refused.code == EXIT_SDK_ERROR
    assert "Parsing" in refused.err and "runs mkosi.version" in refused.err
    assert "--allow-scripts" in refused.err
    allowed = cli("convert", tree, "--from", "mkosi", "--allow-scripts")
    assert allowed.code == EXIT_OK, allowed.err
    assert "# note: ran while parsing: mkosi.version" in allowed.out


def test_a_directory_without_mkosi_conf_is_refused(cli: Cli, tmp_path: Path) -> None:
    result = cli("convert", tmp_path, "--from", "mkosi")
    assert result.code == EXIT_SDK_ERROR and "has no mkosi.conf" in result.err


def test_allow_scripts_applies_to_mkosi_only(cli: Cli, tmp_path: Path) -> None:
    unit = tmp_path / "web.service"
    unit.write_text(SERVICE)
    result = cli("convert", unit, "--from", "systemd", "--allow-scripts")
    assert result.code == EXIT_SDK_ERROR


WORKER = """\
[Unit]
Description=Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/worker --queue jobs
Environment=MODE=batch
StateDirectory=worker
MemoryMax=1G
ProtectSystem=strict
OOMScoreAdjust=-500
"""

BACKUP = "[Service]\nType=oneshot\nExecStart=/usr/bin/backup\n"
BACKUP_TIMER = "[Timer]\nOnCalendar=daily\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"


def test_units_become_an_image_file_that_loads_and_checks(cli: Cli, tmp_path: Path) -> None:
    units = tmp_path / "units"
    (units / "worker.service.d").mkdir(parents=True)
    (units / "web.service").write_text(SERVICE)
    (units / "worker.service").write_text(WORKER)
    (units / "worker.service.d" / "10-trace.conf").write_text("[Service]\nEnvironment=TRACE=1\n")
    (units / "backup.service").write_text(BACKUP)
    (units / "backup.timer").write_text(BACKUP_TIMER)
    (units / "data.mount").write_text("[Mount]\nWhat=/dev/vdb\nWhere=/data\n")
    result = cli("convert", units, "--from", "systemd")
    assert result.code == EXIT_OK, result.err
    imports = [line for line in result.out.splitlines() if line.startswith(("from ", "import "))]
    assert imports == [
        "from tundravm import Image, Service",
        "from tundravm.systemd import Calendar, DropIn, Notify, Resources",
    ]
    assert "# unsupported: data.mount: no Image slot holds it" in result.out
    assert "in place of the unit's ProtectSystem" in result.out
    image_file = tmp_path / "image.py"
    image_file.write_text(result.out, encoding="utf-8")
    image = tundravm.load(image_file)
    services = {service.name: service for service in image.services}
    assert services["web"].argv == ("/usr/bin/web", "--port", "8080")
    assert dict(services["worker"].environment) == {"MODE": "batch", "TRACE": "1"}
    assert services["backup"].timer == Calendar("daily")
    checked = cli("check", image_file)
    assert checked.code == EXIT_OK, checked.out + checked.err


def test_a_directory_without_units_is_refused(cli: Cli, tmp_path: Path) -> None:
    result = cli("convert", tmp_path, "--from", "systemd")
    assert result.code == EXIT_SDK_ERROR and "holds no unit file" in result.err


@pytest.mark.parametrize("source", ["mkosi", "systemd"])
def test_from_is_required_and_closed(
    cli: Cli, source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        cli("convert", "x")
    with pytest.raises(SystemExit):
        cli("convert", "x", "--from", "docker")
    assert "invalid choice: 'docker'" in capsys.readouterr().err
