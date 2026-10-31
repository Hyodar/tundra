"""Tests for the ``tundravm.testing`` helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tundravm.declarative import (
    BootStep,
    Diagnostic,
    File,
    Fragment,
    Hook,
    Package,
    Ready,
    Recipe,
    Service,
    Setting,
    User,
    Variant,
    compile,
    lint,
)
from tundravm.declarative.lifecycle import read_tree
from tundravm.errors import ValidationError
from tundravm.testing import (
    CompiledTree,
    FakeMkosi,
    FakeSources,
    assert_clean,
    assert_diagnostic,
    assert_tree,
    assert_tree_matches,
    compile_tree,
    fake_fragment,
    fragment_plan,
    plan,
    recipe_file,
    run_cli,
)

AZURE = Variant("azure", outputs=("azure",), add=Fragment("azure", (Package("jq"),)))
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
            BootStep("boot", "echo booted"),
            *extra,
        ),
    )
    return Recipe(
        name="demo", items=(common,), variants=(Variant("default", outputs=("qemu",)), AZURE)
    )


def _single(*items: Package | File | User | Service) -> Recipe:
    return Recipe(name="demo", items=(NOT_BOOTABLE, *items))


def _ghost(*, where: str = "common") -> Recipe:
    """A recipe whose ``app`` service runs as an undeclared user, in common or in ``azure``."""
    service = Service("app", "/usr/bin/app", user="ghost")
    if where == "common":
        return _single(service)
    variant = Variant("azure", outputs=("azure",), add=Fragment("azure", (service,)))
    return Recipe(
        name="demo",
        items=(KERNEL,),
        variants=(Variant("default", outputs=("qemu",)), variant),
    )


def _warning_only() -> Recipe:
    """One ``service-command-not-shipped`` warning: nothing ships ``/usr/bin/app``."""
    return _single(User("app"), Service("app", "/usr/bin/app", user="app"))


# ── compile_tree / CompiledTree ──────────────────────────────────────


def test_compile_tree_reads_compiled_files(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(), path=tmp_path / "out")

    assert isinstance(tree, CompiledTree)
    assert tree.variants == ("default", "azure")
    assert tree.default_variant == "default"
    assert os.fspath(tree) == str(tmp_path / "out")
    assert "curl" in tree.conf()
    assert tree.read("mkosi.extra/etc/motd") == "hello\n"
    assert tree.read("/mkosi.extra/etc/motd", variant="default") == "hello\n"
    assert tree.exists("mkosi.extra/etc/motd")
    assert not tree.exists("mkosi.extra/etc/nope")
    assert "User=app" in tree.unit("app")
    assert tree.unit("app.service") == tree.unit("app")
    assert "echo configured" in tree.script("postinst")
    assert "echo booted" in tree.runtime_init()
    files = tree.files()
    assert files == sorted(files)
    assert {"mkosi.conf", "mkosi.extra/etc/motd"} <= set(files)


def test_compile_tree_rejects_unknown_and_uncompiled_variants(tmp_path: Path) -> None:
    recipe = _recipe()
    with pytest.raises(ValidationError, match="Unknown profile"):
        compile_tree(recipe, variants=["gpc"], path=tmp_path / "x")
    tree = compile_tree(recipe, variants="default", path=tmp_path / "y")
    with pytest.raises(KeyError, match="was not compiled"):
        tree.variant("azure")


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


def test_assert_clean_fails_on_warnings_for_every_input() -> None:
    recipe = _warning_only()
    for subject in (recipe, lint(recipe), plan(recipe)):
        with pytest.raises(AssertionError, match="warning service-command-not-shipped"):
            assert_clean(subject)
        found = assert_clean(subject, warnings="allow")
        assert [d.code for d in found] == ["service-command-not-shipped"]
        assert_clean(subject, allow=("service-command-not-shipped",))
    assert assert_clean(_recipe()) == []


def test_assert_clean_fails_on_errors_with_rendered_report() -> None:
    recipe = _ghost()
    with pytest.raises(AssertionError) as excinfo:
        assert_clean(recipe, warnings="allow")
    message = str(excinfo.value)
    assert "error service-user-missing [default] app" in message
    assert "hint:" in message
    assert_clean(recipe, allow=["service-user-missing", "service-command-not-shipped"])


def test_assert_clean_checks_selected_variants() -> None:
    recipe = _ghost(where="azure")
    assert_clean(recipe, variants="default")
    with pytest.raises(AssertionError, match=r"\[azure\]"):
        assert_clean(recipe)
    with pytest.raises(AssertionError, match=r"\[azure\]"):
        assert_clean(recipe, variants=["azure"])


def test_assert_clean_returns_lint_diagnostics_as_given() -> None:
    found = lint(_warning_only())
    assert assert_clean(found, warnings="allow") is found
    assert assert_clean(lint(_recipe())) == ()
    with pytest.raises(TypeError, match="variants="):
        assert_clean(found, variants="default")
    with pytest.raises(TypeError, match="variants="):
        assert_clean(plan(_recipe()), variants="default")


def test_assert_diagnostic_filters_by_variant() -> None:
    recipe = _ghost(where="azure")
    found = assert_diagnostic(recipe, "service-user-missing", variant="azure")
    assert found.variant == "azure"
    assert assert_diagnostic(recipe, "service-user-missing", variant="azure") == found
    with pytest.raises(AssertionError, match="no findings"):
        assert_diagnostic(recipe, "service-user-missing", variants="default")


def test_assert_diagnostic_takes_lint_diagnostics() -> None:
    found = assert_diagnostic(lint(_ghost()), "service-user-missing", variant="default")
    assert isinstance(found, Diagnostic)
    assert (found.severity, found.subject) == ("error", "app")


def test_assert_diagnostic_failure_lists_every_finding() -> None:
    recipe = _warning_only()
    with pytest.raises(AssertionError) as excinfo:
        assert_diagnostic(recipe, "service-user-missing", subject="app")
    message = str(excinfo.value)
    assert "code='service-user-missing', subject='app'" in message
    assert "warning service-command-not-shipped [default]" in message
    with pytest.raises(AssertionError, match="severity='error'"):
        assert_diagnostic(recipe, "service-command-not-shipped", severity="error")
    with pytest.raises(AssertionError, match="no findings"):
        assert_diagnostic(lint(_recipe()), "service-command-not-shipped")


# ── assert_tree / assert_tree_matches ────────────────────────────────


def test_assert_tree_round_trip_reports_each_path(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree(compile(_recipe()), golden, update=True)
    assert_tree(compile(_recipe()), golden)

    changed = compile(_recipe(File("/etc/new", "new\n")))
    (golden / "payload" / "mkosi.extra" / "etc" / "stale").write_text("old\n", encoding="utf-8")
    with pytest.raises(AssertionError) as excinfo:
        assert_tree(changed, golden)
    message = str(excinfo.value)
    assert "+ payload/mkosi.extra/etc/stale" in message
    assert "- payload/mkosi.extra/etc/new" in message
    assert "TUNDRAVM_UPDATE_GOLDEN=1" in message


def test_golden_round_trip(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, update=True)
    assert (golden / "payload" / "mkosi.conf").is_file()
    assert assert_tree_matches(_recipe(), golden).is_clean

    changed = _recipe(Package("htop"), File("/etc/new", "new\n"))
    with pytest.raises(AssertionError) as excinfo:
        assert_tree_matches(changed, golden)
    message = str(excinfo.value)
    assert "M  payload/mkosi.conf" in message
    assert "A  payload/mkosi.extra/etc/new" in message
    assert "+htop" in message or "htop" in message
    assert "TUNDRAVM_UPDATE_GOLDEN=1" in message


def test_golden_update_from_env_rewrites_and_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_recipe(), golden, update=True)
    stale = golden / "payload" / "mkosi.extra" / "etc" / "stale"
    stale.write_text("old\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="D  payload/mkosi.extra/etc/stale"):
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
    overlay = golden / "payload" / "mkosi.profiles" / "azure" / "mkosi.conf"
    wrapper = golden / "mkosi.images" / "azure" / "mkosi.conf"
    kept = (overlay.read_text(encoding="utf-8"), wrapper.read_text(encoding="utf-8"))

    changed = _recipe(Package("htop"))
    assert_tree_matches(changed, golden, variants="default", update=True)
    assert (overlay.read_text(encoding="utf-8"), wrapper.read_text(encoding="utf-8")) == kept
    assert_tree_matches(changed, golden, variants="default")
    with pytest.raises(AssertionError, match="payload/mkosi.conf"):
        assert_tree_matches(_recipe(), golden, variants=["azure"])


def test_golden_truncates_long_diffs(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    assert_tree_matches(_single(), golden, update=True)
    big = _single(File("/etc/big", "".join(f"line {n}\n" for n in range(500))))
    with pytest.raises(AssertionError, match=r"more diff lines"):
        assert_tree_matches(big, golden)


# ── FakeMkosi / FakeSources ──────────────────────────────────────────

IMAGE = """\
from tundravm import Image, Service

