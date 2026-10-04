"""Non-default profiles extend the default profile unless declared with extends=None."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tundravm import Image, MkosiOptions
from tundravm.cli import EXIT_OK, main
from tundravm.errors import ValidationError
from tundravm.lockfile import recipe_digest
from tundravm.modules import DevTools
from tundravm.platforms import AzurePlatform

QEMU_BASIC = Path(__file__).resolve().parent.parent / "examples" / "qemu_basic.py"
QEMU_BASIC_DIGEST = "571668210b9086615b19fde56a4b86cf0aa65f2ec480b2497a42641739d15c8e"


def _image() -> Image:
    img = Image(reproducible=False)
    img.install("curl")
    img.user("app", system=True)
    img.file("/etc/motd", content="default\n")
    img.file("/etc/app.conf", content="a=1\n")
    img.service("app", command="/usr/bin/app", user="app")
    img.shell("echo default-hook", phase="postinst")
    with img.profile("azure"):
        img.install("waagent")
        img.file("/etc/motd", content="azure\n")
        img.service("agent", command="/usr/bin/agent")
        img.shell("echo azure-hook", phase="postinst")
    return img


def _digest(img: Image, *profiles: str) -> str:
    return recipe_digest(img._recipe_payload(profile_names=profiles))


def test_new_profiles_extend_the_default_profile() -> None:
    img = _image()
    assert img.state.profiles["default"].extends is None
    assert img.state.profiles["azure"].extends == "default"
    assert img.state.effective_profile("default") is img.state.profiles["default"]


def test_effective_profile_merges_over_default() -> None:
    img = _image()
    azure = img.state.effective_profile("azure")

    assert azure.packages == {"curl", "waagent"}
    assert [u.name for u in azure.users] == ["app"]
    assert [s.name for s in azure.services] == ["app", "agent"]
    assert [h.command.argv for h in azure.hooks] == [("echo default-hook",), ("echo azure-hook",)]
    assert [c.argv for c in azure.phases["postinst"]] == [
        ("echo default-hook",),
        ("echo azure-hook",),
    ]
    assert img.state.profiles["azure"].packages == {"waagent"}


def test_profile_file_overrides_default_file() -> None:
    files = {f.path: f.content for f in _image().state.effective_profile("azure").files}
    assert files == {"/etc/app.conf": "a=1\n", "/etc/motd": "azure\n"}


def test_extends_none_is_standalone() -> None:
    img = Image(reproducible=False)
    img.install("curl")
    img.targets("qemu", "gcp")
    with img.profile("solo", extends=None):
        img.install("vim")

    solo = img.state.effective_profile("solo")
    assert img.state.profiles["solo"].extends is None
    assert solo.packages == {"vim"}
    assert solo.output_targets == ("qemu", "gcp")


def test_extends_other_profile_is_rejected() -> None:
    img = Image()
    img.profile("other")
    with pytest.raises(ValidationError, match="can only extend the default profile"):
        img.profile("child", extends="other")
    with pytest.raises(ValidationError, match="cannot extend"):
        img.profile("default", extends="default")


def test_compiled_profile_conf_holds_default_and_profile_packages(tmp_path: Path) -> None:
    img = _image()
    with img.all_profiles():
        out = img.compile(tmp_path / "mkosi").path

    conf = (out / "azure" / "mkosi.conf").read_text(encoding="utf-8")
    assert "    curl\n" in conf and "    waagent\n" in conf
    extra = out / "azure" / "mkosi.extra"
    assert (extra / "etc/motd").read_text() == "azure\n"
    assert (extra / "etc/app.conf").read_text() == "a=1\n"
    assert (extra / "usr/lib/systemd/system/app.service").is_file()
    assert "waagent" not in (out / "default" / "mkosi.conf").read_text(encoding="utf-8")


def test_native_profiles_overlay_holds_only_additions(tmp_path: Path) -> None:
    img = _image()
    img.set_mkosi(MkosiOptions(emit_mode="native_profiles"))
    with img.all_profiles():
        out = img.compile(tmp_path / "mkosi").path

    root = (out / "mkosi.conf").read_text(encoding="utf-8")
    overlay = (out / "mkosi.profiles" / "azure" / "mkosi.conf").read_text(encoding="utf-8")
    assert "    curl\n" in root and "waagent" not in root
    assert "    waagent\n" in overlay and "curl" not in overlay
    assert not (out / "mkosi.profiles" / "azure" / "mkosi.extra" / "etc/app.conf").exists()
    default_overlay = out / "mkosi.profiles" / "default"
    assert "Packages" not in (default_overlay / "mkosi.conf").read_text(encoding="utf-8")


def test_native_profiles_rejects_standalone_profile(tmp_path: Path) -> None:
    img = Image(mkosi=MkosiOptions(emit_mode="native_profiles"))
    img.profile("solo", extends=None)
    with img.all_profiles(), pytest.raises(ValidationError, match="standalone"):
        img.compile(tmp_path / "mkosi")


def test_explain_shows_extends_and_modules() -> None:
    img = Image(reproducible=False)
    img.install("curl")
    img.apply(DevTools())
    img.profile("azure").apply(AzurePlatform())

    azure = img.explain(profile="azure")
    assert azure["extends"] == "default"
    assert azure["modules"] == ["AzurePlatform"]
    assert azure["extends_modules"] == ["DevTools"]
    text = img.summary(profile="azure")
    assert "Extends: default (modules: DevTools)\n" in text
    assert "Modules (1): AzurePlatform\n" in text
    assert "Extends" not in img.summary()
    assert "Modules (1): DevTools\n" in img.summary()


def test_hook_preview_skips_comment_lines() -> None:
    img = Image(reproducible=False)
    img.shell("# Set root password\n\nchpasswd <<< root:x\n", phase="postinst")
    assert img.explain()["hooks"] == {"postinst": ["chpasswd <<< root:x"]}


def test_linter_accepts_default_user_for_profile_service() -> None:
    img = Image(reproducible=False)
    img.user("app", system=True)
    with img.profile("dev"):
        img.service("worker", command="/usr/bin/worker", user="app")
    codes = [d.code for d in img.check(profiles=["dev"])]
    assert "service-user-missing" not in codes


def test_digest_changes_when_extends_toggles() -> None:
    img = _image()
    inheriting = _digest(img, "azure")
    payload = img._recipe_payload(profile_names=("default", "azure"))
    profiles = payload["profiles"]
    assert isinstance(profiles, dict)
    assert profiles["azure"]["extends"] == "default"
    assert "extends" not in profiles["default"]

    img.profile("azure", extends=None)
    assert _digest(img, "azure") != inheriting
    img.profile("azure", extends="default")
    assert _digest(img, "azure") == inheriting


def test_default_only_recipe_digest_is_unchanged() -> None:
    out = io.StringIO()
    assert main(["inspect", str(QEMU_BASIC), "--json"], stdout=out) == EXIT_OK
    assert json.loads(out.getvalue())["digest"] == QEMU_BASIC_DIGEST
