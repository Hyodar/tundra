"""Tests for the ``tundravm.testing`` helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.errors import ValidationError
from tundravm.modules import Module
from tundravm.testing import (
    CompiledTree,
    FakeModule,
    assert_clean,
    assert_diagnostic,
    assert_tree_matches,
    bake_in_process,
    compile_tree,
    recipe_file,
    run_cli,
)


def _recipe() -> Image:
    img = Image(reproducible=True)
    img.install("curl")
    img.file("/etc/motd", content="hello\n")
    img.user("app", system=True)
    img.service("app", command="/usr/bin/app", user="app")
    img.run("echo configured")
    img.add_init_script("echo booted", priority=50)
    with img.profile("azure"):
        img.install("jq")
    return img


# ── compile_tree / CompiledTree ──────────────────────────────────────


def test_compile_tree_reads_compiled_files(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(), path=tmp_path / "out")

    assert isinstance(tree, CompiledTree)
    assert tree.profiles == ("default",)
    assert os.fspath(tree) == str(tmp_path / "out")
    assert "curl" in tree.conf()
    assert tree.read("mkosi.extra/etc/motd") == "hello\n"
    assert tree.read("/mkosi.extra/etc/motd", profile="default") == "hello\n"
    assert tree.exists("mkosi.extra/etc/motd")
    assert not tree.exists("mkosi.extra/etc/nope")
    assert "User=app" in tree.unit("app")
    assert tree.unit("app.service") == tree.unit("app")
    assert "echo configured" in tree.script("postinst")
    assert "echo booted" in tree.runtime_init()
    files = tree.files()
    assert files == sorted(files)
    assert {"mkosi.conf", "mkosi.extra/etc/motd"} <= set(files)


def test_compile_tree_defaults_to_a_fresh_temp_dir() -> None:
    img = _recipe()
    first, second = compile_tree(img), compile_tree(img)
    assert first.root != second.root
    assert first.conf() == second.conf()


def test_compile_tree_selects_profiles(tmp_path: Path) -> None:
    img = _recipe()
    tree = compile_tree(img, profiles=["default", "azure"], path=tmp_path / "all")
    assert tree.profiles == ("default", "azure")
    assert tree.profile("azure") == tmp_path / "all" / "azure"
    assert "jq" in tree.conf(profile="azure")

    only = compile_tree(img, profiles="azure", path=tmp_path / "azure-only")
    assert only.profiles == ("azure",)
    assert only.default_profile == "azure"
    assert "jq" in only.conf()


def test_compile_tree_rejects_unknown_and_uncompiled_profiles(tmp_path: Path) -> None:
    img = _recipe()
    with pytest.raises(ValueError, match="unknown profile"):
        compile_tree(img, profiles=["gpc"], path=tmp_path / "x")
    tree = compile_tree(img, path=tmp_path / "y")
    with pytest.raises(KeyError, match="was not compiled"):
        tree.profile("azure")


def test_compile_tree_missing_unit_or_script_lists_what_exists(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(), path=tmp_path / "out")
    with pytest.raises(FileNotFoundError, match="app.service"):
        tree.unit("missing")
    with pytest.raises(FileNotFoundError, match="postinst"):
        tree.script("build")


def test_compile_tree_leaves_compile_cache_alone(tmp_path: Path) -> None:
    img = _recipe()
    compile_tree(img, path=tmp_path / "out")
    assert img._last_compile_path is None
    assert img._last_compile_digest is None


# ── assert_clean / assert_diagnostic ─────────────────────────────────


def test_assert_clean_passes_warnings_unless_strict() -> None:
    img = _recipe()
    diagnostics = assert_clean(img)
    assert [d.code for d in diagnostics] == ["backend-missing"]
    with pytest.raises(AssertionError, match="warning backend-missing"):
        assert_clean(img, strict=True)
    assert_clean(img, strict=True, allow=("backend-missing",))


def test_assert_clean_fails_on_errors_with_rendered_report() -> None:
    img = Image(reproducible=True)
    img.service("app", command="/usr/bin/app", user="ghost")
    with pytest.raises(AssertionError) as excinfo:
        assert_clean(img)
    message = str(excinfo.value)
    assert "error service-user-missing [default] app" in message
    assert "hint:" in message
    assert_clean(img, allow=["service-user-missing"])


def test_assert_clean_checks_selected_profiles() -> None:
    img = Image(reproducible=True)
    with img.profile("azure"):
        img.service("app", command="/usr/bin/app", user="ghost")
    assert_clean(img)
    with pytest.raises(AssertionError, match=r"\[azure\]"):
        assert_clean(img, profiles="azure")


def test_assert_diagnostic_returns_the_match() -> None:
    img = Image(reproducible=True)
    img.service("app", command="/usr/bin/app", user="ghost")
    found = assert_diagnostic(img, "service-user-missing", level="error", subject="app")
    assert found.profile == "default"
    assert "ghost" in found.message


def test_assert_diagnostic_filters_by_profile() -> None:
    img = Image(reproducible=True)
    with img.profile("azure"):
        img.service("app", command="/usr/bin/app", user="ghost")
    found = assert_diagnostic(img, "service-user-missing", profile="azure")
    assert found.profile == "azure"


def test_assert_diagnostic_failure_lists_every_finding() -> None:
    img = _recipe()
    with pytest.raises(AssertionError) as excinfo:
        assert_diagnostic(img, "service-user-missing", subject="app")
    message = str(excinfo.value)
    assert "code='service-user-missing', subject='app'" in message
    assert "warning backend-missing [default]" in message
    with pytest.raises(AssertionError, match="level='error'"):
        assert_diagnostic(img, "backend-missing", level="error")


# ── assert_tree_matches ──────────────────────────────────────────────


def test_golden_round_trip(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, update=True)
    assert (golden / "default" / "mkosi.conf").is_file()
    assert assert_tree_matches(_recipe(), golden).is_clean

    changed = _recipe()
    changed.install("htop")
    changed.file("/etc/new", content="new\n")
    with pytest.raises(AssertionError) as excinfo:
        assert_tree_matches(changed, golden)
    message = str(excinfo.value)
    assert "M  default/mkosi.conf" in message
    assert "A  default/mkosi.extra/etc/new" in message
    assert "+htop" in message or "htop" in message
    assert "TUNDRAVM_UPDATE_GOLDEN=1" in message


def test_golden_update_from_env_rewrites_and_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, update=True)
    stale = golden / "default" / "mkosi.extra" / "etc" / "stale"
    stale.write_text("old\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="D  default/mkosi.extra/etc/stale"):
        assert_tree_matches(_recipe(), golden)

    monkeypatch.setenv("TUNDRAVM_UPDATE_GOLDEN", "1")
    diff = assert_tree_matches(_recipe(), golden)
    assert not diff.is_clean
    assert not stale.exists()
    monkeypatch.delenv("TUNDRAVM_UPDATE_GOLDEN")
    assert_tree_matches(_recipe(), golden)
    assert_tree_matches(_recipe(), golden, update=False)


def test_golden_ignores_and_keeps_profiles_not_compiled(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, profiles=["default", "azure"], update=True)
    azure_conf = (golden / "azure" / "mkosi.conf").read_text(encoding="utf-8")

    changed = _recipe()
    changed.install("htop")
    assert_tree_matches(changed, golden, update=True)
    assert (golden / "azure" / "mkosi.conf").read_text(encoding="utf-8") == azure_conf
    assert_tree_matches(changed, golden)
    with pytest.raises(AssertionError, match="azure/mkosi.conf"):
        assert_tree_matches(changed, golden, profiles=["azure"])


def test_golden_truncates_long_diffs(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(Image(reproducible=True), golden, update=True)
    big = Image(reproducible=True)
    big.file("/etc/big", content="".join(f"line {n}\n" for n in range(500)))
    with pytest.raises(AssertionError, match=r"more diff lines"):
        assert_tree_matches(big, golden)


# ── bake_in_process ──────────────────────────────────────────────────


def test_bake_in_process_restores_backend(tmp_path: Path) -> None:
    img = _recipe()
    result = bake_in_process(img, build_dir=tmp_path / "build")
    assert img.backend is None
    artifact = result.profiles["default"].artifacts["qemu"].path
    assert artifact.is_file()
    assert artifact.is_relative_to(tmp_path / "build")


def test_bake_in_process_defaults_to_temp_dir_and_profiles() -> None:
    img = _recipe()
    result = bake_in_process(img, profiles=["azure"])
    assert set(result.profiles) == {"azure"}


# ── FakeModule ───────────────────────────────────────────────────────


def test_fake_module_declares_packages_and_files() -> None:
    img = Image(reproducible=True)
    fake = FakeModule("x", packages=("pkg",), files={"/etc/x": "content"})
    img.apply(fake)

    state = img.state.profiles["default"]
    assert "pkg" in state.packages
    assert [(f.path, f.content) for f in state.files] == [("/etc/x", "content")]
    assert fake.applied_to == ["default"]
    assert fake.name == "x"
    assert isinstance(fake, FakeModule) and isinstance(fake, Module)
    assert img.applied_modules() == (fake,)


def test_fake_module_records_every_profile() -> None:
    img = Image(reproducible=True)
    fake = FakeModule()
    with img.profiles("default", "azure"):
        img.apply(fake)
    assert sorted(fake.applied_to) == ["azure", "default"]


def test_fake_module_requires_ordering() -> None:
    a = FakeModule("a")
    b = FakeModule("b", requires=(a,))
    assert type(a) is not type(b)
    with pytest.raises(ValidationError, match=r"FakeModule\[b\] requires FakeModule\[a\]"):
        Image(reproducible=True).apply(b)
    img = Image(reproducible=True).apply(a, b)
    assert [m.name for m in img.applied_modules()] == ["a", "b"]


def test_fake_module_requires_a_real_module_class() -> None:
    class Base(Module):
        pass

    with pytest.raises(ValidationError):
        Image(reproducible=True).apply(FakeModule("dep", requires=(Base,)))
    Image(reproducible=True).apply(Base(), FakeModule("dep", requires=(Base,)))


def test_fake_module_init_scripts_compose_by_priority(tmp_path: Path) -> None:
    img = Image(reproducible=True)
    img.apply(
        FakeModule("late", init_script="echo late", init_priority=40),
        FakeModule("early", init_script="echo early", init_priority=10),
        FakeModule("silent", init_priority=None, init_script="echo never"),
    )
    init = compile_tree(img, path=tmp_path / "out").runtime_init()
    assert init.index("echo early") < init.index("echo late")
    assert "echo never" not in init


def test_fake_module_collision_is_diagnosed() -> None:
    img = Image(reproducible=True)
    img.apply(FakeModule("a", init_script="echo a"), FakeModule("b", init_script="echo b"))
    assert_diagnostic(img, "init-priority-collision", subject="priority 50")


# ── recipe_file / run_cli ────────────────────────────────────────────


def test_recipe_file_and_run_cli(tmp_path: Path) -> None:
    path = recipe_file(
        tmp_path,
        """
        from tundravm import Image

        img = Image()
        img.install("curl")
        """,
    )
    assert path == tmp_path / "recipe.py"
    assert path.read_text(encoding="utf-8").startswith("from tundravm import Image\n")

    code, out, err = run_cli("check", path)
    assert code == 0
    assert "backend-missing" in out
    assert err == ""

    code, out, _ = run_cli("compile", path, "--out", tmp_path / "tree")
    assert code == 0, out
    assert (tmp_path / "tree" / "default" / "mkosi.conf").is_file()


def test_run_cli_captures_usage_errors_and_sdk_errors(tmp_path: Path) -> None:
    code, out, err = run_cli("no-such-command")
    assert code == 2
    assert "usage:" in err
    assert out == ""

    broken = recipe_file(tmp_path, "img = 42\n", name="sub/broken.py")
    code, _, err = run_cli("check", broken)
    assert code != 0
    assert "error [" in err
