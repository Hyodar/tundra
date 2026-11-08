"""The attested node: its stages build up to ``image.py``, ``new`` writes the first one, and
its signing proxy vets, tests and builds."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import tundravm
from tests.compiler.test_native_tree import by_image, mkosi_summary
from tests.helpers import ATTESTED_NODE, EXAMPLE_RECIPES
from tundravm.declarative import lint
from tundravm.recipe import load_file
from tundravm.templates import render_recipe_template
from tundravm.testing import plan

ALLOWED = frozenset({"observability-sink-unset", "observability-export-unset"})
"""The warnings a stage reports: stage 6's production profiles ship the journal and the
metrics to in-guest receivers, keeping the metrics' health preset, until the operator
passes each ``url=`` and ``export=``."""


def _names(path: Path) -> tuple[set[str], set[str]]:
    image = tundravm.load(path)
    return {c.name for c in image.components}, {s.name for s in image.services}


@pytest.mark.parametrize(
    "index", range(len(EXAMPLE_RECIPES)), ids=lambda i: EXAMPLE_RECIPES[i].stem
)
def test_each_stage_lints_clean_and_keeps_what_the_last_one_declared(index: int) -> None:
    stage = EXAMPLE_RECIPES[index]
    found = lint(load_file(stage).recipe)
    assert [d.code for d in found if d.severity == "error"] == []
    assert {d.code for d in found if d.severity == "warning"} <= ALLOWED
    if index:
        before, now = _names(EXAMPLE_RECIPES[index - 1]), _names(stage)
        assert before[0] <= now[0] and before[1] <= now[1], stage.name


def test_new_writes_the_first_stage_and_image_py_is_the_last() -> None:
    first = render_recipe_template(
        title="attested-node", filename="attested-node.py", release="trixie"
    )
    assert first == EXAMPLE_RECIPES[0].read_text(encoding="utf-8")
    last = EXAMPLE_RECIPES[-1].read_text(encoding="utf-8")
    image = (ATTESTED_NODE / "image.py").read_text(encoding="utf-8")
    assert image == last.replace('Local("../signer")', 'Local("signer")')
    assert len(image.splitlines()) <= 80


def test_the_image_resolves_every_profile_and_wrapper() -> None:
    images = by_image(
        mkosi_summary(ATTESTED_NODE / "mkosi", "--dependency=azure", "--dependency=gcp")
    )
    assert list(images) == ["default-initrd", "payload", "azure", "gcp", "main"]
    assert images["payload"]["ImageId"] == "attested-node"
    planned = plan(tundravm.load(ATTESTED_NODE / "image.py"), "production")
    for unit in ("nethermind", "lighthouse", "signer", "attested-tls-8546", "tdxs"):
        assert planned.service(unit).get("Service", "User") == unit
    jwt = "LoadCredential=jwt:/run/tundravm/secrets/jwt"
    assert jwt in planned.unit("nethermind.service").text
    assert jwt in planned.unit("lighthouse.service").text


def test_the_signer_compiles_and_its_tests_pass() -> None:
    go = shutil.which("go")
    if go is None:
        pytest.skip("no go on PATH to vet, test and build the signing proxy's signer/")
    for argv in (("vet", "./..."), ("test", "./..."), ("build", "-o", os.devnull, "./...")):
        done = subprocess.run(
            [go, *argv], cwd=ATTESTED_NODE / "signer", capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"go {' '.join(argv)}: {done.stdout}{done.stderr}"
