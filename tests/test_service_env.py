"""Service environment, working directory, description and ExecStartPre."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm import Image, ValidationError
from tundravm.lockfile import recipe_digest

LEGACY_SERVICE_KEYS = {
    "name",
    "command",
    "user",
    "after",
    "requires",
    "wants",
    "restart",
    "enabled",
    "security_profile",
}


def _unit(img: Image, tmp_path: Path, name: str) -> str:
    img.compile(tmp_path / "tree")
    unit = tmp_path / "tree" / "default" / "mkosi.extra" / "usr/lib/systemd/system" / name
    return unit.read_text(encoding="utf-8")


def _digest(img: Image) -> str:
    return recipe_digest(img._recipe_payload(profile_names=("default",)))


def test_unit_renders_env_workdir_env_file_and_pre(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.service(
        "prover",
        command=["/usr/bin/prover", "--port", "8080"],
        description="Surge prover",
        user="prover",
        working_dir="/var/lib/prover",
        env={"RUST_LOG": "info", "OPTS": "--a --b", "QUOTE": 'say "hi"'},
        env_file="/etc/prover/env",
        exec_start_pre=["/usr/bin/mkdir -p /var/lib/prover", "/usr/bin/prover --check"],
        restart="always",
    )

    assert _unit(img, tmp_path, "prover.service") == (
        "[Unit]\n"
        "Description=Surge prover\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStartPre=/usr/bin/mkdir -p /var/lib/prover\n"
        "ExecStartPre=/usr/bin/prover --check\n"
        "ExecStart=/usr/bin/prover --port 8080\n"
        "User=prover\n"
        "WorkingDirectory=/var/lib/prover\n"
        "EnvironmentFile=/etc/prover/env\n"
        'Environment="OPTS=--a --b"\n'
        'Environment="QUOTE=say \\"hi\\""\n'
        "Environment=RUST_LOG=info\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=minimal.target\n"
    )


def test_plain_service_unit_is_unchanged(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.service("plain", command="/usr/bin/plain", user="nobody")

    assert _unit(img, tmp_path, "plain.service") == (
        "[Unit]\n"
        "Description=plain\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/usr/bin/plain\n"
        "User=nobody\n"
        "\n"
        "[Install]\n"
        "WantedBy=minimal.target\n"
    )


def test_digest_unchanged_for_service_without_new_fields() -> None:
    img = Image(reproducible=False)
    img.service("plain", command="/usr/bin/plain")
    explicit_empty = Image(reproducible=False)
    explicit_empty.service(
        "plain", command="/usr/bin/plain", env={}, exec_start_pre=(), description=""
    )

    payload = img._recipe_payload(profile_names=("default",))
    services = payload["profiles"]["default"]["services"]  # type: ignore[index]
    assert set(services[0]) == LEGACY_SERVICE_KEYS
    assert _digest(img) == _digest(explicit_empty)


def test_digest_changes_when_env_is_set() -> None:
    plain = Image(reproducible=False)
    plain.service("svc", command="/usr/bin/svc")
    with_env = Image(reproducible=False)
    with_env.service("svc", command="/usr/bin/svc", env={"A": "1"})
    other_env = Image(reproducible=False)
    other_env.service("svc", command="/usr/bin/svc", env={"A": "2"})

    assert len({_digest(plain), _digest(with_env), _digest(other_env)}) == 3


def test_explain_shows_env_and_working_dir_only_when_set() -> None:
    img = Image(reproducible=False)
    img.service("plain", command="/usr/bin/plain")
    img.service("svc", command="/usr/bin/svc", env={"B": "2", "A": "1"}, working_dir="/srv")

    services = {s["name"]: s for s in img.explain()["services"]}  # type: ignore[attr-defined]
    assert "env" not in services["plain"]
    assert services["svc"]["env"] == {"A": "1", "B": "2"}
    assert services["svc"]["working_dir"] == "/srv"
    assert "env=A,B" in img.summary()
    assert "cwd=/srv" in img.summary()


def test_init_injection_keeps_new_fields(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.add_init_script("echo init")
    img.service("svc", command="/usr/bin/svc", env={"A": "1"}, working_dir="/srv")

    unit = _unit(img, tmp_path, "svc.service")
    assert "After=runtime-init.service" in unit
    assert "Environment=A=1" in unit
    assert "WorkingDirectory=/srv" in unit


def test_rejects_bad_env() -> None:
    with pytest.raises(ValidationError, match="Invalid environment variable name"):
        Image().service("svc", command="/bin/true", env={"1BAD": "x"})
    with pytest.raises(ValidationError, match="newline"):
        Image().service("svc", command="/bin/true", env={"A": "x\ny"})
