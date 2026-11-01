"""Locking is a batch: every source is attempted and one error reports each failure."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import tundravm._source as source_module
from tundravm._source import GitSource, Source
from tundravm.cli import EXIT_ERROR, EXIT_FAILURE, main
from tundravm.declarative import Lock, lock, lock_status, read_lock, write_lock
from tundravm.errors import ErrorCode, LockfileError, SourceError
from tundravm.recipe import load_recipe

PROVER = "https://github.com/example/prover"
NODE = "https://github.com/example/node"
TOOL = "https://example.com/tool-1.0.tar.gz"
SHA = "c" * 40
DIGEST = "d" * 64

RECIPE = f"""
from tundravm.declarative import Build, Fragment, Git, Http, Install, Recipe

recipe = Recipe(
    "prover",
    items=(
        Build("prover", Git({PROVER!r}, "feat/tdx"), script="make",
              install=(Install("prover", "/usr/bin/prover"),)),
        Build("node", Git({NODE!r}, "master"), script="make",
              install=(Install("node", "/usr/bin/node"),)),
        Build("tool", Http({TOOL!r}), script="make",
              install=(Install("tool", "/usr/bin/tool"),)),
    ),
)
"""

PROVER_LINE = f"  prover: git {PROVER} @ feat/tdx: ref 'feat/tdx' not found"
TOOL_LINE = f"  tool: http {TOOL}: HTTP 404"
HINT = "Fix it upstream. A source left out of `tundravm lock --update NAME` keeps its pin."


class _Upstream:
    """A resolver whose upstream lost the prover branch and the tool tarball."""

    def __init__(self, *, broken: bool = True) -> None:
        self.broken = broken
        self.calls: list[str] = []

    def __call__(self, source: Source) -> str:
        self.calls.append(source.url)
        reason = {PROVER: "ref 'feat/tdx' not found", TOOL: "HTTP 404"}.get(source.url)
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
def image_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "image.py"
    path.write_text(RECIPE, encoding="utf-8")
    return path


def _cli(monkeypatch: pytest.MonkeyPatch, upstream: _Upstream, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(source_module, "default_resolver", upstream)
    out = io.StringIO()
    return main(["lock", *argv], stdout=out), out.getvalue()


def test_lock_attempts_every_source_and_reports_each_failure(image_file: Path) -> None:
    upstream = _Upstream()
    with pytest.raises(LockfileError) as caught:
        lock(load_recipe(image_file), resolver=upstream)
    error = caught.value
    assert sorted(upstream.calls) == sorted([PROVER, NODE, TOOL])
    assert error.code == ErrorCode.LOCKFILE
    assert error.args[0].splitlines() == [
        "Cannot lock: 2 sources could not be resolved:",
        PROVER_LINE,
        TOOL_LINE,
        "1 of 3 sources resolved; nothing written.",
    ]
    assert error.hint == HINT
    assert {name: e.reason for name, e in error.failures.items()} == {
        "prover": "ref 'feat/tdx' not found",
        "tool": "HTTP 404",
    }
    assert all(e.code == ErrorCode.SOURCE for e in error.failures.values())


def test_other_resolver_errors_are_collected_too(image_file: Path) -> None:
    def flaky(source: Source) -> str:
        if source.url == NODE:
            raise TimeoutError("timed out")
        return SHA if isinstance(source, GitSource) else DIGEST

    with pytest.raises(LockfileError) as caught:
        lock(load_recipe(image_file), resolver=flaky)
    assert f"  node: git {NODE} @ master: timed out" in str(caught.value)
    assert "2 of 3 sources resolved; nothing written." in str(caught.value)


@pytest.mark.parametrize(
    ("existing", "update", "report"),
    [
        (
            False,
            (),
            "Cannot lock: 2 sources could not be resolved:\n"
            f"{PROVER_LINE}\n{TOOL_LINE}\n1 of 3 sources resolved; nothing written.",
        ),
        (
            True,
            ("--update", "prover"),
            "Cannot lock: 1 source could not be resolved:\n"
            f"{PROVER_LINE}\n2 of 3 sources resolved; nothing written.",
        ),
    ],
    ids=["no-lock", "existing-lock-update-prover"],
)
def test_cli_lock_failure_exits_2_and_leaves_the_lockfile(
    image_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing: bool,
    update: tuple[str, ...],
    report: str,
) -> None:
    """A failed ``tundravm lock`` exits 2 with every failure and the hint on stderr, and
    writes nothing: no lockfile appears, and an existing one keeps its bytes."""
    target = image_file.parent / "build" / "tundravm.lock"
    if existing:
        write_lock(lock(load_recipe(image_file), resolver=_Upstream(broken=False)), target)
    before = target.read_bytes() if existing else None
    code, out = _cli(monkeypatch, _Upstream(), str(image_file), *update)
    assert (code, out) == (EXIT_ERROR, "")
    assert (target.read_bytes() if target.exists() else None) == before
    assert capsys.readouterr().err == f"error [E_LOCKFILE]: {report}\nfix: {HINT}\n"


def test_offline_lock_reports_every_unpinned_source_at_once(image_file: Path) -> None:
    recipe = load_recipe(image_file)
    full = lock(recipe, resolver=_Upstream(broken=False))
    partial = read_lock_without(full, "prover", "tool", tmp=image_file.parent)
    with pytest.raises(LockfileError) as caught:
        lock(recipe, previous=partial, offline=True)
    assert caught.value.args[0].splitlines() == [
        "Cannot lock offline: 2 sources need the network to resolve:",
        f"  prover: git {PROVER} @ feat/tdx: not pinned in the lockfile",
        f"  tool: http {TOOL}: not pinned in the lockfile",
        "1 of 3 sources resolved; nothing written.",
    ]
    assert sorted(caught.value.failures) == ["prover", "tool"]


def test_lock_check_names_unpinned_sources_without_the_network(
    image_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = load_recipe(image_file)
    target = image_file.parent / "build" / "tundravm.lock"
    full = lock(recipe, resolver=_Upstream(broken=False))
    write_lock(read_lock_without(full, "prover", tmp=image_file.parent), target)
    found = {d.subject: d.message for d in lock_status(recipe, read_lock(target))}
    assert found == {"sources.prover": "source prover is not pinned"}
    # conftest refuses network resolution; --check must not need it.
    out = io.StringIO()
    code = main(["lock", str(image_file), "--check"], stdout=out)
    assert (code, out.getvalue()) == (
        EXIT_FAILURE,
        "+ sources.prover: source prover is not pinned\n",
    )


def read_lock_without(full: Lock, *names: str, tmp: Path) -> Lock:
    """*full* re-read with the fetch entries of *names* dropped."""
    path = tmp / "partial.lock"
    write_lock(full, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetches"] = [f for f in data["fetches"] if f.get("name") not in names]
    path.write_text(json.dumps(data), encoding="utf-8")
    return read_lock(path)
