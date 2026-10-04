"""Shortcuts for running the tundravm CLI on this recipe.

Usage:
    python -m examples.surge-tdx-prover compile [--force]  # emit mkosi/ next to this file
    python -m examples.surge-tdx-prover bake [--force]     # compile, lock, then bake --frozen

For anything else, call the CLI directly, e.g.
``tundravm explain examples/surge-tdx-prover/image.py --profile azure``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tundravm.cli import main

HERE = Path(__file__).resolve().parent
RECIPE = [str(HERE / "image.py"), "--pythonpath", str(HERE.parent.parent)]
COMPILE = ["compile", *RECIPE, "--out", str(HERE / "mkosi")]
BAKE = ["bake", *RECIPE, "--lock"]


def run(argv: list[str]) -> int:
    command, *flags = argv or [""]
    if command == "compile":
        return main([*COMPILE, *flags])
    if command == "bake":
        return main([*COMPILE, *flags]) or main([*BAKE, *flags])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
