"""``UnitText`` is lossless: every unit text in the repository renders back byte for byte."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit_corpus import constant_units, emitted_units, golden_units
from tundravm._units import Directive, Other, UnitText

TRICKY = {
    "empty": "",
    "newline only": "\n",
    "no trailing newline": "[Unit]\nDescription=x",
    "two trailing newlines": "[Unit]\nDescription=x\n\n",
    "preamble comment": "# header\n; other\n\n[Unit]\nDescription=x\n",
    "continuation": "[Service]\nExecStart=/bin/a \\\n    --b \\\n--c\n",
    "comment in continuation": "[Service]\nExecStart=/bin/a \\\n# note\n  --b\n",
    "trailing backslash at eof": "[Service]\nExecStart=/bin/a \\",
    "crlf": "[Unit]\r\nDescription=x\r\n",
    "junk and spacing": "[Unit]\n  Description = spaced  \nnot a directive\n\t\n",
    "empty assignment": "[Unit]\nAfter=a\nAfter=\nAfter=b\n",
    "duplicate sections": "[Unit]\nAfter=a\n[Unit]\nAfter=b\n",
}


@pytest.mark.parametrize("name", sorted(TRICKY))
def test_tricky_text_round_trips(name: str) -> None:
    assert UnitText.parse(TRICKY[name]).text == TRICKY[name]


def test_values_follow_systemd_syntax() -> None:
    unit = UnitText.parse(TRICKY["comment in continuation"])
    (directive,) = unit.directives("Service")
    assert directive == Directive(
        "ExecStart", "/bin/a --b", ("ExecStart=/bin/a \\", "# note", "  --b"), 2
    )
    assert UnitText.parse(TRICKY["empty assignment"]).values("Unit", "After") == ("b",)
    assert UnitText.parse(TRICKY["duplicate sections"]).values("Unit", "After") == ("a", "b")
    spaced = UnitText.parse(TRICKY["junk and spacing"])
    assert [d.key for d in spaced.directives()] == ["Description"]
    assert next(spaced.directives()).value == "spaced"
    junk = [e for e in spaced.sections[0].entries if isinstance(e, Other) and e.is_junk]
    assert [e.raw for e in junk] == ["not a directive"]


def test_no_trailing_newline_is_kept() -> None:
    assert not UnitText.parse("[Unit]\nA=b").trailing_newline
    assert UnitText.parse("[Unit]\nA=b\n").trailing_newline


@pytest.mark.parametrize(
    ("name", "text"), [*golden_units(), *constant_units()], ids=lambda value: str(value)[:60]
)
def test_repository_unit_round_trips(name: str, text: str) -> None:
    assert UnitText.parse(text).text == text, name


def test_every_emitted_unit_round_trips(tmp_path_factory: pytest.TempPathFactory) -> None:
    units = emitted_units(Path(tmp_path_factory.mktemp("templates")))
    assert len(units) > 50
    assert [name for name, text in units if UnitText.parse(text).text != text] == []
