"""Tests for the recipe linter (``Image.check()`` / ``tundravm check``)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tundravm import Diagnostic, Image, ValidationError
from tundravm.backends.inprocess import InProcessBackend
from tundravm.check import render
from tundravm.cli import main
from tundravm.models import SecretSpec
from tundravm.modules.secret_delivery import SecretDelivery
from tundravm.platforms import AzurePlatform, GcpPlatform


def clean_image(tmp_path: Path | None = None) -> Image:
    img = Image(build_dir=tmp_path or Path("build"), backend=InProcessBackend())
    img.install("curl")
    img.user("app", system=True)
    img.service("app", command="/usr/bin/app", user="app")
    img.file("/etc/motd", content="hi\n")
    return img


def codes(img: Image, *, profiles: list[str] | None = None) -> list[str]:
    return [d.code for d in img.check(profiles=profiles)]


def test_clean_recipe_has_no_findings() -> None:
    assert clean_image().check() == []


# a. service-user-missing


def test_service_user_missing() -> None:
    img = clean_image()
    img.service("worker", command="/usr/bin/worker", user="svc")
    [diag] = img.check()
    assert (diag.level, diag.code, diag.subject) == ("error", "service-user-missing", "worker")
    assert "img.user('svc'" in (diag.hint or "")


def test_service_user_inherited_from_default() -> None:
    img = clean_image()
    with img.profile("dev"):
        img.install("curl")
        img.service("app", command="/usr/bin/app", user="app")
    assert img.check(profiles=["dev"]) == []


def test_service_user_not_inherited_by_standalone_profile() -> None:
    img = clean_image()
    with img.profile("dev", extends=None):
        img.install("curl")
        img.service("app", command="/usr/bin/app", user="app")
    [diag] = img.check(profiles=["dev"])
    assert diag.code == "service-user-missing"
    assert "does not inherit" in (diag.hint or "")


def test_service_user_root_base_or_hook_created_is_fine() -> None:
    img = clean_image()
    img.service("a", command="/usr/bin/a", user="root")
    img.service("b", command="/usr/bin/b", user="nobody")
    img.run("mkosi-chroot useradd --system hookuser")
    img.service("c", command="/usr/bin/c", user="hookuser")
    assert img.check() == []


# b. file-path-duplicate


def test_file_path_duplicate_with_different_content() -> None:
    img = clean_image()
    img.template("/etc/motd", template="bye {x}\n", variables={"x": 1})
    [diag] = img.check()
    assert (diag.level, diag.code, diag.subject) == ("error", "file-path-duplicate", "/etc/motd")
    assert "file, template" in diag.message


def test_identical_redeclaration_and_skeleton_override_are_fine() -> None:
    img = clean_image()
    img.file("/etc/motd", content="hi\n")
    img.skeleton("/etc/motd", content="build-time\n")
    assert img.check() == []


# c. file-path-relative


def test_file_path_relative() -> None:
    img = clean_image()
    img.file("etc/relative", content="x")
    img.skeleton("/etc/../../escape", content="x")
    diags = img.check()
    assert [(d.code, d.subject) for d in diags] == [
        ("file-path-relative", "/etc/../../escape"),
        ("file-path-relative", "etc/relative"),
    ]
    assert all(d.level == "error" for d in diags)


# d. service-command-not-shipped


def test_service_command_not_shipped() -> None:
    img = Image(backend=InProcessBackend())
    img.service("tool", command="/usr/local/bin/tool --serve")
    [diag] = img.check()
    assert (diag.level, diag.code, diag.subject) == (
        "warning",
        "service-command-not-shipped",
        "tool",
    )
    assert "no packages" in (diag.hint or "")


def test_service_command_shipped_by_file_hook_package_or_essential() -> None:
    img = Image(backend=InProcessBackend())
    img.file("/usr/local/bin/tool", content="#!/bin/sh\n", mode="0755")
    img.service("tool", command="/usr/local/bin/tool")
    img.hook("build", "cp out/builder $DESTDIR/opt/builder")
    img.service("builder", command="/opt/builder")
    img.service("shell", command="/usr/bin/bash -c true")
    assert img.check() == []
    img.service("other", command="/usr/bin/other")
    img.install("other")
    assert img.check() == []


# e. output-target-platform-mismatch


def test_output_target_without_platform() -> None:
    img = clean_image()
    with img.profile("azure"):
        img.install("curl")
        img.output_targets("azure")
    with img.profile("gcp"):
        img.install("curl")
        img.output_targets("gcp")
    diags = img.check(profiles=["azure", "gcp"])
    assert [(d.profile, d.code, d.subject) for d in diags] == [
        ("azure", "output-target-platform-mismatch", "azure"),
        ("gcp", "output-target-platform-mismatch", "gcp"),
    ]


def test_platform_applied_or_guest_agent_is_fine() -> None:
    img = clean_image()
    with img.profile("azure"):
        AzurePlatform().apply(img)
    with img.profile("gcp"):
        GcpPlatform().apply(img)
    with img.profile("agent"):
        img.install("waagent")
        img.output_targets("azure")
    assert img.check(profiles=["azure", "gcp", "agent"]) == []


def test_platform_applied_but_target_overridden() -> None:
    img = clean_image()
    with img.profile("azure"):
        AzurePlatform().apply(img)
        img.output_targets("qemu")
    [diag] = img.check(profiles=["azure"])
    assert diag.code == "platform-target-missing"
    assert diag.subject == "azure"
    assert "no azure artifact" in diag.message


def test_gcp_platform_applied_but_target_overridden() -> None:
    img = clean_image()
    with img.profile("gcp"):
        GcpPlatform().apply(img)
        img.output_targets("qemu")
    assert [(d.code, d.subject) for d in img.check(profiles=["gcp"])] == [
        ("platform-target-missing", "gcp")
    ]


# f. profile-empty


def test_profile_empty() -> None:
    img = clean_image()
    with img.profile("bare"):
        pass
    [diag] = img.check(profiles=["bare"])
    assert (diag.level, diag.code, diag.profile) == ("info", "profile-empty", "bare")


def test_profile_with_content_or_default_is_not_empty() -> None:
    img = Image(backend=InProcessBackend())
    with img.profile("dev"):
        img.install("dropbear")
    assert codes(img, profiles=["default", "dev"]) == []


# g. init-priority-collision


def test_init_priority_collision() -> None:
    img = clean_image()
    img.add_init_script("echo one\n", priority=20)
    img.add_init_script("echo two\n", priority=20)
    img.add_init_script("echo three\n", priority=30)
    [diag] = img.check()
    assert (diag.code, diag.subject) == ("init-priority-collision", "priority 20")
    assert "'echo one'" in diag.message


def test_distinct_or_duplicate_init_scripts_are_fine() -> None:
    img = clean_image()
    img.add_init_script("echo one\n", priority=20)
    img.add_init_script("echo one\n", priority=20)
    img.add_init_script("echo two\n", priority=30)
    assert img.check() == []


# h. backend-missing


def test_backend_missing_reported_once_under_default() -> None:
    img = Image()
    img.install("curl")
    with img.profile("dev"):
        img.install("curl")
    diags = img.check(profiles=["default", "dev"])
    assert [(d.code, d.profile) for d in diags] == [("backend-missing", "default")]
    assert clean_image().check() == []


# i. debloat-removes-needed-unit / debloat-removes-declared-file


def test_debloat_masks_needed_units() -> None:
    img = clean_image()
    img.service("net", command="/usr/bin/net", requires=["systemd-networkd.service"])
    img.service("systemd-resolved", command="/usr/lib/systemd/systemd-resolved")
    diags = img.check()
    assert [(d.code, d.subject) for d in diags] == [
        ("debloat-removes-needed-unit", "net"),
        ("debloat-removes-needed-unit", "systemd-resolved"),
    ]
    assert "systemd_units_keep_extra" in (diags[0].hint or "")


def test_debloat_keep_extra_or_disabled_is_fine() -> None:
    img = clean_image()
    img.service("net", command="/usr/bin/net", requires=["systemd-networkd.service"])
    img.debloat(systemd_units_keep_extra=["systemd-networkd.service"])
    assert img.check() == []
    img.debloat(enabled=False)
    assert img.check() == []


def test_debloat_removes_declared_file() -> None:
    img = clean_image()
    img.file("/etc/systemd/network/10-eth.network", content="[Match]\n")
    [diag] = img.check()
    assert (diag.code, diag.subject) == (
        "debloat-removes-declared-file",
        "/etc/systemd/network/10-eth.network",
    )
    img.debloat(paths_skip=["/etc/systemd/network"])
    assert img.check() == []


# j. secret-undelivered


def test_secret_undelivered() -> None:
    img = clean_image()
    img.state.profiles["default"].secrets.append(SecretSpec(name="token"))
    diags = img.check()
    assert [(d.code, d.subject) for d in diags] == [
        ("secret-undelivered", None),
        ("secret-undelivered", "token"),
    ]


def test_secret_delivery_applied_is_fine() -> None:
    from tundravm import SecretTarget

    img = clean_image()
    delivery = SecretDelivery()
    delivery.secret("token", targets=(SecretTarget.file("/run/token"),))
    delivery.apply(img)
    assert img.check() == []


# rendering + ordering


def test_ordering_is_deterministic() -> None:
    img = Image()
    with img.profile("b"):
        img.file("rel", content="x")
    with img.profile("a"):
        pass
    img.add_init_script("echo 1\n", priority=1)
    img.add_init_script("echo 2\n", priority=1)
    diags = img.check(profiles=["b", "a", "default"])
    keys = [(d.profile, d.level, d.code) for d in diags]
    assert keys == [
        ("a", "warning", "init-priority-collision"),
        ("a", "info", "profile-empty"),
        ("b", "error", "file-path-relative"),
        ("b", "warning", "init-priority-collision"),
        ("default", "warning", "backend-missing"),
        ("default", "warning", "init-priority-collision"),
    ]
    assert diags == img.check(profiles=["default", "a", "b"])


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
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl")
img.user("app", system=True)
img.service("app", command="/usr/bin/app", user="app")
"""


