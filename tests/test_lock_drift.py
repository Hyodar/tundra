"""Section digests, lock drift reports, and `tundravm lock --check/--explain`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import (
    Backend,
    Diagnostic,
    File,
    Fragment,
    Package,
    Recipe,
    bake,
    lock,
    lock_status,
    lower,
    read_lock,
)
from tundravm.errors import LockfileError
from tundravm.lockfile import (
    LOCKFILE_VERSION,
    LockDrift,
    build_lockfile,
    compare_lock,
    describe_change,
    parse_lockfile,
    recipe_digest,
    section_digests,
    serialize_lockfile,
)

RECIPE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import File, Fragment, Package, Recipe

recipe = Recipe(
    "drift",
    Fragment(
        "drift",
        items=(Package("curl"), Package("jq"), File("/etc/motd", "hello\\n")),
    ),
)
backend = InProcessBackend()
"""

HTOP = 'Package("jq"), Package("htop")'


def _recipe(*extra: Package | File) -> Recipe:
    items = (Package("curl"), Package("jq"), File("/etc/motd", "hello\n"), *extra)
    return Recipe("drift", Fragment("drift", items=items))


def _payload(recipe: Recipe) -> dict[str, object]:
    return lower(recipe)._recipe_payload(profile_names=("default",))


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE, encoding="utf-8")
    return path


def _add_htop(recipe: Path) -> None:
    recipe.write_text(recipe.read_text().replace('Package("jq")', HTOP), encoding="utf-8")


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
        "variants.default.files",
        "variants.default.packages",
    ]
    assert digests == section_digests(json.loads(json.dumps(payload)))
    assert all(len(value) == 64 for value in digests.values())


def test_lockfile_records_section_digests_and_keeps_whole_digest() -> None:
    recipe = _recipe()
    locked = lock(recipe)
    payload = _payload(recipe)

    assert locked.lockfile.version == LOCKFILE_VERSION
    assert locked.recipe_digest == recipe_digest(payload)
    assert locked.lockfile.sections == section_digests(payload)
    assert dict(locked.sections) == section_digests(payload)
    assert {"base", "arch", "variants.default.packages", "variants.default.files"} <= set(
        locked.lockfile.sections
    )
    assert lock_status(recipe, locked) == ()
    assert compare_lock(locked.lockfile, payload).render() == "lock is up to date"


def test_drift_after_install_names_packages_with_detail() -> None:
    locked = lock(_recipe())
    recipe = _recipe(Package("htop"))

    assert lock_status(recipe, locked) == (
        Diagnostic(
            "lock-changed",
            "variants.default.packages changed since the lock: +htop",
            subject="variants.default.packages",
        ),
    )
    drift = compare_lock(locked.lockfile, _payload(recipe))
    assert drift.changed == ("variants.default.packages",)
    assert drift.render() == "~ variants.default.packages: +htop"


def test_drift_after_file_names_files_section_and_path() -> None:
    locked = lock(_recipe())
    recipe = _recipe(File("/etc/issue", "tundra\n"))

    changed = {d.subject: d.message for d in lock_status(recipe, locked)}

    assert "variants.default.files" in changed
    assert changed["variants.default.files"].endswith(": +/etc/issue")
    drift = compare_lock(locked.lockfile, _payload(recipe))
    assert "~ variants.default.files: +/etc/issue" in drift.render().splitlines()


def test_frozen_bake_error_carries_drift(tmp_path: Path) -> None:
    locked = lock(_recipe())

    with pytest.raises(LockfileError) as excinfo:
        bake(_recipe(Package("htop")), locked=locked, backend=Backend("inprocess"), out=tmp_path)

    error = excinfo.value
    assert "stale" in str(error)
    assert "~ variants.default.packages: +htop" in str(error)
    assert error.hint == "Run tundravm lock RECIPE to accept these changes, or revert them."
    assert error.context["changed"] == "variants.default.packages"
    assert error.context["lock"].endswith("tundravm.lock")


def test_lock_status_missing_lockfile_raises(tmp_path: Path) -> None:
    with pytest.raises(LockfileError) as excinfo:
        read_lock(tmp_path / "tundravm.lock")
    assert excinfo.value.hint is not None
    assert "lock" in excinfo.value.hint


def test_old_format_lockfile_still_freezes_and_reports_all_sections_added(
    tmp_path: Path,
) -> None:
    recipe = _recipe()
    payload = _payload(recipe)
    lock_path = tmp_path / "legacy.lock"
    legacy = {
        "version": 1,
        "recipe_digest": recipe_digest(payload),
        "recipe": payload,
        "dependencies": {"default": ["curl", "jq"]},
        "fetches": [],
    }
    lock_path.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    locked = read_lock(lock_path)
    assert locked.lockfile.sections == {}
    bake(recipe, locked=locked, backend=Backend("inprocess"), out=tmp_path / "out")

    found = lock_status(recipe, locked)
    assert found
    assert {d.code for d in found} == {"lock-added"}
    assert {d.subject for d in found} == set(section_digests(payload))
    drift = compare_lock(locked.lockfile, payload)
    assert drift.digest_matches
    assert drift.changed == ()
    assert all(line.startswith("+ ") for line in drift.render().splitlines())


def test_removed_profile_sections_and_scalar_detail() -> None:
    old = {"base": "debian/bookworm", "profiles": {"a": {"packages": []}, "b": {"packages": []}}}
    new = {"base": "ubuntu/noble", "profiles": {"a": {"packages": []}}}

    drift = compare_lock(build_lockfile(recipe=old), new)

    assert drift.changed == ("base",)
    assert drift.removed == ("variants.b.packages",)
    assert drift.render().splitlines() == [
        '~ base: "debian/bookworm" -> "ubuntu/noble"',
        "- variants.b.packages",
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
        changed=("variants.default.packages",), details={}, digest_matches=False
    )


