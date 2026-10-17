"""``tundravm units convert`` and ``tundravm units render``."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm._units.parse import rendered
from tundravm.cli import EXIT_OK, main
from tundravm.declarative import Calendar, Hardening, Task
from tundravm.templates import render_recipe_template

WEB = """\
[Unit]
Description=Web

[Service]
Type=simple
ExecStart=/usr/bin/web --port 8080
User=web

[Install]
WantedBy=multi-user.target
"""


def _run(argv: list[str]) -> str:
    out = io.StringIO()
    assert main(argv, stdout=out) == EXIT_OK
    return out.getvalue()


def test_convert_prints_typed_declarations_with_opt_outs(tmp_path: Path) -> None:
    task = Task(
        "backup",
        "/usr/bin/backup",
        schedule=Calendar("daily"),
        user="backup",
        hardening=Hardening.none(),
    )
    for name, text in rendered(task).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "web.service").write_text(WEB, encoding="utf-8")
    files = [str(tmp_path / n) for n in ("backup.service", "backup.timer", "web.service")]
    source = _run(["units", "convert", *files])
    assert "# backup.service + backup.timer: Task (exact)" in source
    assert "# web.service: Service (exact)" in source
    assert 'restart="no"' in source and "hardening=Hardening.none()" in source
    namespace: dict[str, object] = {}
    exec(source, namespace)
    assert namespace["unit"] == replace(task, needs=())  # no runtime-init in the text
    assert rendered(namespace["unit_2"]) == {"web.service": WEB}  # type: ignore[arg-type]


def test_convert_keeps_text_and_lists_blockers(tmp_path: Path) -> None:
    unit = tmp_path / "web.service"
    unit.write_text(WEB.replace("User=web\n", "User=web\nOOMScoreAdjust=-500\n"), "utf-8")
    exact = _run(["units", "convert", str(unit)])
    assert "# web.service: kept as text, blocked by Service.OOMScoreAdjust" in exact
    assert 'unit = Unit("web.service", content=WEB_SERVICE)' in exact
    semantic = _run(["units", "convert", str(unit), "--mode", "semantic", "--name", "api.service"])
    assert "# api.service: Service (semantic); extra= holds Service.OOMScoreAdjust" in semantic
    assert 'extra={"Service": {"OOMScoreAdjust": "-500"}}' in semantic


def test_convert_adopt_defaults_shows_what_the_defaults_add(tmp_path: Path) -> None:
    (tmp_path / "web.service").write_text(WEB, encoding="utf-8")
    out = _run(["units", "convert", str(tmp_path / "web.service"), "--adopt-defaults"])
    assert "# web.service with the defaults (hardening, needs, restart, restart_sec):" in out
    assert "+++ web.service (defaults)" in out
    assert "+ProtectSystem=strict\n" in out and "+Restart=on-failure\n" in out


def test_render_prints_the_generated_unit(tmp_path: Path) -> None:
    recipe = tmp_path / "service.py"
    recipe.write_text(
        render_recipe_template(
            title="service", filename=recipe.name, release="trixie", backend="inprocess"
        ),
        encoding="utf-8",
    )
    out = _run(["units", "render", str(recipe), "--variant", "default", "--unit", "app.service"])
    assert out.startswith("# default/mkosi.extra/usr/lib/systemd/system/app.service\n[Unit]\n")
    assert "ExecStart=/usr/bin/app --config /etc/app/app.conf\n" in out


def test_render_names_a_missing_unit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recipe = tmp_path / "service.py"
    recipe.write_text(
        render_recipe_template(
            title="service", filename=recipe.name, release="trixie", backend="inprocess"
        ),
        encoding="utf-8",
    )
    argv = ["units", "render", str(recipe), "--variant", "default", "--unit", "nope.service"]
    assert main(argv, stdout=io.StringIO()) != EXIT_OK
    assert "generates no unit 'nope.service'" in capsys.readouterr().err