def write_recipe(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "recipe.py"
    build = str(tmp_path / "build")
    path.write_text(f"BUILD_DIR = {build!r}\n{RECIPE}{extra}", encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(["check", *argv], stdout=out), out.getvalue()


def test_cli_clean_recipe(tmp_path: Path) -> None:
    code, out = run(str(write_recipe(tmp_path)))
    assert (code, out) == (0, "no findings\n")


def test_cli_error_exit_and_json(tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path, 'img.service("w", command="/usr/bin/w", user="ghost")\n')
    code, out = run(str(recipe))
    assert code == 1
    assert out.startswith("error service-user-missing [default] w:")
    assert out.rstrip().endswith("1 error, 0 warnings, 0 infos")

    code, out = run(str(recipe), "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["summary"] == {"errors": 1, "warnings": 0, "infos": 0}
    assert payload["diagnostics"][0]["code"] == "service-user-missing"


def test_cli_strict_fails_on_warnings(tmp_path: Path) -> None:
    extra = (
        'img.add_init_script("echo 1\\n", priority=5)\n'
        'img.add_init_script("echo 2\\n", priority=5)\n'
    )
    recipe = write_recipe(tmp_path, extra)
    assert run(str(recipe))[0] == 0
    assert run(str(recipe), "--strict")[0] == 1


def test_cli_profile_selection(tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path, 'with img.profile("bare"):\n    pass\n')
    code, out = run(str(recipe), "--all-profiles")
    assert code == 0
    assert "info profile-empty [bare]" in out
    assert run(str(recipe), "--profile", "default")[1] == "no findings\n"


# bake pre-flight


def test_bake_refuses_error_level_findings(tmp_path: Path) -> None:
    img = clean_image(tmp_path)
    img.file("relative/path", content="x")
    with pytest.raises(ValidationError) as excinfo:
        img.bake(tmp_path / "out")
    assert "1 error-level diagnostics" in str(excinfo.value)
    assert excinfo.value.context["codes"] == "file-path-relative"


def test_clean_recipe_bakes(tmp_path: Path, inprocess_backend: InProcessBackend) -> None:
    img = clean_image(tmp_path)
    img.backend = inprocess_backend
    result = img.bake(tmp_path / "out")
    assert "default" in result.profiles
