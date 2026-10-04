"""Locking is a batch: every source is attempted and one error reports each failure."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import tundravm._source as source_module
from tundravm._source import GitSource, Source
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import Lock, lock, lock_status, read_lock, write_lock
from tundravm.errors import ErrorCode, LockfileError, SourceError
from tundravm.recipe import load_recipe

RAIKO = "https://github.com/taikoxyz/raiko"
NETHERMIND = "https://github.com/NethermindEth/nethermind"
TOOL = "https://example.com/tool-1.0.tar.gz"
SHA = "c" * 40
DIGEST = "d" * 64

RECIPE = f"""
from tundravm.declarative import Build, Fragment, Git, Http, Install, Recipe

recipe = Recipe(
    "prover",
    Fragment(
        "prover",
        items=(
            Build("raiko", Git({RAIKO!r}, "feat/tdx"), script="make",
                  install=(Install("raiko", "/usr/bin/raiko"),)),
            Build("nethermind", Git({NETHERMIND!r}, "master"), script="make",
                  install=(Install("nethermind", "/usr/bin/nethermind"),)),
            Build("tool", Http({TOOL!r}), script="make",
                  install=(Install("tool", "/usr/bin/tool"),)),
        ),
    ),
)
"""

RAIKO_LINE = f"  raiko: git {RAIKO} @ feat/tdx: ref 'feat/tdx' not found"
TOOL_LINE = f"  tool: http {TOOL}: HTTP 404"
HINT = (
    "Fix the refs above, or drop NAME from --update to keep its existing pin, "
    "or pass --offline to reuse existing pins."
)


class _Upstream:
    """A resolver whose upstream lost the raiko branch and the tool tarball."""

    def __init__(self, *, broken: bool = True) -> None:
        self.broken = broken
        self.calls: list[str] = []

    def __call__(self, source: Source) -> str:
        self.calls.append(source.url)
        reason = {RAIKO: "ref 'feat/tdx' not found", TOOL: "HTTP 404"}.get(source.url)
        if self.broken and reason is not None:
            described = source.describe()
            raise SourceError(
                f"Cannot resolve {described}: {reason}.",
                source=described,
                reason=reason,
                hint="Fix it upstream.",
            )
        return SHA if isinstance(source, GitSource) else DIGEST


@pytest.fixture
def recipe_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "image.py"
    path.write_text(RECIPE, encoding="utf-8")
    return path


def _cli(monkeypatch: pytest.MonkeyPatch, upstream: _Upstream, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(source_module, "default_resolver", upstream)
    out = io.StringIO()
    return main(["lock", *argv], stdout=out), out.getvalue()


def test_lock_attempts_every_source_and_reports_each_failure(recipe_file: Path) -> None:
    upstream = _Upstream()
    with pytest.raises(LockfileError) as caught:
        lock(load_recipe(recipe_file), resolver=upstream)
    error = caught.value
    assert sorted(upstream.calls) == sorted([RAIKO, NETHERMIND, TOOL])
    assert error.code == ErrorCode.LOCKFILE
    assert error.args[0].splitlines() == [
        "Cannot lock: 2 sources could not be resolved:",
        RAIKO_LINE,
        TOOL_LINE,
        "1 of 3 sources resolved; nothing written.",
    ]
    assert error.hint == HINT
    assert {name: e.reason for name, e in error.failures.items()} == {
        "raiko": "ref 'feat/tdx' not found",
        "tool": "HTTP 404",
    }
    assert all(e.code == ErrorCode.SOURCE for e in error.failures.values())


def test_other_resolver_errors_are_collected_too(recipe_file: Path) -> None:
    def flaky(source: Source) -> str:
        if source.url == NETHERMIND:
            raise TimeoutError("timed out")
        return SHA if isinstance(source, GitSource) else DIGEST

    with pytest.raises(LockfileError) as caught:
        lock(load_recipe(recipe_file), resolver=flaky)
    assert f"  nethermind: git {NETHERMIND} @ master: timed out" in str(caught.value)
    assert "2 of 3 sources resolved; nothing written." in str(caught.value)


def test_cli_lock_failure_exits_2_and_writes_nothing(
    recipe_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = recipe_file.parent / "out" / "tundravm.lock"
    code, out = _cli(monkeypatch, _Upstream(), str(recipe_file), "--path", str(target))
    assert (code, out) == (EXIT_SDK_ERROR, "")
    assert not target.exists()
    err = capsys.readouterr().err
    assert err.startswith("error [E_LOCKFILE]: Cannot lock: 2 sources could not be resolved:\n")
    assert f"{RAIKO_LINE}\n{TOOL_LINE}\n1 of 3 sources resolved; nothing written.\n" in err
    assert f"Hint: {HINT}" in err


def test_cli_lock_failure_keeps_the_existing_lockfile(
    recipe_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = recipe_file.parent / "tundravm.lock"
    write_lock(lock(load_recipe(recipe_file), resolver=_Upstream(broken=False)), target)
    before = target.read_bytes()
    argv = (str(recipe_file), "--path", str(target), "--update", "raiko")
    assert _cli(monkeypatch, _Upstream(), *argv)[0] == EXIT_SDK_ERROR
    assert target.read_bytes() == before


def test_cli_lock_github_format_annotates_each_failed_source(
    recipe_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = recipe_file.parent / "tundravm.lock"
    argv = (str(recipe_file), "--path", str(target), "--format", "github")
    code, out = _cli(monkeypatch, _Upstream(), *argv)
    assert code == EXIT_SDK_ERROR
    assert out.splitlines() == [
        f"::error file=image.py,title=E_SOURCE::{RAIKO_LINE.strip()}",
        f"::error file=image.py,title=E_SOURCE::{TOOL_LINE.strip()}",
    ]
    assert not target.exists()


def test_cli_lock_success_path_is_unchanged(
    recipe_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = recipe_file.parent / "tundravm.lock"
    upstream = _Upstream(broken=False)
    code, out = _cli(monkeypatch, upstream, str(recipe_file), "--path", str(target))
    assert (code, out) == (EXIT_OK, f"locked {target}\n")
    pins = {pin.identity: pin.digest for pin in read_lock(target).pins}
    assert pins == {"nethermind": SHA, "raiko": SHA, "tool": DIGEST}
    assert len(upstream.calls) == 3


def test_offline_lock_reports_every_unpinned_source_at_once(recipe_file: Path) -> None:
    recipe = load_recipe(recipe_file)
    full = lock(recipe, resolver=_Upstream(broken=False))
    partial = read_lock_without(full, "raiko", "tool", tmp=recipe_file.parent)
    with pytest.raises(LockfileError) as caught:
        lock(recipe, previous=partial, offline=True)
    assert caught.value.args[0].splitlines() == [
        "Cannot lock offline: 2 sources need the network to resolve:",
        f"  raiko: git {RAIKO} @ feat/tdx: not pinned in the lockfile",
        f"  tool: http {TOOL}: not pinned in the lockfile",
        "1 of 3 sources resolved; nothing written.",
    ]
    assert sorted(caught.value.failures) == ["raiko", "tool"]


def test_lock_check_names_unpinned_sources_without_the_network(
    recipe_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = load_recipe(recipe_file)
    target = recipe_file.parent / "tundravm.lock"
    full = lock(recipe, resolver=_Upstream(broken=False))
    write_lock(read_lock_without(full, "raiko", tmp=recipe_file.parent), target)
    found = {d.subject: d.message for d in lock_status(recipe, read_lock(target))}
    assert found == {"sources.raiko": "source raiko is not pinned"}
    # conftest refuses network resolution; --check must not need it.
    out = io.StringIO()
    code = main(["lock", str(recipe_file), "--path", str(target), "--check"], stdout=out)
    assert (code, out.getvalue()) == (
        EXIT_FAILURE,
        "+ sources.raiko: source raiko is not pinned\n",
    )


def read_lock_without(full: Lock, *names: str, tmp: Path) -> Lock:
    """*full* re-read with the fetch entries of *names* dropped."""
    path = tmp / "partial.lock"
    write_lock(full, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetches"] = [f for f in data["fetches"] if f.get("name") not in names]
    path.write_text(json.dumps(data), encoding="utf-8")
    return read_lock(path)
