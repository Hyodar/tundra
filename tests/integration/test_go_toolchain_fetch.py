"""The Go toolchain fetch against the real proxy.golang.org and sum.golang.org.

It downloads Go 1.25.0 (64 MB). Run with:
uv run pytest tests/integration/test_go_toolchain_fetch.py -m integration
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest

from tundravm import _go_toolchain as go


@pytest.mark.integration
@pytest.mark.skipif(platform.machine() != "x86_64", reason="tundravm fetches Go for amd64")
def test_go_1_25_0_is_fetched_from_the_proxy_and_vouched_for_by_the_checksum_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live tree head verifies, the record is proven from live tiles, the zip arrives
    through the proxy's redirect to its storage, hashes to the record, and runs."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = go.fetch_go("go1.25.0")
    env = {"GOTOOLCHAIN": "local", "GOROOT": str(root), "PATH": "/usr/bin:/bin"}
    completed = subprocess.run(
        [root / "bin" / "go", "version"], env=env, capture_output=True, text=True, check=True
    )
    assert completed.stdout.split()[2:4] == ["go1.25.0", "linux/amd64"]
    marker = (root / go.VERIFIED).read_text(encoding="utf-8")
    assert (
        marker == f"{go.host_module('go1.25.0')} h1:wVC9wx2XOcP5gHiN8ZzfyTfjlrDLSS7Hu1wjI01n68U=\n"
    )
