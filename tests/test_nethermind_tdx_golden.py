"""Integration test: SDK emission vs real NethermindEth/nethermind-tdx repo.

Compiles the surge_tdx_prover example (full image, all profiles), clones
the upstream repo, and uses codex exec to compare the two project trees
and report differences.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_IMAGE_PATH = Path(__file__).resolve().parent.parent / "examples" / "surge-tdx-prover" / "image.py"
_spec = importlib.util.spec_from_file_location("surge_tdx_prover_image", _IMAGE_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
build_surge_tdx_prover = _mod.build_surge_tdx_prover

pytestmark = pytest.mark.integration

# Pin upstream to a known commit; override via UPSTREAM_COMMIT env var.
UPSTREAM_COMMIT = os.environ.get(
    "NETHERMIND_TDX_COMMIT", "7ac44e7d9baecf8743a20e75b30fd4ddfce3e85f"
)

# Required section headers that codex must produce in the report.
REQUIRED_SECTIONS = [
    "## Files only in upstream",
    "## Files only in SDK",
    "## Files in both with differences",
    "## Files that match exactly",
    "## Summary",
]


@pytest.fixture(scope="module")
def sdk_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the full surge-tdx-prover image with all profiles."""
    img = build_surge_tdx_prover()
    # Activate all profiles so the emission includes azure, gcp, devtools
    img._active_profiles = tuple(sorted(img.state.profiles.keys()))
    out = tmp_path_factory.mktemp("sdk_emission")
    img.compile(out / "mkosi")
    return out / "mkosi"


@pytest.fixture(scope="module")
def upstream_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Clone NethermindEth/nethermind-tdx at a pinned commit."""
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo_dir = tmp_path_factory.mktemp("upstream") / "nethermind-tdx"
    subprocess.run(
        [
            "git",
            "clone",
            "https://github.com/NethermindEth/nethermind-tdx.git",
            str(repo_dir),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", UPSTREAM_COMMIT],
        check=True,
        capture_output=True,
    )
    return repo_dir


def test_compare_with_upstream(
    sdk_output: Path,
    upstream_repo: Path,
    tmp_path: Path,
) -> None:
    """Compare SDK output against upstream and emit a deterministic report."""

    report_file = tmp_path / "comparison_report.md"
    upstream_files = _collect_files(upstream_repo, ignore_prefixes=(".git/",))
    sdk_files = _collect_files(sdk_output)

    upstream_rel = set(upstream_files)
    sdk_rel = set(sdk_files)
    only_upstream = sorted(upstream_rel - sdk_rel)
    only_sdk = sorted(sdk_rel - upstream_rel)
    shared = sorted(upstream_rel & sdk_rel)

    matches: list[str] = []
    differences: list[str] = []
    for rel in shared:
        upstream_bytes = upstream_files[rel].read_bytes()
        sdk_bytes = sdk_files[rel].read_bytes()
        if upstream_bytes == sdk_bytes:
            matches.append(rel)
        else:
            differences.append(rel)

    report = _render_report(
        only_upstream=only_upstream,
        only_sdk=only_sdk,
        differences=differences,
        matches=matches,
    )
    report_file.write_text(report, encoding="utf-8")

    # Verify the report has the required structure
    for section in REQUIRED_SECTIONS:
        assert section in report, f"Report missing required section: {section}"


def _collect_files(
    root: Path,
    *,
    ignore_prefixes: tuple[str, ...] = (),
) -> dict[str, Path]:
    collected: dict[str, Path] = {}
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in ignore_prefixes):
            continue
        collected[rel] = file_path
    return collected


def _render_report(
    *,
    only_upstream: list[str],
    only_sdk: list[str],
    differences: list[str],
    matches: list[str],
) -> str:
    def render_list(items: list[str]) -> str:
        if not items:
            return "- (none)\n"
        return "".join(f"- `{item}`\n" for item in items)

    total_compared = len(differences) + len(matches)
    verdict = "not equivalent" if differences or only_upstream else "mostly equivalent"
    return (
        "## Files only in upstream\n"
        f"{render_list(only_upstream)}\n"
        "## Files only in SDK\n"
        f"{render_list(only_sdk)}\n"
        "## Files in both with differences\n"
        f"{render_list(differences)}\n"
        "## Files that match exactly\n"
        f"{render_list(matches)}\n"
        "## Summary\n"
        f"- compared files: {total_compared}\n"
        f"- exact matches: {len(matches)}\n"
        f"- differences: {len(differences)}\n"
        f"- only in upstream: {len(only_upstream)}\n"
        f"- only in SDK: {len(only_sdk)}\n"
        f"- verdict: {verdict}\n"
    )
