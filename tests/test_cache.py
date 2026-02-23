import json
from pathlib import Path
from typing import Any, cast

import pytest

from tdx import Image
from tdx.cache import BuildCacheInput, BuildCacheStore, cache_key
from tdx.errors import ReproducibilityError


def test_cache_key_includes_all_canonical_inputs() -> None:
    base = BuildCacheInput(
        source_hash="src-hash",
        source_tree="tree-hash",
        toolchain="go1.22",
        flags=("-trimpath",),
        dependencies=("libssl-dev",),
        env={"CGO_ENABLED": "0"},
        target="qemu",
    )
    changed_env = BuildCacheInput(
        source_hash="src-hash",
        source_tree="tree-hash",
        toolchain="go1.22",
        flags=("-trimpath",),
        dependencies=("libssl-dev",),
        env={"CGO_ENABLED": "1"},
        target="qemu",
    )

    assert cache_key(base) != cache_key(changed_env)


def test_cache_manifest_verification_detects_mismatch(tmp_path: Path) -> None:
    store = BuildCacheStore(tmp_path / "cache")
    inputs = BuildCacheInput(
        source_hash="src",
        source_tree="tree",
        toolchain="tool",
        flags=(),
        dependencies=(),
        env={},
        target="qemu",
    )
    key = store.save(inputs=inputs, artifact=b"payload")

    manifest_path = tmp_path / "cache" / key / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["toolchain"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReproducibilityError):
        store.load(key=key, expected_inputs=inputs)


def test_bake_produces_build_report(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend="inprocess")
    image.output_targets("qemu")

    result = image.bake()
    report = _read_report(result.profiles["default"].report_path)

    assert report["profile"] == "default"
    assert report["backend"] == "inprocess"
    assert "artifact_digests" in report
    assert "debloat" in report


def _read_report(path: Path | None) -> dict[str, Any]:
    assert path is not None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)
