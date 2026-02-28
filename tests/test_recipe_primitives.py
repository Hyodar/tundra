from pathlib import Path

import pytest

from tundravm import Image
from tundravm.errors import ValidationError


def test_recipe_primitives_are_recorded() -> None:
    image = Image(reproducible=False)
    image.repository("https://deb.example/security", name="debian-security", priority=10)
    image.file("/etc/example.conf", content="key=value\n")
    image.template(
        "/etc/app/env",
        template="A={a}\nB={b}\n",
        variables={"b": "2", "a": "1"},
    )
    image.user("app", uid=1000, gid=1000, shell="/bin/bash")
    image.service("app.service", enabled=True, wants=("network-online.target",))
    image.partition("data", size="4G", mount="/data", fs="ext4")
    image.hook("build", "echo build", after_phase="prepare")

    profile = image.state.profiles["default"]
    assert profile.repositories[0].name == "debian-security"
    assert profile.files[0].content == "key=value\n"
    assert profile.templates[0].rendered == "A=1\nB=2\n"
    assert profile.users[0].name == "app"
    assert profile.services[0].name == "app.service"
    assert profile.partitions[0].mount == "/data"
    assert profile.hooks[0].after_phase == "prepare"


def test_file_src_snapshot_is_deterministic(tmp_path: Path) -> None:
    image = Image()
    source = tmp_path / "config.txt"
    source.write_text("version=1\n", encoding="utf-8")
    image.file("/etc/config.txt", src=source)
    source.write_text("version=2\n", encoding="utf-8")

    assert image.state.profiles["default"].files[0].content == "version=1\n"


def test_invalid_phase_dependency_order_is_rejected() -> None:
    image = Image()
    with pytest.raises(ValidationError):
        image.hook("prepare", "echo wrong", after_phase="build")


def test_hook_rejects_invalid_phase_name() -> None:
    image = Image()
    with pytest.raises(ValidationError, match="Invalid phase"):
        image.hook("nonexistent", "echo bad")  # type: ignore[arg-type]


def test_run_alias_records_hook() -> None:
    image = Image(reproducible=False)
    image.run("echo hello", phase="prepare")

    profile = image.state.profiles["default"]
    assert profile.hooks[0].phase == "prepare"
    assert profile.phases["prepare"][0].argv == ("echo hello",)
