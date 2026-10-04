"""Section digests, lock drift reports, and `tundravm lock --check/--explain`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends import InProcessBackend
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.errors import LockfileError
from tundravm.lockfile import (
    LOCKFILE_VERSION,
    LockDrift,
    build_lockfile,
    compare_lock,
    describe_change,
    read_lockfile,
    recipe_digest,
    section_digests,
)

RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl", "jq")
img.file("/etc/motd", content="hello\\n")
"""


@pytest.fixture
def image(tmp_path: Path, inprocess_backend: InProcessBackend) -> Image:
    img = Image(build_dir=tmp_path / "build", backend=inprocess_backend)
    img.install("curl", "jq")
    img.file("/etc/motd", content="hello\n")
    return img


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    path = tmp_path / "recipe.py"
    build_dir = tmp_path / "build"
    path.write_text(f"BUILD_DIR = {str(build_dir)!r}\n" + RECIPE, encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def test_section_digests_cover_top_level_and_profile_sections() -> None:
    payload: dict[str, object] = {
        "base": "debian/bookworm",
        "arch": "x86_64",
        "profiles": {"default": {"packages": ["curl"], "files": []}},
    }
    digests = section_digests(payload)

    assert sorted(digests) == [
        "arch",
        "base",
        "profiles.default.files",
        "profiles.default.packages",
    ]
    assert digests == section_digests(json.loads(json.dumps(payload)))
    assert all(len(value) == 64 for value in digests.values())


def test_lockfile_records_section_digests_and_keeps_whole_digest(image: Image) -> None:
    lock = read_lockfile(image.lock())
    payload = image._recipe_payload(profile_names=image._active_profiles)

    assert lock.version == LOCKFILE_VERSION
    assert lock.recipe_digest == recipe_digest(payload)
    assert lock.sections == section_digests(payload)
    assert {"base", "arch", "profiles.default.packages", "profiles.default.files"} <= set(
        lock.sections
    )
    assert image.lock_status().is_clean
    assert image.lock_status().render() == "lock is up to date"


def test_drift_after_install_names_packages_with_detail(image: Image) -> None:
    image.lock()
    image.install("htop")

    drift = image.lock_status()

    assert drift.changed == ("profiles.default.packages",)
    assert drift.render() == "~ profiles.default.packages: +htop"


def test_drift_after_file_names_files_section_and_path(image: Image) -> None:
    image.lock()
    image.file("/etc/issue", content="tundra\n")

    drift = image.lock_status()

    assert "profiles.default.files" in drift.changed
    assert "~ profiles.default.files: +/etc/issue" in drift.render().splitlines()


def test_frozen_bake_error_carries_drift(image: Image) -> None:
    image.lock()
    image.install("htop")

    with pytest.raises(LockfileError) as excinfo:
        image.bake(frozen=True)

    error = excinfo.value
    assert "stale" in str(error)
    assert "~ profiles.default.packages: +htop" in str(error)
    assert error.hint == "Run tundravm lock RECIPE to accept these changes, or revert them."
    assert error.context["changed"] == "profiles.default.packages"
    assert error.context["lock"].endswith("tundravm.lock")


def test_lock_status_missing_lockfile_raises(image: Image) -> None:
    with pytest.raises(LockfileError) as excinfo:
        image.lock_status()
    assert excinfo.value.hint is not None
    assert "lock" in excinfo.value.hint


def test_old_format_lockfile_still_freezes_and_reports_all_sections_added(
    image: Image,
) -> None:
    payload = image._recipe_payload(profile_names=image._active_profiles)
    lock_path = image.build_dir / "tundravm.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 1,
        "recipe_digest": recipe_digest(payload),
        "recipe": payload,
        "dependencies": {"default": ["curl", "jq"]},
        "fetches": [],
    }
    lock_path.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert read_lockfile(lock_path).sections == {}
    image.bake(frozen=True)

    drift = image.lock_status()
    assert not drift.is_clean
    assert drift.digest_matches
    assert drift.changed == ()
    assert set(drift.added) == set(section_digests(payload))
    assert all(line.startswith("+ ") for line in drift.render().splitlines())


