"""Tests for compiled-tree diffing (``Image.diff``, ``tundravm diff``, ``compile --check``)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tundravm import FileChange, Image, TreeDiff
from tundravm.cli import EXIT_FAILURE, EXIT_OK, main
from tundravm.diff import diff_against, diff_trees

RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.platforms import AzurePlatform

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl", "jq")
img.file("/etc/motd", content="hello\\n")
img.user("app", system=True)
img.service("app", command="/usr/bin/app")
img.targets("qemu")
with img.profile("azure"):
    AzurePlatform().apply(img)
"""


def write(root: Path, rel: str, data: str | bytes, mode: int = 0o644) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    old, new = tmp_path / "old", tmp_path / "new"
    for root in (old, new):
        write(root, "same.txt", "unchanged\n")
    write(old, "gone.txt", "bye\n")
    write(new, "fresh.txt", "hi\n")
    write(old, "conf/edit.conf", "a\nb\nc\n")
    write(new, "conf/edit.conf", "a\nB\nc\n")
    write(old, "run.sh", "#!/bin/sh\n", 0o644)
    write(new, "run.sh", "#!/bin/sh\n", 0o755)
    write(old, "blob.bin", b"\x00\x01old")
    write(new, "blob.bin", b"\x00\x01new")
    return old, new


def by_path(diff: TreeDiff) -> dict[str, FileChange]:
    return {change.path: change for change in diff.changes}


def test_diff_trees_classifies_changes(trees: tuple[Path, Path]) -> None:
    diff = diff_trees(*trees)
    changes = by_path(diff)
    assert list(changes) == sorted(changes)
    assert "same.txt" not in changes
    assert {path: change.status for path, change in changes.items()} == {
        "blob.bin": "modified",
        "conf/edit.conf": "modified",
        "fresh.txt": "added",
        "gone.txt": "removed",
        "run.sh": "mode",
    }
    assert changes["fresh.txt"].old_text is None
    assert changes["fresh.txt"].new_text == "hi\n"
    assert changes["gone.txt"].new_mode is None
    assert (changes["run.sh"].old_mode, changes["run.sh"].new_mode) == (0o644, 0o755)
    blob = changes["blob.bin"]
    assert blob.is_binary and blob.old_text is None and blob.new_text is None
    assert not diff.is_clean


def test_diff_trees_identical_and_ignore(trees: tuple[Path, Path]) -> None:
    old, _ = trees
    assert diff_trees(old, old).is_clean
    assert diff_trees(old, old).stat() == "0 files changed\n"
    ignored = diff_trees(*trees, ignore=["conf/*", "*.bin"])
    assert {c.path for c in ignored.changes} == {"fresh.txt", "gone.txt", "run.sh"}


def test_stat_lists_codes_and_summary(trees: tuple[Path, Path]) -> None:
    assert diff_trees(*trees).stat() == (
        "M  blob.bin\nM  conf/edit.conf\nA  fresh.txt\nD  gone.txt\nT  run.sh\n5 files changed\n"
    )


def test_unified_headers_bodies_and_notes(trees: tuple[Path, Path]) -> None:
    text = diff_trees(*trees).unified()
    assert "\x1b[" not in text
    assert "--- a/conf/edit.conf\n+++ b/conf/edit.conf\n" in text
    assert "-b\n+B\n" in text
    assert "--- /dev/null\n+++ b/fresh.txt\n@@ -0,0 +1 @@\n+hi\n" in text
    assert "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n" in text
    assert "Binary files a/blob.bin and b/blob.bin differ\n" in text
    assert "old mode 100644\nnew mode 100755\n" in text


def test_unified_marks_missing_trailing_newline(tmp_path: Path) -> None:
    write(tmp_path / "a", "f", "x\ny")
    write(tmp_path / "b", "f", "x\nz")
    text = diff_trees(tmp_path / "a", tmp_path / "b").unified()
    assert "-y\n\\ No newline at end of file\n+z\n\\ No newline at end of file\n" in text


def test_unified_color_only_when_requested(trees: tuple[Path, Path]) -> None:
    text = diff_trees(*trees).unified(color=True)
    assert "\x1b[31m-b\x1b[0m\n" in text
    assert "\x1b[32m+B\x1b[0m\n" in text
    assert "\x1b[36m@@" in text
    assert "\x1b[1m--- a/conf/edit.conf\x1b[0m\n" in text


def test_to_dict_is_serializable(trees: tuple[Path, Path]) -> None:
    payload = diff_trees(*trees).to_dict()
    assert payload["clean"] is False
    changes = payload["changes"]
    assert isinstance(changes, list)
    assert {"path": "run.sh", "status": "mode", "old_mode": "0644", "new_mode": "0755"}.items() <= (
        next(c for c in changes if c["path"] == "run.sh").items()
    )


