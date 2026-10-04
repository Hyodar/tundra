"""Profile handles returned by ``Image.profile()``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm import Image, Profile, ValidationError
from tundravm.profile import declaration_methods


def test_context_manager_still_scopes_declarations() -> None:
    img = Image()
    with img.profile("dev") as entered:
        assert entered is img
        img.install("htop")
    img.install("curl")

    assert img.state.profiles["dev"].packages == {"htop"}
    assert "htop" not in img.state.profiles["default"].packages
    assert "curl" in img.state.profiles["default"].packages


def test_context_manager_is_reentrant_and_restores() -> None:
    img = Image()
    dev = img.profile("dev")
    with dev:
        with img.profile("azure"):
            img.install("walinuxagent")
        with dev:
            img.install("strace")
        img.install("htop")
    img.install("curl")

    assert img.state.profiles["dev"].packages == {"strace", "htop"}
    assert img.state.profiles["azure"].packages == {"walinuxagent"}
    assert "curl" in img.state.profiles["default"].packages


def test_bound_declarations_land_in_that_profile_only() -> None:
    img = Image()
    azure = img.profile("azure")
    azure.install("walinuxagent")
    azure.file("/etc/azure.conf", content="x=1\n")
    azure.service("agent", command="/usr/bin/agent")
    azure.output_targets("azure")

    state = img.state.profiles["azure"]
    default = img.state.profiles["default"]
    assert state.packages == {"walinuxagent"}
    assert [f.path for f in state.files] == ["/etc/azure.conf"]
    assert [s.name for s in state.services] == ["agent"]
    assert state.output_targets == ("azure",)
    assert "walinuxagent" not in default.packages
    assert default.files == []
    assert default.services == []
    assert img._active_profiles == ("default",)


def test_chaining_returns_the_profile() -> None:
    img = Image()
    gcp = img.profile("gcp")

    result = gcp.install("google-guest-agent").run("echo hi").output_targets("gcp")

    assert result is gcp
    assert isinstance(result, Profile)
    assert img.state.profiles["gcp"].packages == {"google-guest-agent"}


def test_getattr_falls_back_to_declaration_methods() -> None:
    img = Image()
    dev = img.profile("dev")

    assert dev.backports(mirror="http://mirror.example") is dev
    assert dev.build_install("gcc") is dev

    dev_hooks = img.state.profiles["dev"].phases["sync"]
    assert any("http://mirror.example" in cmd.argv[0] for cmd in dev_hooks)
    assert "sync" not in img.state.profiles["default"].phases
    assert img.state.profiles["dev"].build_packages == {"gcc"}


def test_getattr_rejects_non_declaration_attributes() -> None:
    dev = Image().profile("dev")
    with pytest.raises(AttributeError, match="profile.image.mkosi"):
        _ = dev.mkosi
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = dev.nope


def test_declaration_methods_are_the_self_returning_public_methods() -> None:
    names = declaration_methods(Image)
    assert {"install", "file", "directory", "service", "backports", "apply", "ssh"} <= names
    assert names.isdisjoint({"compile", "bake", "explain", "profile", "profiles", "lock"})


def test_profile_names_and_repr() -> None:
    img = Image()
    img.profile("gcp")
    img.profile("azure")

    assert img.profile_names == ("azure", "default", "gcp")
    assert repr(img.profile("gcp")) == "Profile('gcp')"
    assert img.profile("gcp").name == "gcp"
    with pytest.raises(ValidationError):
        img.profile("")


def test_profile_scoped_explain_summary_and_state() -> None:
    img = Image()
    img.install("curl")
    dev = img.profile("dev").install("htop")

    assert dev.explain()["profile"] == "dev"
    assert dev.explain()["packages"] == ["curl", "htop"]
    assert "htop" in dev.summary()
    assert dev.state is img.state.profiles["dev"]


def test_profile_scoped_check_filters_to_that_profile() -> None:
    img = Image()
    img.service("svc", command="/bin/true", user="ghost")
    dev = img.profile("dev").service("dev-svc", command="/bin/true", user="phantom")

    codes = {(d.profile, d.subject) for d in dev.check() if d.code == "service-user-missing"}
    assert codes == {("dev", "dev-svc"), ("dev", "svc")}
    assert all(d.profile == "dev" for d in dev.check())


def test_profile_scoped_compile_emits_only_that_profile(tmp_path: Path) -> None:
    img = Image()
    img.profile("dev").install("htop")
    img.profile("prod").install("curl")

    result = img.profile("dev").compile(tmp_path / "tree")

    assert result.profiles == ("dev",)
    assert (tmp_path / "tree" / "dev").is_dir()
    assert not (tmp_path / "tree" / "prod").exists()
    assert img._active_profiles == ("default",)
