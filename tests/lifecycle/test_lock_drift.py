"""Section digests, lock drift reports, and `tundravm lock --check/--explain`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import write_recipe_file
from tundravm.declarative import (
    Backend,
    Diagnostic,
    File,
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
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import File, Fragment, Package, Recipe

recipe = Recipe(
    "drift",
    items=(
        Package("curl"),
        Package("linux-image-amd64"),
        Package("jq"),
        File("/etc/motd", "hello\\n"),
    ),
)
backend = LocalLinuxBackend()
"""


def _recipe(*extra: Package | File) -> Recipe:
    items = (
        Package("curl"),
        Package("linux-image-amd64"),
        Package("jq"),
        File("/etc/motd", "hello\n"),
        *extra,
    )
    return Recipe("drift", items=items)


def _payload(recipe: Recipe) -> dict[str, object]:
    return lower(recipe).payload()


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    return write_recipe_file(tmp_path, RECIPE)


def test_lockfile_records_section_digests_and_keeps_whole_digest() -> None:
    recipe = _recipe()
    locked = lock(recipe)
    payload = _payload(recipe)

    assert locked.lockfile.version == LOCKFILE_VERSION
    assert locked.recipe_digest == recipe_digest(payload)
    assert locked.lockfile.sections == section_digests(payload)
    assert dict(locked.sections) == section_digests(payload)
    assert {
        "distribution",
        "compiler",
        "profiles.default.packages",
        "profiles.default.files",
        "profiles.default.kernel",
        "profiles.default.debloat",
    } <= set(locked.lockfile.sections)
    assert lock_status(recipe, locked) == ()
    assert compare_lock(locked.lockfile, payload).render() == "lock is up to date"


@pytest.mark.parametrize(
    ("extra", "key", "detail"),
    [
        (Package("htop"), "packages", "+htop"),
        (File("/etc/issue", "tundra\n"), "files", "+/etc/issue"),
    ],
    ids=["package", "file"],
)
def test_drift_names_the_profile_the_section_and_the_change(
    extra: Package | File, key: str, detail: str
) -> None:
    """One more package or file changes exactly its profile's section: the finding is the
    profile's, names the key and what changed, and the drift report names the section."""
    locked = lock(_recipe())
    recipe = _recipe(extra)

    (found,) = lock_status(recipe, locked)
    assert found == Diagnostic(
        "lock-changed",
        "error",
        f"{key} changed since the lock: {detail}",
        subject=key,
        variant="default",
        hint=found.hint,
    )
    drift = compare_lock(locked.lockfile, _payload(recipe))
    assert drift.changed == (f"profiles.default.{key}",)
    assert drift.render() == f"~ profiles.default.{key}: {detail}"


def test_frozen_bake_error_carries_drift(tmp_path: Path) -> None:
    locked = lock(_recipe())

    with pytest.raises(LockfileError) as excinfo:
        bake(_recipe(Package("htop")), lock=locked, backend=Backend("local"), build=tmp_path)

    error = excinfo.value
    assert "stale" in str(error)
    assert "~ profiles.default.packages: +htop" in str(error)
    assert error.hint == "Run `tundravm lock IMAGE` to accept these changes, or revert them."
    assert error.context["changed"] == "profiles.default.packages"
    assert error.context["lock"].endswith("tundravm.lock")


def test_lock_status_missing_lockfile_raises(tmp_path: Path) -> None:
    with pytest.raises(LockfileError) as excinfo:
        read_lock(tmp_path / "tundravm.lock")
    assert excinfo.value.hint is not None
    assert "lock" in excinfo.value.hint


@pytest.mark.parametrize("version", [1, 2, 5])
def test_older_lockfile_versions_ask_to_lock_again(tmp_path: Path, version: int) -> None:
    recipe = _recipe()
    payload = _payload(recipe)
    lock_path = tmp_path / "legacy.lock"
    legacy = {
        "version": version,
        "recipe_digest": recipe_digest(payload),
        "recipe": payload,
        "dependencies": {"default": ["curl", "jq"]},
        "fetches": [],
    }
    lock_path.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(LockfileError, match=f"version {version} is not supported") as raised:
        read_lock(lock_path)

    assert raised.value.code == "E_LOCKFILE"
    assert raised.value.hint == "Run `tundravm lock IMAGE` to lock again, and commit the lockfile."


def test_lockfile_without_toolchain_section_is_invalid() -> None:
    raw = json.loads(serialize_lockfile(build_lockfile(recipe={"base": "debian/trixie"})))
    del raw["toolchain"]

    with pytest.raises(LockfileError, match="no `toolchain` section"):
        parse_lockfile(json.dumps(raw))


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


# ── variant subsets and lockfile versions ───────────────────────────────


def _two_variants(payload: dict[str, object]) -> dict[str, object]:
    return {"base": "debian/bookworm", "profiles": payload}


def test_partial_comparison_skips_the_locks_other_variants() -> None:
    both = build_lockfile(recipe=_two_variants({"a": {"packages": []}, "b": {"packages": []}}))
    only_a = _two_variants({"a": {"packages": []}})

    assert compare_lock(both, only_a, partial=True).is_clean
    full = compare_lock(both, only_a)
    assert full.removed == ("profiles.b.packages",) and not full.digest_matches

    changed = compare_lock(both, _two_variants({"a": {"packages": ["htop"]}}), partial=True)
    assert changed.changed == ("profiles.a.packages",) and changed.removed == ()


def test_partial_comparison_checks_the_digest_for_the_locks_full_set() -> None:
    lock_a = build_lockfile(recipe=_two_variants({"a": {"packages": []}}))
    wider = _two_variants({"a": {"packages": []}, "b": {"packages": []}})

    assert compare_lock(lock_a, wider, partial=True).added == ("profiles.b.packages",)
    forged = type(lock_a)(
        version=lock_a.version,
        recipe_digest="0" * 64,
        recipe=lock_a.recipe,
        dependencies=lock_a.dependencies,
        sections=lock_a.sections,
    )
    assert not compare_lock(forged, _two_variants({"a": {"packages": []}}), partial=True).is_clean


def test_lockfile_round_trips_its_toolchain() -> None:
    current = build_lockfile(recipe=_two_variants({"a": {"packages": ["curl"]}}))

    parsed = parse_lockfile(serialize_lockfile(current))

    assert parsed == current
    assert LOCKFILE_VERSION == 6
    assert parsed.toolchain is not None and parsed.toolchain.tundravm