image = Image("demo", packages=["busybox"], services=[Service("app", ["/usr/bin/busybox"])])
"""
"""An image: it implies the evidence service, which builds from a git source."""


def test_the_fakes_run_a_whole_build_with_nothing_forked_or_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mkosi = FakeMkosi().install(monkeypatch.setattr)
    sources = FakeSources().install(monkeypatch.setattr)
    (tmp_path / "demo.py").write_text(IMAGE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    code, out, err = run_cli("build", "demo.py")
    assert code == 0, err
    assert "built build/demo/production/" in out
    assert mkosi.verbs() == ["summary", "build", "summary"]
    (fetched,) = sources.fetched
    assert fetched.url == "https://github.com/Hyodar/tundra-tools.git"
    assert fetched.path.is_relative_to(tmp_path / "build" / "demo" / ".work" / ".sources")

    code, _, err = run_cli("build", "demo.py")
    assert code == 0, err
    assert mkosi.verbs().count("build") == 2
    assert len(sources.fetched) == 1, "the second build reuses the complete checkout"


# ── fake_fragment ────────────────────────────────────────────────────


def test_fake_fragment_declares_packages_and_files() -> None:
    fake = fake_fragment("x", packages=("pkg",), files={"/etc/x": "content"})
    assert fake.name == "x"
    assert fake.items == (Package("pkg"), File("/etc/x", "content"))
    assert (fake.requires, fake.checks) == ((), ())

    image = fragment_plan(fake)
    assert image.packages["pkg"] == "runtime"
    assert image.file("/etc/x").text == "content"
    assert image.file("/etc/x").fragment == "x"


def test_fake_fragment_init_scripts_compose_by_after() -> None:
    silent = fake_fragment("silent", packages=("pkg",))
    assert not any(isinstance(item, BootStep) for item in silent.items)
    image = fragment_plan(
        fake_fragment("late", init="echo late", after=[Ready("early")]),
        fake_fragment("early", init="echo early"),
        silent,
    )
    assert [(step.name, step.after) for step in image.init_steps] == [
        ("early", ()),
        ("late", ("early",)),
    ]
    unit = image.file("/usr/lib/systemd/system/runtime-init-late.service").text
    assert "Requires=runtime-init-early.service" in unit
    assert image.file("/usr/lib/runtime-init/late").text.endswith("echo late")


# ── recipe_file / run_cli ────────────────────────────────────────────


def test_run_cli_captures_usage_errors_and_sdk_errors(tmp_path: Path) -> None:
    code, out, err = run_cli("no-such-command")
    assert code == 2
    assert "usage:" in err
    assert out == ""

    broken = recipe_file(tmp_path, "recipe = 42\n", name="sub/broken.py")
    assert broken == tmp_path / "sub" / "broken.py"
    code, _, err = run_cli("check", broken)
    assert code != 0
    assert "error [" in err
    assert "does not define an Image" in err
