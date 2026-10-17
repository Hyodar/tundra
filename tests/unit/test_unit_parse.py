"""``Unit.parse`` / ``convert``: unit text -> typed declarations (design 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit_corpus import constant_units, emitted_units, golden_units
from tundravm._modules.tdxs import Tdxs
from tundravm._units.parse import Conversion, Mode, convert, rendered, semantic
from tundravm.declarative import Calendar, DropIn, Every, Hardening, Mount, Service, Task, Unit


def _name(label: str) -> str:
    """The unit file name of a corpus label (``recipe:usr/lib/.../x.service``)."""
    parts = label.split(" ")[0].replace(":", "/").split("/")
    if len(parts) > 1 and parts[-2].endswith(".d"):
        return f"{parts[-2]}/{parts[-1]}"
    name = parts[-1]
    if "." in name:
        return name
    return name.lower() + (".target" if "TARGET" in name else ".service")


def _check(texts: dict[str, str], conversions: list[Conversion], mode: Mode) -> None:
    for conversion in conversions:
        got = {name: texts[name] for name in conversion.units}
        out = rendered(conversion.declaration, init=False)
        if mode == "exact" or not conversion.typed:
            assert out == got, conversion.units
        else:
            assert {n: semantic(t) for n, t in out.items()} == {
                n: semantic(t) for n, t in got.items()
            }, conversion.units


def _corpus(units: list[tuple[str, str]], mode: Mode, *, pair: bool = False) -> tuple[int, int]:
    """Convert every unit (with *pair*, each directory's units together, so sockets and
    timers meet their services) and check the guarantee; the typed and raw counts."""
    groups: dict[str, dict[str, str]] = {}
    for index, (label, text) in enumerate(units):
        key = label.rpartition("/")[0] if pair else str(index)
        groups.setdefault(key, {})[_name(label)] = text
    typed = raw = 0
    for texts in groups.values():
        conversions = convert(texts, mode=mode)
        _check(texts, conversions, mode)
        assert sorted(n for c in conversions for n in c.units) == sorted(texts)
        typed += sum(c.typed for c in conversions)
        raw += sum(not c.typed for c in conversions)
    return typed, raw


@pytest.mark.parametrize("mode", ["exact", "semantic"])
def test_repository_units_round_trip(mode: Mode) -> None:
    typed, raw = _corpus([*golden_units(), *constant_units()], mode)
    assert typed + raw == len([*golden_units(), *constant_units()])


@pytest.mark.parametrize("mode", ["exact", "semantic"])
def test_emitted_units_round_trip(tmp_path_factory: pytest.TempPathFactory, mode: Mode) -> None:
    units = list(emitted_units(Path(tmp_path_factory.mktemp("templates"))))
    typed, raw = _corpus(units, mode, pair=True)
    assert typed > 0
    print(f"{mode}: {typed} typed, {raw} raw of {len(units)} emitted units")


@pytest.mark.parametrize(
    "decl",
    [
        Service("app", "/usr/bin/app --port 8080", user="app"),
        Service("web", "/usr/bin/web", user="root", hardening=Hardening.none(), restart="no"),
        Task("backup", "/usr/bin/backup", schedule=Every("1h"), user="backup"),
        Task("rotate", "/usr/bin/rotate", schedule=Calendar("daily"), user="rotate"),
        Task("migrate", "/usr/bin/migrate", user="migrate", timeout=None),
        Mount("/var/lib/app/cache", "tmpfs", type="tmpfs", options=("size=1G",)),
        Unit("ssh.service", dropin=DropIn(restart="always", limits={"NOFILE": "4096"})),
    ],
    ids=lambda decl: type(decl).__name__,
)
@pytest.mark.parametrize("init", [False, True])
@pytest.mark.parametrize("mode", ["exact", "semantic"])
def test_canonical_renderings_parse_back(decl: object, init: bool, mode: Mode) -> None:
    texts = rendered(decl, init=init)  # type: ignore[arg-type]
    [conversion] = convert(texts, mode=mode, init=init)
    assert conversion.typed, conversion.blockers
    assert rendered(conversion.declaration, init=init) == texts
    assert type(conversion.declaration) is type(decl)


def test_timer_pair_converts_to_a_scheduled_task() -> None:
    texts = rendered(Task("backup", "/usr/bin/backup", schedule=Every("1h"), user="backup"))
    [conversion] = convert(texts)
    assert conversion.units == ("backup.service", "backup.timer")
    assert conversion.declaration == Task(
        "backup", "/usr/bin/backup", schedule=Every("1h"), user="backup"
    )


def test_mount_and_dropin_convert() -> None:
    mount = "[Unit]\nDescription=/srv\n\n[Mount]\nWhat=tmpfs\nWhere=/srv\nType=tmpfs\n"
    [conversion] = convert({"srv.mount": mount})
    assert conversion.declaration == Mount("/srv", "tmpfs", type="tmpfs", enabled=False)
    dropin = "[Service]\nRestart=always\nOOMScoreAdjust=-500\n"
    [exact] = convert({"x.service.d/30-oom.conf": dropin})
    assert not exact.typed and exact.blockers == ("Service.OOMScoreAdjust",)
    [loose] = convert({"x.service.d/30-oom.conf": dropin}, mode="semantic")
    assert loose.declaration == Unit(
        "x.service",
        dropin=DropIn("30-oom", restart="always", extra={"Service": {"OOMScoreAdjust": "-500"}}),
    )
    assert loose.extra == ("Service.OOMScoreAdjust",)


def test_tdxs_pair_stays_text_and_names_its_blockers() -> None:
    texts = {
        "tdxs.service": Tdxs().render_service_unit(),
        "tdxs.socket": Tdxs().render_socket_unit(),
    }
    for mode in ("exact", "semantic"):
        conversions = convert(texts, mode=mode)
        assert [c.typed for c in conversions] == [False, False]
        assert all(c.blockers for c in conversions)
        assert {c.declaration for c in conversions} == {Unit(n, t) for n, t in texts.items()}


def test_unit_parse_and_opt_outs() -> None:
    text = "[Unit]\nDescription=Web\n\n[Service]\nType=simple\nExecStart=/usr/bin/web\nUser=web\n"
    decl = Unit.parse(text, name="web.service")
    assert isinstance(decl, Service)
    assert decl.hardening == Hardening.none() and decl.restart == "no" and not decl.enabled
    assert rendered(decl) == {"web.service": text}
    assert Unit.parse("[Unit]\nDescription=x\n", name="x.target") == Unit(
        "x.target", "[Unit]\nDescription=x\n"
    )
