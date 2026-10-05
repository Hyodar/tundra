"""``secret-in-file`` and ``secret-in-env``: credentials baked into the image."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lint.test_check_rules import recipe, report
from tundravm import Directory, File, Fragment, Service, Template, Unit, Variant, compile
from tundravm.declarative import lock

# Built at runtime so this file does not trip secret scanners itself.
PEM = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1x2y3z4w5v6u7t8s9r0q\n-----END RSA PRIVATE KEY-----\n"
)
AWS = "AKIA" + "Q3EGRT5YHU8JKD2N"
GITHUB = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z5A"
TOKEN = "s3cR3tV4lu3Xq9Zp2Lm7"


def _secret_codes(
    *items: object, variants: tuple[Variant, ...] | None = None
) -> list[tuple[str, str]]:
    subject = recipe(*items) if variants is None else recipe(*items, variants=variants)  # type: ignore[arg-type]
    return [(d.code, d.subject or "") for d in report(subject) if d.code.startswith("secret-")]


@pytest.mark.parametrize(
    ("path", "content", "kind"),
    [
        ("/root/.ssh/id_rsa", PEM, "a private key"),
        ("/usr/local/bin/sync", f"#!/bin/sh\nexport KEY={AWS}\n", "an AWS access key id"),
        ("/opt/app/README", f"clone with {GITHUB}\n", "a GitHub token"),
        ("/etc/app/app.conf", f"listen = 8080\napi_token = {TOKEN}\n", "a literal value"),
        ("/srv/app/config.yaml", f'db:\n  password: "{TOKEN}"\n', "a literal value"),
        ("/etc/app/settings.json", f'{{"client_secret": "{TOKEN}"}}', "a literal value"),
        ("/srv/secrets.yaml", "db:\n  password: hunter2hunter2\n", "a literal secrets file"),
    ],
)
def test_secret_in_file_finds_credentials(path: str, content: str, kind: str) -> None:
    [diag] = [d for d in report(recipe(File(path, content))) if d.code == "secret-in-file"]
    assert (diag.level, diag.subject) == ("error", path)
    assert kind in diag.message
    assert "Secrets(" in (diag.hint or "") and "allow_secret=True" in (diag.hint or "")
    for value in (AWS, GITHUB, TOKEN, "hunter2hunter2", "MIIEowIBAAKCAQEA"):
        assert value not in diag.message


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("/etc/ssl/certs/ca.pem", "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n"),
        ("/etc/ssh/key.pub", "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0B\n"),
        ("/usr/local/bin/check", 'grep -q "-----BEGIN OPENSSH PRIVATE KEY-----" "$1"\n'),
        ("/etc/app/aws.conf", "key_id = AKIAIOSFODNN7EXAMPLE\n"),
        ("/etc/app/app.conf", "password = changeme-changeme-123\n"),
        ("/etc/app/app.conf", "# api_token = " + TOKEN + "\n"),
        ("/etc/app/app.conf", "token_file = /run/secrets/app-token-file-path\n"),
        ("/etc/app/app.conf", "password = ${APP_PASSWORD_FROM_ENVIRONMENT}\n"),
        ("/usr/share/doc/app/notes.txt", f"api_token = {TOKEN}\n"),
        ("/etc/app/app.conf", "secret_name = database-credentials-name\n"),
        ("/etc/tdx/secrets.yaml", 'ssh:\n  strategy: "webserver"\n  key_path: "/etc/root_key"\n'),
        ("/srv/secrets.yaml", "db:\n  password: !vault |\n    $ANSIBLE_VAULT;1.1\n"),
    ],
)
def test_secret_in_file_ignores_lookalikes(path: str, content: str) -> None:
    assert _secret_codes(File(path, content)) == []


def test_secret_in_file_covers_templates_and_directories(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "id_ed25519").write_text(PEM, encoding="utf-8")
    template = Template("/etc/app/env.conf", "token={value}\n", variables=(("value", TOKEN),))
    found = _secret_codes(template, Directory("/etc/keys", tmp_path / "conf"))
    assert found == [
        ("secret-in-file", "/etc/app/env.conf"),
        ("secret-in-file", "/etc/keys/id_ed25519"),
    ]


def test_allow_secret_silences_each_declaration(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "id_ed25519").write_text(PEM, encoding="utf-8")
    template = Template(
        "/etc/app/env.conf", "token={v}\n", variables=(("v", TOKEN),), allow_secret=True
    )
    assert (
        _secret_codes(
            File("/root/.ssh/id_rsa", PEM, allow_secret=True),
            template,
            Directory("/etc/keys", tmp_path / "conf", allow_secret=True),
            Service("api", "/usr/bin/api", env=(("API_TOKEN", TOKEN),), allow_secret=True),
            Unit("w.service", f"[Service]\nEnvironment=GH={GITHUB}\n", allow_secret=True),
        )
        == []
    )


def test_allow_secret_is_per_variant() -> None:
    dev = Variant("dev", add=Fragment("dev", items=(File("/etc/dev.conf", f"token={TOKEN}\n"),)))
    prod = Variant(
        "prod",
        add=Fragment("prod", items=(File("/etc/dev.conf", f"token={TOKEN}\n", allow_secret=True),)),
    )
    subject = recipe(variants=(Variant("default", target="qemu"), dev, prod))
    found = [(d.code, d.profile) for d in report(subject) if d.code.startswith("secret-")]
    assert found == [("secret-in-file", "dev")]


def test_allow_secret_leaves_the_digest_alone() -> None:
    plain = recipe(File("/etc/motd.d/x", "x\n"))
    allowed = recipe(File("/etc/motd.d/x", "x\n", allow_secret=True))
    assert lock(plain).recipe_digest == lock(allowed).recipe_digest
    assert compile(plain).digest == compile(allowed).digest


def test_secret_in_env_for_services_and_units() -> None:
    found = _secret_codes(
        Service("api", "/usr/bin/api", env=(("API_TOKEN", TOKEN), ("PORT", "8080"))),
        Service("aws", "/usr/bin/aws", env=(("KEY", AWS),)),
        Unit("w.service", f'[Service]\nEnvironment="DB_PASSWORD={TOKEN}" "MODE=prod"\n'),
        Unit("x.service", f"[Service]\nExecStart=/usr/bin/x --token {GITHUB}\n"),
    )
    assert sorted(found) == [
        ("secret-in-env", "/usr/lib/systemd/system/w.service"),
        ("secret-in-env", "/usr/lib/systemd/system/x.service"),
        ("secret-in-env", "api.service"),
        ("secret-in-env", "aws.service"),
    ]


def test_secret_in_env_ignores_references_and_plain_settings() -> None:
    found = _secret_codes(
        Service(
            "api",
            "/usr/bin/api",
            env=(
                ("API_TOKEN_FILE", "/run/secrets/api-token"),
                ("DB_PASSWORD", "changeme"),
                ("LOG_LEVEL", "debug-verbose-mode-on"),
            ),
        ),
        Unit(
            "w.service",
            "[Service]\nEnvironment=TOKEN_PATH=/run/secrets/w\nEnvironmentFile=-/run/secrets/w.env\n",
        ),
    )
    assert found == []


def test_bool_flag_is_validated() -> None:
    with pytest.raises(Exception, match="allow_secret must be True or False"):
        File("/etc/x", "x", allow_secret="yes")  # type: ignore[arg-type]
