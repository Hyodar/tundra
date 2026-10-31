"""``convert PATH --from mkosi|systemd``."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.helpers import REPO_ROOT
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR

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


def _rendered_task(directory: Path) -> None:
    """``backup.service`` + ``backup.timer`` as a scheduled ``Task`` renders them."""
    from tundravm._units.parse import rendered
    from tundravm.declarative import Calendar, Hardening, Task

    task = Task(
        "backup",
        "/usr/bin/backup",
        schedule=Calendar("daily"),
        user="backup",
        hardening=Hardening.none(),
    )
    for name, text in rendered(task).items():
        (directory / name).write_text(text, encoding="utf-8")


def _image_call(source: str) -> ast.Call:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Image":
            return node
    raise AssertionError("no Image(...) in the source")


def test_an_mkosi_tree_becomes_an_image_file(cli: Cli, tmp_path: Path) -> None:
    result = cli("convert", TREE, "--from", "mkosi")
    assert result.code == EXIT_OK, result.err
    source, _, report = result.out.partition("\n# ")
    call = _image_call(result.out)
    assert ast.literal_eval(call.args[0]) == "fixture"
    keywords = {kw.arg: kw.value for kw in call.keywords}
    assert ast.unparse(keywords["base"]) == "Debian('trixie')"
    packages = ast.literal_eval(keywords["packages"])
    assert {"busybox", "curl", "linux-image-amd64"} <= set(packages)
    assert "Tree('payload/mkosi.extra', '/')" in ast.unparse(keywords["files"])
    assert "postinst" in ast.unparse(keywords["mkosi"])
    assert "# note: main image (disk): the QEMU disk tundravm builds for every image" in result.out
    assert "# unsupported: SkeletonTrees=payload/mkosi.skeleton" in result.out
    assert str(TREE) not in result.out  # paths are relative to the tree
    assert source and report


def test_a_converted_file_loads_beside_the_tree(cli: Cli, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(TREE, tree, symlinks=True)
    result = cli("convert", tree, "--from", "mkosi")
    (tree / "image.py").write_text(result.out, encoding="utf-8")
    from tundravm.recipe import load_file

    loaded = load_file(tree / "image.py")
    assert loaded.recipe.name == "fixture"


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


def test_a_unit_file_becomes_a_typed_declaration(cli: Cli, tmp_path: Path) -> None:
    unit = tmp_path / "web.service"
    unit.write_text(SERVICE)
    result = cli("convert", unit, "--from", "systemd")
    assert result.code == EXIT_OK, result.err
    assert result.out.startswith("# web.service: Service (exact)")
    assert "Service(" in result.out and "/usr/bin/web" in result.out
    unit.write_text(SERVICE.replace("User=web\n", "User=web\nOOMScoreAdjust=-500\n"))
    exact = cli("convert", unit, "--from", "systemd")
    assert "# web.service: kept as text, blocked by Service.OOMScoreAdjust" in exact.out
    assert "# unsupported: web.service: Service.OOMScoreAdjust" in exact.out
    semantic = cli("convert", unit, "--from", "systemd", "--mode", "semantic")
    assert "# web.service: Service (semantic); extra= holds Service.OOMScoreAdjust" in semantic.out


def test_a_directory_of_units_pairs_sockets_and_timers(cli: Cli, tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir()
    (units / "web.service").write_text(SERVICE)
    _rendered_task(units)
    (units / "README").write_text("not a unit")
    result = cli("convert", units, "--from", "systemd")
    assert result.code == EXIT_OK, result.err
    assert "# backup.service + backup.timer: Task (exact)" in result.out
    assert "# web.service: Service (exact)" in result.out
    namespace: dict[str, object] = {}
    exec(result.out, namespace)
    assert {type(v).__name__ for k, v in namespace.items() if k.startswith("unit")} == {
        "Task",
        "Service",
    }


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