def test_cli_lock_check_exit_codes(recipe: Path) -> None:
    lock_path = recipe.parent / "app.lock"
    code, out = run("lock", str(recipe), "--path", str(lock_path), "--check")
    assert code == EXIT_SDK_ERROR
    assert out == ""

    assert run("lock", str(recipe), "--path", str(lock_path))[0] == EXIT_OK
    code, out = run("lock", str(recipe), "--path", str(lock_path), "--check")
    assert (code, out) == (EXIT_OK, "lock is up to date\n")

    _add_htop(recipe)
    before = lock_path.read_text()
    code, out = run("lock", str(recipe), "--path", str(lock_path), "--check")
    assert code == EXIT_FAILURE
    assert out == "~ variants.default.packages: +htop\n"
    assert lock_path.read_text() == before


def test_cli_lock_explain_prints_drift_then_writes(recipe: Path) -> None:
    path = str(recipe.parent / "app.lock")
    code, out = run("lock", str(recipe), "--path", path, "--explain")
    assert code == EXIT_OK
    assert out.startswith("no lockfile at ")
    assert "locked " in out

    code, out = run("lock", str(recipe), "--path", path, "--explain")
    assert code == EXIT_OK
    assert out.splitlines()[0] == "lock is up to date"

    _add_htop(recipe)
    code, out = run("lock", str(recipe), "--path", path, "--explain", "--check")
    assert code == EXIT_FAILURE
    assert out == "~ variants.default.packages: +htop\n"

    code, out = run("lock", str(recipe), "--path", path, "--explain")
    assert code == EXIT_OK
    assert out.splitlines()[0] == "~ variants.default.packages: +htop"
    assert run("lock", str(recipe), "--path", path, "--check") == (
        EXIT_OK,
        "lock is up to date\n",
    )


def test_cli_bake_frozen_surfaces_drift(recipe: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lock_path = str(recipe.parent / "app.lock")
    run("lock", str(recipe), "--path", lock_path)
    _add_htop(recipe)

    code, _ = run("bake", str(recipe), "--lockfile", lock_path, "--out", str(recipe.parent / "out"))

    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_LOCKFILE]" in err
    assert "~ variants.default.packages: +htop" in err


# ── variant subsets and lockfile versions ───────────────────────────────


def _two_variants(payload: dict[str, object]) -> dict[str, object]:
    return {"base": "debian/bookworm", "profiles": payload}


def test_partial_comparison_skips_the_locks_other_variants() -> None:
    both = build_lockfile(recipe=_two_variants({"a": {"packages": []}, "b": {"packages": []}}))
    only_a = _two_variants({"a": {"packages": []}})

    assert compare_lock(both, only_a, partial=True).is_clean
    full = compare_lock(both, only_a)
    assert full.removed == ("variants.b.packages",) and not full.digest_matches

    changed = compare_lock(both, _two_variants({"a": {"packages": ["htop"]}}), partial=True)
    assert changed.changed == ("variants.a.packages",) and changed.removed == ()


def test_partial_comparison_checks_the_digest_for_the_locks_full_set() -> None:
    lock_a = build_lockfile(recipe=_two_variants({"a": {"packages": []}}))
    wider = _two_variants({"a": {"packages": []}, "b": {"packages": []}})

    assert compare_lock(lock_a, wider, partial=True).added == ("variants.b.packages",)
    forged = type(lock_a)(
        version=lock_a.version,
        recipe_digest="0" * 64,
        recipe=lock_a.recipe,
        dependencies=lock_a.dependencies,
        sections=lock_a.sections,
    )
    assert not compare_lock(forged, _two_variants({"a": {"packages": []}}), partial=True).is_clean


def test_version_2_lockfile_sections_load_as_variants() -> None:
    current = build_lockfile(recipe=_two_variants({"a": {"packages": ["curl"]}}))
    old = json.loads(serialize_lockfile(current))
    old["version"] = 2
    old["sections"] = {
        name.replace("variants.", "profiles.", 1): digest
        for name, digest in old["sections"].items()
    }
    assert "profiles.a.packages" in old["sections"]

    parsed = parse_lockfile(json.dumps(old))

    assert parsed == current
    assert parsed.version == LOCKFILE_VERSION == 3
    assert compare_lock(parsed, current.recipe).is_clean


def test_cli_lock_check_of_one_variant_against_a_full_lock(tmp_path: Path) -> None:
    path = tmp_path / "recipe.py"
    path.write_text(VARIANTS_RECIPE, encoding="utf-8")
    lock_path = tmp_path / "app.lock"
    assert run("lock", str(path), "--path", str(lock_path))[0] == EXIT_OK

    assert run("lock", str(path), "--path", str(lock_path), "--check", "--variant", "dev") == (
        EXIT_OK,
        "lock is up to date\n",
    )
    path.write_text(VARIANTS_RECIPE.replace('Package("vim")', 'Package("vim"), Package("gdb")'))
    assert run("lock", str(path), "--path", str(lock_path), "--check", "--variant", "default") == (
        EXIT_OK,
        "lock is up to date\n",
    )
    code, out = run("lock", str(path), "--path", str(lock_path), "--check", "--variant", "dev")
    assert (code, out) == (EXIT_FAILURE, "~ variants.dev.packages: +gdb\n")


VARIANTS_RECIPE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import Fragment, Package, Recipe, Variant

recipe = Recipe(
    "drift",
    Fragment("drift", items=(Package("curl"),)),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", add=Fragment("dev", items=(Package("vim"),))),
    ),
)
backend = InProcessBackend()
"""
