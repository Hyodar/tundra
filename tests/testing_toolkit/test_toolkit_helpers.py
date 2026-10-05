"""Tests for the ``tundravm.testing`` helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tundravm import (
    Diagnostic,
    File,
    Fragment,
    Hook,
    Init,
    Package,
    Recipe,
    Resolved,
    Service,
    Setting,
    User,
    Variant,
    compile,
    lint,
    lock,
    read_artifacts,
)
from tundravm.declarative.lifecycle import read_tree
from tundravm.errors import ValidationError
from tundravm.testing import (
    CompiledTree,
    assert_clean,
    assert_diagnostic,
    assert_tree,
    assert_tree_matches,
    bake_in_process,
    compile_tree,
    fake_bake,
    fake_fragment,
    recipe_file,
    run_cli,
)

AZURE = Variant("azure", target="azure", add=Fragment("azure", (Package("jq"),)))
KERNEL = Package("linux-image-amd64")
NOT_BOOTABLE = Setting("Content", "Bootable", ("no",))
"""Keeps a package-free recipe free of ``kernel-missing``, so its own findings stand alone."""


def _recipe(*extra: Package | File) -> Recipe:
    common = Fragment(
        "app",
        (
            Package("curl"),
            KERNEL,
            File("/etc/motd", "hello\n"),
            User("app", system=True),
            Service("app", "/usr/bin/app", user="app"),
            Hook("configured", "postinst", "echo configured"),
            Init("boot", "echo booted", priority=50),
            *extra,
        ),
    )
    return Recipe(name="demo", common=common, variants=(Variant("default", target="qemu"), AZURE))


def _single(*items: Package | File | User | Service) -> Recipe:
    return Recipe(name="demo", common=Fragment("app", (NOT_BOOTABLE, *items)))


def _ghost(*, where: str = "common") -> Recipe:
    """A recipe whose ``app`` service runs as an undeclared user, in common or in ``azure``."""
    service = Service("app", "/usr/bin/app", user="ghost")
    if where == "common":
        return _single(service)
    variant = Variant("azure", target="azure", add=Fragment("azure", (service,)))
    return Recipe(
        name="demo",
        common=Fragment("app", (KERNEL,)),
        variants=(Variant("default", target="qemu"), variant),
    )


def _warning_only() -> Recipe:
    """One ``service-command-not-shipped`` warning: nothing ships ``/usr/bin/app``."""
    return _single(User("app"), Service("app", "/usr/bin/app", user="app"))


# ── compile_tree / CompiledTree ──────────────────────────────────────


def test_compile_tree_reads_compiled_files(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(), path=tmp_path / "out")

    assert isinstance(tree, CompiledTree)
    assert tree.profiles == ("default", "azure")
    assert tree.default_profile == "default"
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
    recipe = _recipe()
    first, second = compile_tree(recipe), compile_tree(recipe)
    assert first.root != second.root
    assert first.conf() == second.conf()


def test_compile_tree_selects_variants(tmp_path: Path) -> None:
    recipe = _recipe()
    tree = compile_tree(recipe, variants=["default", "azure"], path=tmp_path / "all")
    assert tree.profiles == tree.variants == ("default", "azure")
    assert tree.profile("azure") == tmp_path / "all" / "azure"
    assert "jq" in tree.conf(profile="azure")
    assert "jq" not in tree.conf()

    only = compile_tree(recipe, variants="azure", path=tmp_path / "azure-only")
    assert only.profiles == ("azure",)
    assert only.default_profile == "azure"
    assert "jq" in only.conf()


def test_compile_tree_rejects_unknown_and_uncompiled_variants(tmp_path: Path) -> None:
    recipe = _recipe()
    with pytest.raises(ValidationError, match="Unknown variant"):
        compile_tree(recipe, variants=["gpc"], path=tmp_path / "x")
    tree = compile_tree(recipe, variants="default", path=tmp_path / "y")
    with pytest.raises(KeyError, match="was not compiled"):
        tree.profile("azure")


def test_compile_tree_missing_unit_or_script_lists_what_exists(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(), path=tmp_path / "out")
    with pytest.raises(FileNotFoundError, match="app.service"):
        tree.unit("missing")
    with pytest.raises(FileNotFoundError, match="postinst"):
        tree.script("build")


def test_compile_tree_writes_the_tree_compile_returns(tmp_path: Path) -> None:
    recipe = _recipe()
    on_disk = compile_tree(recipe, path=tmp_path / "out")
    assert read_tree(on_disk.root).digest == compile(recipe).digest


# ── assert_clean / assert_diagnostic ─────────────────────────────────


def test_assert_clean_passes_warnings_unless_strict() -> None:
    recipe = _warning_only()
    diagnostics = assert_clean(recipe)
    assert [d.code for d in diagnostics] == ["service-command-not-shipped"]
    with pytest.raises(AssertionError, match="warning service-command-not-shipped"):
        assert_clean(recipe, strict=True)
    assert_clean(recipe, strict=True, allow=("service-command-not-shipped",))
    assert assert_clean(_recipe(), strict=True) == []


def test_assert_clean_fails_on_errors_with_rendered_report() -> None:
    recipe = _ghost()
    with pytest.raises(AssertionError) as excinfo:
        assert_clean(recipe)
    message = str(excinfo.value)
    assert "error service-user-missing [default] app" in message
    assert "hint:" in message
    assert_clean(recipe, allow=["service-user-missing"])


def test_assert_clean_checks_selected_variants() -> None:
    recipe = _ghost(where="azure")
    assert_clean(recipe, variants="default")
    with pytest.raises(AssertionError, match=r"\[azure\]"):
        assert_clean(recipe)
    with pytest.raises(AssertionError, match=r"\[azure\]"):
        assert_clean(recipe, variants=["azure"])


def test_assert_clean_takes_lint_diagnostics_strictly() -> None:
    found = lint(_warning_only())
    with pytest.raises(
        AssertionError, match=r"warning service-command-not-shipped \[default\] app"
    ):
        assert_clean(found)
    assert assert_clean(found, strict=False) is found
    assert assert_clean(lint(_recipe())) == ()
    with pytest.raises(TypeError, match="variants="):
        assert_clean(found, variants="default")


def test_assert_diagnostic_returns_the_match() -> None:
    found = assert_diagnostic(_ghost(), "service-user-missing", level="error", subject="app")
    assert found.profile == "default"
    assert "ghost" in found.message
    assert found.hint is not None


def test_assert_diagnostic_filters_by_variant() -> None:
    recipe = _ghost(where="azure")
    found = assert_diagnostic(recipe, "service-user-missing", profile="azure")
    assert found.profile == "azure"
    assert assert_diagnostic(recipe, "service-user-missing", variant="azure") == found
    with pytest.raises(AssertionError, match="no findings"):
        assert_diagnostic(recipe, "service-user-missing", variants="default")


def test_assert_diagnostic_takes_lint_diagnostics() -> None:
    found = assert_diagnostic(lint(_ghost()), "service-user-missing", variant="default")
    assert isinstance(found, Diagnostic)
    assert (found.level, found.subject) == ("error", "app")


def test_assert_diagnostic_failure_lists_every_finding() -> None:
    recipe = _warning_only()
    with pytest.raises(AssertionError) as excinfo:
        assert_diagnostic(recipe, "service-user-missing", subject="app")
    message = str(excinfo.value)
    assert "code='service-user-missing', subject='app'" in message
    assert "warning service-command-not-shipped [default]" in message
    with pytest.raises(AssertionError, match="level='error'"):
        assert_diagnostic(recipe, "service-command-not-shipped", level="error")
    with pytest.raises(AssertionError, match="no findings"):
        assert_diagnostic(lint(_recipe()), "service-command-not-shipped")


# ── assert_tree / assert_tree_matches ────────────────────────────────


def test_assert_tree_round_trip_reports_each_path(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree(compile(_recipe()), golden, update=True)
    assert_tree(compile(_recipe()), golden)

    changed = compile(_recipe(File("/etc/new", "new\n")))
    (golden / "default" / "mkosi.extra" / "etc" / "stale").write_text("old\n", encoding="utf-8")
    with pytest.raises(AssertionError) as excinfo:
        assert_tree(changed, golden)
    message = str(excinfo.value)
    assert "+ default/mkosi.extra/etc/stale" in message
    assert "- default/mkosi.extra/etc/new" in message
    assert "TUNDRAVM_UPDATE_GOLDEN=1" in message


def test_golden_round_trip(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, update=True)
    assert (golden / "default" / "mkosi.conf").is_file()
    assert assert_tree_matches(_recipe(), golden).is_clean

    changed = _recipe(Package("htop"), File("/etc/new", "new\n"))
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


def test_golden_ignores_and_keeps_variants_not_compiled(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, variants=["default", "azure"], update=True)
    azure_conf = (golden / "azure" / "mkosi.conf").read_text(encoding="utf-8")

    changed = _recipe(Package("htop"))
    assert_tree_matches(changed, golden, variants="default", update=True)
    assert (golden / "azure" / "mkosi.conf").read_text(encoding="utf-8") == azure_conf
    assert_tree_matches(changed, golden, variants="default")
    with pytest.raises(AssertionError, match="azure/mkosi.conf"):
        assert_tree_matches(changed, golden, variants=["azure"])


def test_golden_truncates_long_diffs(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_single(), golden, update=True)
    big = _single(File("/etc/big", "".join(f"line {n}\n" for n in range(500))))
    with pytest.raises(AssertionError, match=r"more diff lines"):
        assert_tree_matches(big, golden)


# ── fake_bake / bake_in_process ──────────────────────────────────────


def test_fake_bake_merges_into_one_manifest(tmp_path: Path) -> None:
    recipe = _recipe()
    tree = compile(recipe)
    qemu = fake_bake(tree, variant="default", target="qemu", out=tmp_path)
    azure = fake_bake(tree, variant="azure", target="azure", out=tmp_path)
    assert qemu.path == tmp_path / "default" / "disk.qcow2"
    assert azure.path == tmp_path / "azure" / "disk.vhd"
    assert qemu.simulated and azure.simulated
    assert qemu.tree_digest == tree.digest
    assert set(read_artifacts(tmp_path)) == {qemu, azure}


def test_bake_in_process_writes_simulated_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "build"
    artifacts = bake_in_process(_recipe(), out=out)
    assert {(a.variant, a.target) for a in artifacts} == {("default", "qemu"), ("azure", "azure")}
    for artifact in artifacts:
        assert artifact.simulated
        assert artifact.path.is_file()
        assert artifact.path.is_relative_to(out)
    assert set(read_artifacts(out)) == set(artifacts)
    assert (out / "tundravm.lock").is_file()


def test_bake_in_process_defaults_to_temp_dir_and_takes_a_lock() -> None:
    recipe = _recipe()
    locked = lock(recipe, variants=["azure"])
    (artifact,) = bake_in_process(recipe, variants=["azure"], lock=locked)
    assert (artifact.variant, artifact.target) == ("azure", "azure")
    assert artifact.path.is_file()


# ── fake_fragment ────────────────────────────────────────────────────


def test_fake_fragment_declares_packages_and_files(tmp_path: Path) -> None:
    fake = fake_fragment("x", packages=("pkg",), files={"/etc/x": "content"})
    assert fake.name == "x"
    assert fake.items == (Package("pkg"), File("/etc/x", "content"))
    assert (fake.requires, fake.checks) == ((), ())

    tree = compile_tree(Recipe(name="demo", common=fake), path=tmp_path / "out")
    assert "pkg" in tree.conf()
    assert tree.read("mkosi.extra/etc/x") == "content"


def test_fake_fragment_lands_only_in_its_variant(tmp_path: Path) -> None:
    fake = fake_fragment(packages=("pkg",))
    assert fake.name == "fake"
    recipe = Recipe(
        name="demo",
        common=Fragment("app"),
        variants=(
            Variant("default", target="qemu"),
            Variant("azure", target="azure", add=Fragment("azure", (fake,))),
        ),
    )
    tree = compile_tree(recipe, path=tmp_path / "out")
    assert "pkg" in tree.conf(profile="azure")
    assert "pkg" not in tree.conf()


def test_fake_fragment_requires_other_fragments() -> None:
    a = fake_fragment("a")
    b = fake_fragment("b", requires=(a,))
    assert b.requires == ("a",)
    alone = Recipe(name="demo", common=Fragment("app", (b,)))
    found = assert_diagnostic(lint(alone), "fragment-requires-missing", subject="b")
    assert "'a'" in found.message
    assert_clean(lint(Recipe(name="demo", common=Fragment("app", (KERNEL, a, b)))))


def test_fake_fragment_requires_a_fragment_by_name() -> None:
    dep = fake_fragment("dep", requires=("base-tools",))
    with pytest.raises(AssertionError, match="fragment-requires-missing"):
        assert_clean(Recipe(name="demo", common=Fragment("app", (dep,))))
    tools = Fragment("base-tools", (Package("curl"), KERNEL))
    assert_clean(Recipe(name="demo", common=Fragment("app", (tools, dep))), strict=True)


def test_fake_fragment_init_scripts_compose_by_priority(tmp_path: Path) -> None:
    silent = fake_fragment("silent", packages=("pkg",))
    assert not any(isinstance(item, Init) for item in silent.items)
    common = Fragment(
        "app",
        (
            fake_fragment("late", init="echo late", priority=40),
            fake_fragment("early", init="echo early", priority=10),
            silent,
        ),
    )
    init = compile_tree(Recipe(name="demo", common=common), path=tmp_path / "out").runtime_init()
    assert init.index("echo early") < init.index("echo late")
    assert init.count("echo ") == 2


def test_fake_fragment_collision_is_diagnosed() -> None:
    common = Fragment("app", (fake_fragment("a", init="echo a"), fake_fragment("b", init="echo b")))
    found = assert_diagnostic(
        Recipe(name="demo", common=common), "init-priority-collision", subject="priority 50"
    )
    assert found.level == "warning"


def test_fake_fragment_checks_run_per_variant() -> None:
    seen: list[str] = []

    def check(resolved: Resolved) -> tuple[Diagnostic, ...]:
        seen.append(resolved.variant)
        return (Diagnostic(code="fake-check", message="checked", level="warning"),)

    recipe = Recipe(
        name="demo",
        common=fake_fragment(checks=(check,)),
        variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
    )
    found = lint(recipe)
    assert set(seen) == {"azure", "default"}
    assert assert_diagnostic(found, "fake-check", variant="azure").subject == ""
    assert assert_diagnostic(found, "fake-check", variant="default").level == "warning"


# ── recipe_file / run_cli ────────────────────────────────────────────


def test_recipe_file_and_run_cli(tmp_path: Path) -> None:
    path = recipe_file(
        tmp_path,
        """
        from tundravm import Fragment, Package, Recipe

        recipe = Recipe(
            name="demo",
            common=Fragment("demo", (Package("curl"), Package("linux-image-amd64"))),
        )
        """,
    )
    assert path == tmp_path / "recipe.py"
    assert path.read_text(encoding="utf-8").startswith("from tundravm import Fragment")

    code, out, err = run_cli("lint", path)
    assert code == 0
    assert "no findings" in out
    assert err == ""

    code, out, _ = run_cli("compile", path, "--out", tmp_path / "tree")
    assert code == 0, out
    assert (tmp_path / "tree" / "default" / "mkosi.conf").is_file()


def test_run_cli_captures_usage_errors_and_sdk_errors(tmp_path: Path) -> None:
    code, out, err = run_cli("no-such-command")
    assert code == 2
    assert "usage:" in err
    assert out == ""

    broken = recipe_file(tmp_path, "recipe = 42\n", name="sub/broken.py")
    assert broken == tmp_path / "sub" / "broken.py"
    code, _, err = run_cli("lint", broken)
    assert code != 0
    assert "error [" in err
    assert "does not define a Recipe" in err
