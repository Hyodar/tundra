"""``SshAccess(login=Login(...))``: a non-root login into a forced command or a shell, with
fixed sudo rules, keys baked or delivered after attestation, its probe row and its lint."""

from __future__ import annotations

import pytest

from tundravm import Image, Profile, Service
from tundravm.components import Login, SshAccess, Sudo
from tundravm.errors import ValidationError
from tundravm.secrets import Secret
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
STATUS = "/usr/bin/systemctl status app"
CONFIG = "/etc/ssh/sshd_config.d/60-tundravm-login-tenant.conf"


def _image(*logins: Login) -> Image:
    return Image(
        "x",
        services=[Service("app", ["/usr/bin/app"])],
        components=[SshAccess(login=list(logins))],
        profiles={"dev": Profile(production=False)},
    )


def _postinst(planned: object) -> str:
    entries = planned.tree.entries  # type: ignore[attr-defined]
    return "".join((e.content or b"").decode() for e in entries if "mkosi.postinst.d" in e.path)


@pytest.mark.parametrize(
    ("login", "files", "absent"),
    [
        (
            Login("tenant", keys=[KEY], command=STATUS),
            {
                "/etc/ssh/authorized_keys.d/tenant": f'restrict,command="{STATUS}" {KEY}\n',
                CONFIG: f"    PermitTTY no\n    ForceCommand {STATUS}\n",
            },
            ["/etc/sudoers.d/tenant"],
        ),
        (
            Login("tenant", keys=[KEY], shell="/usr/bin/menu", home=True),
            {
                "/etc/ssh/authorized_keys.d/tenant": f"restrict,pty {KEY}\n",
                CONFIG: "    AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u\n"
                "    DisableForwarding yes\n",
            },
            [],
        ),
        (
            Login(
                "tenant",
                keys=[KEY],
                command=STATUS,
                sudo=[Sudo("/usr/bin/systemctl restart app"), Sudo("/usr/bin/toggle")],
            ),
            {
                "/etc/sudoers.d/tenant": "tenant ALL=(root) NOPASSWD: /usr/bin/systemctl "
                'restart app\ntenant ALL=(root) NOPASSWD: /usr/bin/toggle ""\n',
            },
            [],
        ),
        (
            Login("tenant", keys=Secret("tenant-keys", required=False), command=STATUS),
            {CONFIG: f"    ForceCommand {STATUS}\n"},
            ["/etc/ssh/authorized_keys.d/tenant"],
        ),
    ],
)
def test_a_login_lowers_to_its_account_keys_and_rules(
    login: Login, files: dict[str, str], absent: list[str]
) -> None:
    planned = plan(_image(login), "production")
    for path, text in files.items():
        assert text in planned.file(path).text, path
    for path in absent:
        assert path not in planned.files, path
    user = planned.users["tenant"]
    assert (user.shell, user.home) == (
        login.shell or "/bin/sh",
        "/home/tenant" if login.home else None,
    )
    steps = {step.name: step.script for step in planned.init_steps}
    assert steps.get("owners-login-tenant") == (
        "chown -R tenant:tenant /home/tenant\n" if login.home else None
    )
    if login.sudo:
        assert planned.file("/etc/sudoers.d/tenant").mode == 0o440
        assert "mkosi-chroot visudo -c -f /etc/sudoers.d/tenant" in _postinst(planned)
        assert "sudo" in planned.packages


def test_delivered_keys_land_in_the_logins_authorized_keys_file() -> None:
    keys = Secret("tenant-keys", required=False)
    planned = plan(_image(Login("tenant", keys=keys, command=STATUS)), "production")
    config = planned.file("/etc/tdx/secrets.yaml").text
    assert config.count('name: "tenant-keys"') == 2
    assert '"/etc/ssh/authorized_keys.d/tenant"' in config
    assert "-/etc/ssh/authorized_keys.d" in planned.unit("tundravm-endpoint.service").text
    assert 'mkdir -p "$BUILDROOT/etc/ssh/authorized_keys.d"' in _postinst(planned)


def test_the_dev_probe_checks_each_login() -> None:
    login = Login("tenant", keys=[KEY], command=STATUS, sudo=[Sudo("/usr/bin/toggle")])
    probe = plan(_image(login), "dev").file("/usr/lib/tundravm/probe").text
    assert "check=login:tenant\n" in probe
    assert (
        f"probe_login tenant /etc/sudoers.d/tenant 'restrict,command=\"{STATUS}\"' '{STATUS}'"
        in probe
    )


@pytest.mark.parametrize(
    ("login", "production", "found"),
    [
        (Login("tenant", keys=[KEY], shell="/bin/bash"), True, True),
        (Login("tenant", keys=[KEY], shell="/bin/bash", command=STATUS), True, False),
        (Login("tenant", keys=[KEY], command=STATUS), True, False),
        (Login("tenant", keys=[KEY], shell="/bin/bash"), False, False),
    ],
)
def test_a_production_shell_login_without_a_command_warns(
    login: Login, production: bool, found: bool
) -> None:
    image = _image(login)
    codes = {d.code for d in plan(image, "production" if production else "dev").lint()}
    assert ("ssh-login-shell" in codes) is found


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Login("root", keys=[KEY], command=STATUS), "root is not a login"),
        (lambda: Login("Tenant", keys=[KEY], command=STATUS), "not an account name"),
        (lambda: Login("tenant", command=STATUS), "no key to accept"),
        (lambda: Login("tenant", keys=KEY, command=STATUS), "takes a list"),
        (lambda: Login("tenant", keys=["nope"], command=STATUS), "not one OpenSSH"),
        (lambda: Login("tenant", keys=[KEY]), "runs nothing"),
        (lambda: Login("tenant", keys=[KEY], command="app"), "absolute path"),
        (lambda: Login("tenant", keys=[KEY], command='/bin/echo "x"'), "absolute path"),
        (lambda: Login("tenant", keys=[KEY], shell="/bin/sh -r"), "not a shell"),
        (lambda: Login("tenant", keys=[KEY], shell="/bin/sh", sudo=Sudo("/bin/x")), "list"),  # type: ignore[arg-type]
        (lambda: Sudo("systemctl restart app"), "not an absolute path"),
        (lambda: Sudo("/usr/bin/journalctl -u *"), "wildcard"),
        (lambda: Sudo("/usr/bin/toggle", nopasswd=False), "does not have"),
        (lambda: SshAccess(login=["tenant"]), "takes a Login"),  # type: ignore[list-item]
        (
            lambda: SshAccess(
                login=[Login("a", keys=[KEY], command=STATUS), Login("a", keys=[KEY], shell="/x")]
            ).compose(),
            "two logins are called a",
        ),
    ],
)
def test_a_bad_login_is_refused(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        result = make()  # type: ignore[operator]
        list(result) if not isinstance(result, (Login, Sudo, SshAccess)) else None
