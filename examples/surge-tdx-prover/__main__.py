"""CLI for the surge-tdx-prover image.

Usage:
    python examples/surge-tdx-prover compile
    python examples/surge-tdx-prover bake
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from image import build_surge_tdx_prover  # noqa: E402

MKOSI_DIR = _HERE / "mkosi"


def cmd_compile(args: argparse.Namespace) -> None:
    img = build_surge_tdx_prover()
    img.compile(MKOSI_DIR, force=args.force)
    print(f"Compiled to {MKOSI_DIR}")


def cmd_bake(args: argparse.Namespace) -> None:
    img = build_surge_tdx_prover()
    img.compile(MKOSI_DIR, force=args.force)
    img.lock()
    img.bake(frozen=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="surge-tdx-prover image builder")
    sub = parser.add_subparsers(dest="command", required=True)

    compile_p = sub.add_parser("compile", help="Compile image definition to mkosi tree")
    compile_p.add_argument("--force", action="store_true", help="Force recompilation")

    bake_p = sub.add_parser("bake", help="Compile, lock, and bake the image")
    bake_p.add_argument("--force", action="store_true", help="Force recompilation")

    args = parser.parse_args()
    if args.command == "compile":
        cmd_compile(args)
    elif args.command == "bake":
        cmd_bake(args)


if __name__ == "__main__":
    main()
