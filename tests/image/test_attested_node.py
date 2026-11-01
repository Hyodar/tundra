"""The attested node example: its signing proxy vets, tests and builds."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.helpers import REPO_ROOT

EXAMPLE = REPO_ROOT / "examples" / "attested-node"


def test_the_signer_compiles_and_its_tests_pass() -> None:
    go = shutil.which("go")
    if go is None:
        pytest.skip("no go on PATH to vet, test and build the signing proxy's signer/")
    for argv in (("vet", "./..."), ("test", "./..."), ("build", "-o", os.devnull, "./...")):
        done = subprocess.run(
            [go, *argv], cwd=EXAMPLE / "signer", capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"go {' '.join(argv)}: {done.stdout}{done.stderr}"