def test_removed_profile_sections_and_scalar_detail() -> None:
    old = {"base": "debian/bookworm", "profiles": {"a": {"packages": []}, "b": {"packages": []}}}
    new = {"base": "ubuntu/noble", "profiles": {"a": {"packages": []}}}

    drift = compare_lock(build_lockfile(recipe=old), new)

    assert drift.changed == ("base",)
    assert drift.removed == ("profiles.b.packages",)
    assert drift.render().splitlines() == [
        '~ base: "debian/bookworm" -> "ubuntu/noble"',
        "- profiles.b.packages",
    ]


def test_describe_change_marks_modified_items_and_elides() -> None:
    old = [{"path": "/etc/motd", "sha256": "a"}, {"path": "/etc/gone", "sha256": "b"}]
    new = [{"path": "/etc/motd", "sha256": "c"}]
    assert describe_change(old, new) == "-/etc/gone ~/etc/motd"
    assert describe_change(["jq"], ["htop"]) == "+htop -jq"
    many = describe_change([], [f"p{index}" for index in range(8)])
    assert many == "+p0 +p1 +p2 +p3 +p4 … +3 more"
    assert describe_change({"x": 1}, {"x": 1}) is None


def test_drift_ignores_embedded_recipe_that_does_not_match_section_digest() -> None:
    lock = build_lockfile(recipe={"profiles": {"default": {"packages": ["jq"]}}})
    tampered = lock.recipe | {"profiles": {"default": {"packages": ["other"]}}}
    forged = type(lock)(
        version=lock.version,
        recipe_digest=lock.recipe_digest,
        recipe=tampered,
        dependencies=lock.dependencies,
        sections=lock.sections,
    )

    drift = compare_lock(forged, {"profiles": {"default": {"packages": ["htop"]}}})

    assert drift == LockDrift(
        changed=("profiles.default.packages",), details={}, digest_matches=False
    )


def test_cli_lock_check_exit_codes(recipe: Path) -> None:
    code, out = run("lock", str(recipe), "--check")
    assert code == EXIT_SDK_ERROR
    assert out == ""

    assert run("lock", str(recipe))[0] == EXIT_OK
    code, out = run("lock", str(recipe), "--check")
    assert (code, out) == (EXIT_OK, "lock is up to date\n")

    recipe.write_text(recipe.read_text() + 'img.install("htop")\n', encoding="utf-8")
    lock_path = recipe.parent / "build" / "tundravm.lock"
    before = lock_path.read_text()
    code, out = run("lock", str(recipe), "--check")
    assert code == EXIT_FAILURE
    assert out == "~ profiles.default.packages: +htop\n"
    assert lock_path.read_text() == before


def test_cli_lock_explain_prints_drift_then_writes(recipe: Path) -> None:
    code, out = run("lock", str(recipe), "--explain")
    assert code == EXIT_OK
    assert out.startswith("no lockfile at ")
    assert "locked " in out

    code, out = run("lock", str(recipe), "--explain")
    assert code == EXIT_OK
    assert out.splitlines()[0] == "lock is up to date"

    recipe.write_text(recipe.read_text() + 'img.install("htop")\n', encoding="utf-8")
    code, out = run("lock", str(recipe), "--explain", "--check")
    assert code == EXIT_FAILURE
    assert out == "~ profiles.default.packages: +htop\n"

    code, out = run("lock", str(recipe), "--explain")
    assert code == EXIT_OK
    assert out.splitlines()[0] == "~ profiles.default.packages: +htop"
    assert run("lock", str(recipe), "--check") == (EXIT_OK, "lock is up to date\n")


def test_cli_bake_frozen_surfaces_drift(recipe: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run("lock", str(recipe))
    recipe.write_text(recipe.read_text() + 'img.install("htop")\n', encoding="utf-8")

    code, _ = run("bake", str(recipe), "--frozen")

    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_LOCKFILE]" in err
    assert "~ profiles.default.packages: +htop" in err