def make_image(tmp_path: Path) -> Image:
    img = Image(build_dir=tmp_path / "build")
    img.install("curl")
    img.file("/etc/motd", content="hello\n")
    return img


def test_image_diff_reports_new_package(tmp_path: Path) -> None:
    img = make_image(tmp_path)
    tree = tmp_path / "tree"
    img.compile(tree)
    assert img.diff(tree).is_clean

    img.install("extra")
    diff = img.diff(tree)
    assert [(c.path, c.status) for c in diff.changes] == [("default/mkosi.conf", "modified")]
    assert "+    extra\n" in diff.unified()
    assert "    extra\n" not in (tree / "default" / "mkosi.conf").read_text(encoding="utf-8")


def test_diff_against_missing_tree_is_all_added(tmp_path: Path) -> None:
    diff = diff_against(make_image(tmp_path), tmp_path / "missing")
    assert diff.changes
    assert {c.status for c in diff.changes} == {"added"}
    assert "default/mkosi.conf" in {c.path for c in diff.changes}


def test_diff_ignores_profiles_that_were_not_compiled(tmp_path: Path) -> None:
    img = make_image(tmp_path)
    with img.profile("other"):
        img.install("htop")
    tree = tmp_path / "tree"
    with img.all_profiles():
        img.compile(tree)
    assert (tree / "other" / "mkosi.conf").is_file()
    assert img.diff(tree).is_clean


def test_diff_preserves_compile_skip_cache(tmp_path: Path) -> None:
    img = make_image(tmp_path)
    tree = tmp_path / "tree"
    first = img.compile(tree)
    conf = tree / "default" / "mkosi.conf"
    conf.write_text("hand edit\n", encoding="utf-8")

    assert [c.path for c in img.diff(tree).changes] == ["default/mkosi.conf"]
    assert img._last_compile_path == tree
    again = img.compile(tree)
    assert again.digest == first.digest and again.path == tree
    assert conf.read_text(encoding="utf-8") == "hand edit\n"  # unchanged recipe: compile skipped

    img.install("extra")
    img.diff(tree)
    changed = img.compile(tree)
    assert changed.path == tree and changed.digest != first.digest
    assert "    extra\n" in conf.read_text(encoding="utf-8")
    assert img._last_compile_emission is not None
    assert img.diff(tree).is_clean


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    path = tmp_path / "recipe.py"
    path.write_text(f"BUILD_DIR = {str(tmp_path / 'build')!r}\n" + RECIPE, encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def test_compile_check_detects_stale_tree(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    code, out = run("compile", str(recipe), "--check", "--out", str(tree))
    assert code == EXIT_FAILURE and "A  default/mkosi.conf" in out
    assert not tree.exists()

    assert run("compile", str(recipe), "--out", str(tree))[0] == EXIT_OK
    code, out = run("compile", str(recipe), "--out", str(tree), "--check")
    assert (code, out) == (EXIT_OK, "tree is up to date with the recipe\n")

    recipe.write_text(recipe.read_text().replace('"curl", "jq"', '"curl", "jq", "htop"'))
    code, out = run("compile", str(recipe), "--out", str(tree), "--check")
    assert code == EXIT_FAILURE
    assert out == "M  default/mkosi.conf\n1 file changed\n"


def test_diff_command(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    run("compile", str(recipe), "--out", str(tree), "--all-profiles")
    code, out = run("diff", str(recipe), "--against", str(tree), "--stat")
    assert (code, out) == (EXIT_OK, "tree is up to date with the recipe\n")
    code, out = run("diff", str(recipe), "--against", str(tree), "--all-profiles")
    assert code == EXIT_OK

    recipe.write_text(recipe.read_text().replace("hello", "goodbye"))
    code, out = run("diff", str(recipe), "--against", str(tree), "--stat", "--all-profiles")
    assert (tree / "azure" / "mkosi.conf").is_file()
    assert (code, out) == (
        EXIT_FAILURE,
        "M  azure/mkosi.extra/etc/motd\nM  default/mkosi.extra/etc/motd\n2 files changed\n",
    )

    code, out = run("diff", str(recipe), "--against", str(tree), "--color", "never")
    assert code == EXIT_FAILURE
    assert "-hello\n+goodbye\n" in out and "\x1b[" not in out
    code, out = run("diff", str(recipe), "--against", str(tree), "--color", "always")
    assert "\x1b[31m-hello\x1b[0m" in out


def test_diff_command_defaults_to_build_dir(recipe: Path) -> None:
    code, out = run("diff", str(recipe), "--stat")
    assert code == EXIT_FAILURE and "A  default/mkosi.conf" in out
    run("compile", str(recipe))
    assert run("diff", str(recipe))[0] == EXIT_OK
