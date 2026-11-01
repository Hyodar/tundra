"""A profile edit builds what the edited service would have built, declared so.

One image (``db`` with a TCP probe, ``api`` requiring it and exposing 8080, ``tick`` on a
timer, ``web`` with a probe, a drop-in and a credential) and one ``staging`` edit per kind,
or the removal of ``web``, ``api`` and ``tick``: the profile overlays, every file under
``payload/mkosi.profiles/``, are compared with ``edit-overlays/<kind>.txt`` beside this file
(``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites them). What an edit makes obsolete leaves the shared
payload for the overlay of the profile that keeps it: ``readiness=Notify()`` moves the probe
unit into ``production``'s, and ``api`` waits for ``db.service`` itself. A removal moves
every unit and user of the service there, and closes its ports in ``staging``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import tundravm
from tundravm import Image, Profile, Service
from tundravm._image._change import Edit, Remove
from tundravm.ports import TCP
from tundravm.secrets import Secret
from tundravm.systemd import DropIn, Every, Notify, TcpProbe

GOLDENS = Path(__file__).with_name("edit-overlays")
OVERLAYS = Path("payload/mkosi.profiles")


@pytest.mark.parametrize("kind", ["readiness", "credentials", "unit", "timer", "expose", "remove"])
def test_an_edit_regenerates_what_its_keyword_lowers_to(kind: str, tmp_path: Path) -> None:
    db = Service("db", ["/usr/bin/db"], readiness=TcpProbe(5432))
    api = Service("api", ["/usr/bin/api"], requires=[db.ready], expose=[TCP(8080)])
    tick = Service("tick", ["/usr/bin/tick"], timer=Every("1h"))
    web = Service(
        "web",
        ["/usr/bin/web"],
        readiness=TcpProbe(9000),
        credentials=[Secret("key")],
        unit=DropIn(restart="always"),
    )
    kinds: dict[str, list[Edit | Remove]] = {
        "readiness": [db.ref.edit(readiness=Notify())],
        "credentials": [api.ref.edit(credentials=[Secret("token")])],
        "unit": [api.ref.edit(unit=DropIn(restart="always"))],
        "timer": [tick.ref.edit(timer=Every("15m"))],
        "expose": [api.ref.edit(expose=[TCP(8080), TCP(9090)])],
        "remove": [web.ref.remove(), api.ref.remove(), tick.ref.remove()],
    }
    staging = Profile(changes=kinds[kind])
    image = Image("edits", services=[db, api, tick, web], profiles={"staging": staging})
    tundravm.compile(image).write(tmp_path)
    root = tmp_path / OVERLAYS
    overlays = "".join(
        f"== {path.relative_to(root)}\n{path.read_text(encoding='utf-8')}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    golden = GOLDENS / f"{kind}.txt"
    if os.environ.get("TUNDRAVM_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(overlays, encoding="utf-8")
    assert overlays == golden.read_text(encoding="utf-8")
