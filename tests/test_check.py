"""Tests for the recipe linter (``tundravm.lint`` / ``tundravm lint``).

The compiler rules in ``tundravm.check`` run on the lowered recipe: ``report``
is the CLI's lint report (resolution diagnostics, then the compiler rules,
without ``backend-missing``); ``check(lower(recipe))`` runs the rules alone.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tundravm import (
    Backend,
    Debloat,
    Declaration,
    File,
    Fragment,
    Hook,
    Init,
    LintError,
    Package,
    Recipe,
    Secret,
    SecretFile,
    Secrets,
    Service,
    Template,
    User,
    ValidationError,
    Variant,
    bake,
    lint,
    lock,
)
from tundravm.backends.inprocess import InProcessBackend
from tundravm.check import Diagnostic, check, render
from tundravm.cli import main
from tundravm.declarative import lower
from tundravm.declarative.lifecycle import check_report
from tundravm.models import SecretSpec

DEFAULT = Variant("default", target="qemu")
CLEAN: tuple[Declaration, ...] = (
    Package("curl"),
    User("app", system=True),
    Service("app", "/usr/bin/app", user="app"),
    File("/etc/motd", "hi\n"),
)
PLATFORM_MARKERS = frozenset(
    {
        "/usr/bin/azure-complete-provisioning",
        "/usr/lib/systemd/system/azure-complete-provisioning.service",
        "/usr/lib/udev/rules.d/65-gce-disk-naming.rules",
        "/usr/lib/udev/google_nvme_id",
    }
)


def recipe(
    *items: Declaration, variants: tuple[Variant, ...] = (DEFAULT,), clean: bool = True
) -> Recipe:
    common = (*CLEAN, *items) if clean else items
    return Recipe("check", Fragment("common", items=common), variants=variants)


def report(subject: Recipe, *variants: str) -> list[Diagnostic]:
    return check_report(subject, None, variants=variants or None)


def codes(subject: Recipe, *variants: str) -> list[str]:
    return [d.code for d in report(subject, *variants)]


def test_clean_recipe_has_no_findings() -> None:
    assert report(recipe()) == []
    assert lint(recipe()) == ()


# a. service-user-missing


def test_service_user_missing() -> None:
    [diag] = report(recipe(Service("worker", "/usr/bin/worker", user="svc")))
    assert (diag.level, diag.code, diag.subject) == ("error", "service-user-missing", "worker")
    assert "'svc'" in (diag.hint or "")


def test_service_user_inherited_from_default() -> None:
    dev = Variant(
        "dev", add=Fragment("dev", items=(Service("worker", "/usr/bin/app", user="app"),))
    )
    assert report(recipe(variants=(DEFAULT, dev)), "dev") == []


def test_service_user_not_inherited_by_standalone_profile() -> None:
    items = (Package("curl"), Service("app", "/usr/bin/app", user="app"))
    dev = Variant("dev", parent=None, add=Fragment("dev", items=items))
    [diag] = report(recipe(variants=(DEFAULT, dev)), "dev")
    assert diag.code == "service-user-missing"
    assert "does not inherit" in (diag.hint or "")


def test_service_user_root_base_or_hook_created_is_fine() -> None:
    subject = recipe(
        Service("a", "/usr/bin/a", user="root"),
        Service("b", "/usr/bin/b", user="nobody"),
        Hook("hookuser", "postinst", "mkosi-chroot useradd --system hookuser"),
        Service("c", "/usr/bin/c", user="hookuser"),
    )
    assert report(subject) == []


# b. file-path-duplicate


def test_file_path_duplicate_with_different_content() -> None:
    [diag] = report(recipe(Template("/etc/motd", "bye {x}\n", variables=(("x", 1),))))
    assert (diag.level, diag.code, diag.subject) == ("error", "file-path-duplicate", "/etc/motd")
    assert "file, template" in diag.message


def test_identical_redeclaration_and_skeleton_override_are_fine() -> None:
    subject = recipe(
        File("/etc/motd", "hi\n"),
        Template("/etc/motd", "hi\n"),
        File("/etc/motd", "build-time\n", stage="skeleton"),
    )
    assert report(subject) == []


# c. file-path-relative


def test_file_path_relative() -> None:
    with pytest.raises(ValidationError, match="must be an absolute path"):
        File("etc/relative", "x")
    subject = recipe(File("/etc/../../escape", "x", stage="skeleton"), File("/opt/../../up", "y"))
    diags = report(subject)
    assert [(d.code, d.subject) for d in diags] == [
        ("file-path-relative", "/etc/../../escape"),
        ("file-path-relative", "/opt/../../up"),
    ]
    assert all(d.level == "error" for d in diags)


# d. service-command-not-shipped


def test_service_command_not_shipped() -> None:
    [diag] = report(recipe(Service("tool", "/usr/local/bin/tool --serve"), clean=False))
    assert (diag.level, diag.code, diag.subject) == (
        "warning",
        "service-command-not-shipped",
        "tool",
    )
    assert "no packages" in (diag.hint or "")


def test_service_command_shipped_by_file_hook_package_or_essential() -> None:
    shipped = (
        File("/usr/local/bin/tool", "#!/bin/sh\n", mode=0o755),
        Service("tool", "/usr/local/bin/tool"),
        Hook("builder", "build", "cp out/builder $DESTDIR/opt/builder"),
        Service("builder", "/opt/builder"),
        Service("shell", "/usr/bin/bash -c true"),
    )
    assert report(recipe(*shipped, clean=False)) == []
    packaged = recipe(*shipped, Service("other", "/usr/bin/other"), Package("other"), clean=False)
    assert report(packaged) == []


# e. output-target-platform-mismatch / platform-target-missing
#
# A cloud target always lowers with its platform module, so these rules only
# fire on a lowered image whose platform files or target were changed after
# lowering.


def cloud_recipe() -> Recipe:
    variants = (DEFAULT, Variant("azure", target="azure"), Variant("gcp", target="gcp"))
    return recipe(variants=variants)


def test_output_target_without_platform() -> None:
    img = lower(cloud_recipe())
    for name in ("azure", "gcp"):
        state = img.state.profiles[name]
        state.files = [f for f in state.files if f.path not in PLATFORM_MARKERS]
        state.skeleton_files = [f for f in state.skeleton_files if f.path not in PLATFORM_MARKERS]
    img.backend = InProcessBackend()
    diags = check(img, profiles=["azure", "gcp"])
    assert [(d.profile, d.code, d.subject) for d in diags] == [
        ("azure", "output-target-platform-mismatch", "azure"),
        ("gcp", "output-target-platform-mismatch", "gcp"),
    ]


def test_platform_applied_or_guest_agent_is_fine() -> None:
    agent = Variant("agent", target="azure", add=Fragment("agent", items=(Package("waagent"),)))
    subject = recipe(variants=(*cloud_recipe().variants, agent))
    assert report(subject, "azure", "gcp", "agent") == []


def test_platform_applied_but_target_overridden() -> None:
    img = lower(cloud_recipe())
    img.state.profiles["azure"].output_targets = ("qemu",)
    img.backend = InProcessBackend()
    [diag] = check(img, profiles=["azure"])
    assert diag.code == "platform-target-missing"
    assert diag.subject == "azure"
    assert "no azure artifact" in diag.message


def test_gcp_platform_applied_but_target_overridden() -> None:
    img = lower(cloud_recipe())
    img.state.profiles["gcp"].output_targets = ("qemu",)
    img.backend = InProcessBackend()
    assert [(d.code, d.subject) for d in check(img, profiles=["gcp"])] == [
        ("platform-target-missing", "gcp")
    ]
    child = Variant("plain", parent="gcp", target="qemu")
    subject = recipe(variants=(*cloud_recipe().variants, child))
    assert [(d.code, d.subject) for d in lint(subject, variants=["plain"])] == [
        ("target-inconsistent", "plain")
    ]


# f. profile-empty


def test_profile_empty() -> None:
    [diag] = report(recipe(variants=(DEFAULT, Variant("bare"))), "bare")
    assert (diag.level, diag.code, diag.profile) == ("info", "profile-empty", "bare")


def test_profile_with_content_or_default_is_not_empty() -> None:
    dev = Variant("dev", add=Fragment("dev", items=(Package("dropbear"),)))
    assert codes(recipe(variants=(DEFAULT, dev), clean=False), "default", "dev") == []


# g. init-priority-collision


def test_init_priority_collision() -> None:
    subject = recipe(
        Init("one", "echo one\n", priority=20),
        Init("two", "echo two\n", priority=20),
        Init("three", "echo three\n", priority=30),
    )
    [diag] = report(subject)
    assert (diag.code, diag.subject) == ("init-priority-collision", "priority 20")
    assert "'echo one'" in diag.message


def test_distinct_or_duplicate_init_scripts_are_fine() -> None:
    subject = recipe(
        Init("one", "echo one\n", priority=20),
        Init("one-again", "echo one\n", priority=20),
        Init("two", "echo two\n", priority=30),
    )
    assert report(subject) == []


# h. backend-missing


def test_backend_missing_reported_once_under_default() -> None:
    dev = Variant("dev", add=Fragment("dev", items=(Package("htop"),)))
    subject = recipe(Package("curl"), variants=(DEFAULT, dev), clean=False)
    img = lower(subject)
    diags = check(img, profiles=["default", "dev"])
    assert [(d.code, d.profile) for d in diags] == [("backend-missing", "default")]
    img.backend = InProcessBackend()
    assert check(img, profiles=["default", "dev"]) == []
    assert report(subject) == []  # a recipe's backend is a bake() argument


# i. debloat-removes-needed-unit / debloat-removes-declared-file

NEEDS_NETWORKD = Service("net", "/usr/bin/net", requires=("systemd-networkd.service",))


def test_debloat_masks_needed_units() -> None:
    resolved = Service("systemd-resolved", "/usr/lib/systemd/systemd-resolved")
    diags = report(recipe(NEEDS_NETWORKD, resolved))
    assert [(d.code, d.subject) for d in diags] == [
        ("debloat-removes-needed-unit", "net"),
        ("debloat-removes-needed-unit", "systemd-resolved"),
    ]
    assert "keep_units" in (diags[0].hint or "")


def test_debloat_keep_extra_or_disabled_is_fine() -> None:
    kept = Debloat(keep_units_extra=("systemd-networkd.service",))
    assert report(recipe(NEEDS_NETWORKD, kept)) == []
    assert report(recipe(NEEDS_NETWORKD, Debloat(enabled=False))) == []


def test_debloat_removes_declared_file() -> None:
    network = File("/etc/systemd/network/10-eth.network", "[Match]\n")
    [diag] = report(recipe(network))
    assert (diag.code, diag.subject) == (
        "debloat-removes-declared-file",
        "/etc/systemd/network/10-eth.network",
    )
    assert report(recipe(network, Debloat(keep_paths=("/etc/systemd/network",)))) == []


# j. secret-undelivered


def test_secret_undelivered() -> None:
    with pytest.raises(ValidationError, match="at least one delivery target"):
        Secret("token", targets=())
    img = lower(recipe())
    img.state.profiles["default"].secrets.append(SecretSpec(name="token"))
    img.backend = InProcessBackend()
    diags = check(img)
    assert [(d.code, d.subject) for d in diags] == [
        ("secret-undelivered", None),
        ("secret-undelivered", "token"),
    ]


def test_secret_delivery_applied_is_fine() -> None:
    # no Disk store: store=None, otherwise secret-store-undefined
    secrets = Secrets(entries=(Secret("token", (SecretFile("/run/token"),)),))
    # The delivery tool builds from an unpinned branch; that is source-unpinned's concern.
    assert [d for d in report(recipe(secrets)) if d.code != "source-unpinned"] == []


# rendering + ordering


def test_ordering_is_deterministic() -> None:
    variants = (
        DEFAULT,
        Variant("b", add=Fragment("b", items=(File("/etc/../../escape", "x"),))),
        Variant("a"),
    )
    subject = recipe(
        Init("one", "echo 1\n", priority=1),
        Init("two", "echo 2\n", priority=1),
        variants=variants,
        clean=False,
    )
    img = lower(subject)
    diags = check(img, profiles=["b", "a", "default"])
    keys = [(d.profile, d.level, d.code) for d in diags]
    assert keys == [
        ("a", "warning", "init-priority-collision"),
        ("a", "info", "profile-empty"),
        ("b", "error", "file-path-relative"),
        ("b", "warning", "init-priority-collision"),
        ("default", "warning", "backend-missing"),
        ("default", "warning", "init-priority-collision"),
    ]
    assert diags == check(img, profiles=["default", "a", "b"])
    assert [(d.variant, d.code) for d in lint(subject)] == [
        (profile, code) for profile, _, code in keys if code != "backend-missing"
    ]


def test_render_text_and_dict() -> None:
    assert render([]) == "no findings"
    diag = Diagnostic(
        level="error",
        code="service-user-missing",
        message="boom",
        hint="fix it",
        profile="default",
        subject="app",
    )
    info = Diagnostic(level="info", code="profile-empty", message="m", hint=None, profile="x")
    text = render([diag, info])
    assert text.splitlines() == [
        "error service-user-missing [default] app: boom",
        "    hint: fix it",
        "info profile-empty [x]: m",
        "1 error, 0 warnings, 1 info",
    ]
    assert diag.to_dict() == {
        "level": "error",
        "code": "service-user-missing",
        "message": "boom",
        "hint": "fix it",
        "profile": "default",
        "subject": "app",
    }


# CLI

RECIPE = """
from tundravm import Fragment, Init, Package, Recipe, Service, User, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
items = [Package("curl"), User("app", system=True), Service("app", "/usr/bin/app", user="app")]
variants = [Variant("default", target="qemu")]
"""

BIND = """
recipe = Recipe("cli", Fragment("common", items=tuple(items)), variants=tuple(variants))
"""


def write_recipe(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "recipe.py"
    path.write_text(f"{RECIPE}{extra}{BIND}", encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(["lint", *argv], stdout=out), out.getvalue()


def test_cli_clean_recipe(tmp_path: Path) -> None:
    code, out = run(str(write_recipe(tmp_path)))
    assert (code, out) == (0, "no findings\n")


def test_cli_error_exit_and_json(tmp_path: Path) -> None:
    extra = 'items.append(Service("w", "/usr/bin/w", user="ghost"))\n'
    recipe_path = write_recipe(tmp_path, extra)
    code, out = run(str(recipe_path))
    assert code == 1
    assert out.startswith("error service-user-missing [default] w:")
    assert out.rstrip().endswith("1 error, 0 warnings, 0 infos")

    code, out = run(str(recipe_path), "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["summary"] == {"errors": 1, "warnings": 0, "infos": 0}
    assert payload["diagnostics"][0]["code"] == "service-user-missing"


def test_cli_strict_fails_on_warnings(tmp_path: Path) -> None:
    extra = (
        'items.append(Init("one", "echo 1\\n", priority=5))\n'
        'items.append(Init("two", "echo 2\\n", priority=5))\n'
    )
    recipe_path = write_recipe(tmp_path, extra)
    assert run(str(recipe_path))[0] == 0
    assert run(str(recipe_path), "--strict")[0] == 1


def test_cli_variant_selection(tmp_path: Path) -> None:
    recipe_path = write_recipe(tmp_path, 'variants.append(Variant("bare"))\n')
    code, out = run(str(recipe_path), "--variant", "default", "--variant", "bare")
    assert code == 0
    assert "info profile-empty [bare]" in out
    assert run(str(recipe_path), "--variant", "default")[1] == "no findings\n"


# bake pre-flight


def test_bake_refuses_error_level_findings(tmp_path: Path) -> None:
    subject = recipe(File("/etc/../../escape", "x"))
    with pytest.raises(LintError) as excinfo:
        bake(subject, locked=lock(subject), backend=Backend("inprocess"), out=tmp_path / "out")
    assert excinfo.value.code == "E_LINT"
    assert "1 error-level diagnostics" in str(excinfo.value)
    assert excinfo.value.context["codes"] == "file-path-relative"


def test_clean_recipe_bakes(tmp_path: Path) -> None:
    subject = recipe()
    artifacts = bake(
        subject, locked=lock(subject), backend=Backend("inprocess"), out=tmp_path / "out"
    )
    assert [a.variant for a in artifacts] == ["default"]
