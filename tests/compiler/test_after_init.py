"""Units declared with ``after_init`` wait for runtime-init only when the variant has one."""

from pathlib import Path

from tundravm.declarative import Declaration, Fragment, Key, Recipe, Unit
from tundravm.testing import compile_tree


def _unit_text(tmp_path: Path, content: str, *extra: Declaration) -> str:
    unit = Unit("app.service", content, enabled=True, after_init=True)
    recipe = Recipe("app", Fragment("app", items=(unit, *extra)))
    return compile_tree(recipe, path=tmp_path / "tree").unit("app.service")


def _after(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("After="))


APP_UNIT = "[Unit]\nDescription=App\nAfter=network.target\n\n[Service]\nExecStart=/usr/bin/app\n"


def test_after_init_prepends_init_when_scripts_present(tmp_path: Path) -> None:
    text = _unit_text(tmp_path, APP_UNIT, Key("k"))

    assert _after(text) == "After=runtime-init.service network.target"
    assert "Requires=runtime-init.service" in text.splitlines()


def test_after_init_noop_when_no_init(tmp_path: Path) -> None:
    text = _unit_text(tmp_path, APP_UNIT)

    assert _after(text) == "After=network.target"
    assert text == APP_UNIT


def test_after_init_no_duplicate_if_already_present(tmp_path: Path) -> None:
    content = APP_UNIT.replace(
        "After=network.target",
        "After=runtime-init.service network.target\nRequires=runtime-init.service",
    )
    text = _unit_text(tmp_path, content, Key("k"))

    assert _after(text) == "After=runtime-init.service network.target"
    assert text == content
