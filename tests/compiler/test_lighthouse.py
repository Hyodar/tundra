"""Lighthouse beside Nethermind, sharing a ``Generated`` Engine API secret.

The beacon node's unit, its drop-in and the oneshot that writes the secret are compared
with ``lighthouse-units.txt`` beside this file (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it).
"""

from __future__ import annotations

import os
from pathlib import Path

import tundravm
from tundravm import Image
from tundravm._image import lower
from tundravm.components import Lighthouse, Nethermind
from tundravm.secrets import Generated
from tundravm.storage import OperatorKey, Volume
from tundravm.testing._model import CompiledTree

GOLDEN = Path(__file__).with_name("lighthouse-units.txt")
UNITS = (
    "lighthouse.service",
    "lighthouse.service.d/20-tundravm.conf",
    "tundravm-generated-jwt.service",
)

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
execution = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
beacon = Lighthouse(
    execution=execution.engine_api,
    jwt=jwt,
    checkpoint_sync_url="https://checkpoint.example",
    state=data.directory("lighthouse"),
)


def test_the_clients_share_a_secret_the_image_generates(tmp_path: Path) -> None:
    image = Image("node", components=[execution, beacon])
    compiled = tundravm.compile(image)
    compiled.write(tmp_path / "tree")
    tree = CompiledTree(tmp_path / "tree", compiled.profiles, "production")
    text = "".join(f"# {unit}\n{tree.unit(unit)}\n" for unit in UNITS)
    if os.environ.get("TUNDRAVM_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")
    nethermind = tree.unit("nethermind.service")
    assert "LoadCredential=jwt:/run/tundravm/secrets/jwt" in nethermind
    assert "tundravm-generated-jwt.service" in nethermind
    assert not image.secrets
    assert lower(image).source_builds()["lighthouse"].source.url == beacon.source.url
