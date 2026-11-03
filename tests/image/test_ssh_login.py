"""``SshAccess(login=Login(...))``: a non-root login into a command menu or a shell, keys
baked, delivered or the owner's, its root entries as exact sudoers lines, its probe rows and
its lint."""

from __future__ import annotations

import json

import pytest

from tundravm import Image, Profile, Service
from tundravm.components import Login, SshAccess, Sudo
from tundravm.errors import ValidationError
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
STATUS = {"status": ["/usr/bin/systemctl", "status", "app"]}
CONFIG = "/etc/ssh/sshd_config.d/60-tundravm-login-tenant.conf"
SSHD = "/etc/ssh/sshd_config.d/50-tundravm.conf"
MENU = "/etc/tundravm/logins/tenant.json"
LOGIN_MENU = "/usr/bin/login-menu"
DATA = Volume("data", unlock=OperatorKey())


def _image(*logins: Login, from_delivery: bool = False) -> Image:
    return Image(
        "x",
        services=[Service("app", ["/usr/bin/app"], state=DATA.directory("app"))],
        components=[SshAccess(login=list(logins), from_delivery=from_delivery)],
        profiles={"dev": Profile(production=False)},
    )


def _postinst(planned: object) -> str:
    entries = planned.tree.entries  # type: ignore[attr-defined]
    return "".join((e.content or b"").decode() for e in entries if "mkosi.postinst.d" in e.path)


@pytest.mark.parametrize(
    ("login", "files", "absent", "shell"),
    [
        (
            Login("tenant", keys=[KEY], commands=STATUS),
            {
                "/etc/ssh/authorized_keys.d/tenant": f'restrict,command="{LOGIN_MENU}" {KEY}\n',
                CONFIG: "    PermitTTY no\n    ForceCommand /usr/bin/login-menu\n",
                MENU: '"word": "/usr/bin/systemctl"',
            },
            ["/etc/sudoers.d/tenant"],
            "/usr/bin/login-menu",
        ),
        (
            Login("tenant", keys=[KEY], shell="/usr/bin/menu", home=True),
            {
                "/etc/ssh/authorized_keys.d/tenant": f"restrict,pty {KEY}\n",
                CONFIG: "    AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u\n"
                "    DisableForwarding yes\n",
            },
            [MENU, "/etc/sudoers.d/tenant"],
            "/usr/bin/menu",
        ),
        (
            Login("tenant", owner=True, commands={"web": Sudo(["/usr/bin/systemctl", "restart"])}),
            {"/etc/sudoers.d/tenant": "tenant ALL=(root) NOPASSWD: /usr/bin/systemctl restart\n"},
            ["/etc/ssh/authorized_keys.d/tenant"],
            "/usr/bin/login-menu",
        ),
    ],
)
def test_a_login_lowers_to_its_account_keys_menu_and_rules(
    login: Login, files: dict[str, str], absent: list[str], shell: str
) -> None:
    planned = plan(_image(login), "production")
    for path, text in files.items():
        assert text in planned.file(path).text, path
    for path in absent:
        assert path not in planned.files, path
    user = planned.users["tenant"]
    assert (user.shell, user.home) == (shell, "/home/tenant" if login.home else None)
    if MENU in planned.files:
        assert planned.file(MENU).mode == 0o444
        assert "login-menu" in planned.builds
    if login.sudoers_file:
        assert planned.file("/etc/sudoers.d/tenant").mode == 0o440
        assert "mkosi-chroot visudo -c -f /etc/sudoers.d/tenant" in _postinst(planned)


@pytest.mark.parametrize(
    ("login", "from_delivery", "ssh", "root"),
    [
        (Login("tenant", owner=True, commands=STATUS), False, 'ssh:\n  login: "tenant"\n', "no"),
        (Login("tenant", keys=[KEY], commands=STATUS), False, None, "no"),
        (Login("tenant", keys=[KEY], commands=STATUS), True, 'ssh:\n  dir: "/root/.ssh"\n', None),
    ],
)
def test_the_owners_key_goes_to_the_owner_login_and_root_takes_none_without_asking(
    login: Login, from_delivery: bool, ssh: str | None, root: str | None
) -> None:
    planned = plan(_image(login, from_delivery=from_delivery), "production")
    config = planned.file("/etc/tdx/secrets.yaml").text
    assert (ssh in config) if ssh else ("ssh:" not in config)
    assert f"PermitRootLogin {root or 'prohibit-password'}\n" in planned.file(SSHD).text
    if login.owner:
        assert "-/etc/ssh/authorized_keys.d" in planned.unit("tundravm-endpoint.service").text


@pytest.mark.parametrize(
    ("argv", "rules"),
    [
        (["/usr/bin/toggle"], ['/usr/bin/toggle ""']),
        (["/usr/bin/x", "a=b,c"], ["/usr/bin/x a\\=b\\,c"]),
        (
            ["/usr/bin/mode-switch", "to", "{mode:choice:production|maintenance}"],
            ["/usr/bin/mode-switch to production", "/usr/bin/mode-switch to maintenance"],
        ),
        (
            ["/usr/bin/journalctl", "-u", "a.service", "-n", "{lines:int:1-1000}"],
            ["/usr/bin/journalctl ^-u a[.]service -n [0-9][0-9]?[0-9]?[0-9]?$"],
        ),
    ],
)
def test_a_root_entry_is_exact_sudoers_lines(argv: list[str], rules: list[str]) -> None:
    login = Login("tenant", keys=[KEY], commands={"run": Sudo(argv)})
    assert login.sudo_rules() == tuple(f"tenant ALL=(root) NOPASSWD: {r}" for r in rules)


def test_the_menu_types_each_argument() -> None:
    login = Login("tenant", keys=[KEY], commands={"logs": ["/bin/x", "{n:int}", "{m:choice:a|b}"]})
    assert json.loads(login.menu())["entries"][0]["argv"] == [
        {"word": "/bin/x"},
        {"arg": "n", "int": [0, 65535]},
        {"arg": "m", "choice": ["a", "b"]},
    ]


def test_the_dev_probe_checks_each_login_its_menu_and_root() -> None:
    login = Login("tenant", owner=True, commands={"logs": Sudo(["/bin/x", "{n:int:1-9}"])})
    probe = plan(_image(login), "dev").file("/usr/lib/tundravm/probe").text
    assert "probe_login tenant /etc/sudoers.d/tenant '-' '/usr/bin/login-menu'\n" in probe
    assert f"probe_menu tenant {MENU} /etc/sudoers.d/tenant 'logs 10' logs\n" in probe
    assert "check=ssh:root\nprobe_no_root\n" in probe


@pytest.mark.parametrize(
    ("shell", "production", "found"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_a_production_shell_login_warns(shell: bool, production: bool, found: bool) -> None:
    login = (
        Login("tenant", keys=[KEY], shell="/bin/bash")
        if shell
        else Login("tenant", keys=[KEY], commands=STATUS)
    )
    codes = {d.code for d in plan(_image(login), "production" if production else "dev").lint()}
    assert ("ssh-login-shell" in codes) is found


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Login("root", keys=[KEY], commands=STATUS), "root is not a login"),
        (lambda: Login("Tenant", keys=[KEY], commands=STATUS), "not an account name"),
        (lambda: Login("tenant", commands=STATUS), "no key to accept"),
        (lambda: Login("tenant", keys=KEY, commands=STATUS), "takes a list"),
        (lambda: Login("tenant", keys=["nope"], commands=STATUS), "not one OpenSSH"),
        (lambda: Login("tenant", owner=True, keys=[KEY], commands=STATUS), "names others"),
        (lambda: Login("tenant", owner=True, keys=Secret("k"), commands=STATUS), "names another"),
        (lambda: Login("tenant", keys=[KEY]), "runs nothing"),
        (lambda: Login("tenant", keys=[KEY], shell="/bin/sh", commands=STATUS), "both"),
        (lambda: Login("tenant", keys=[KEY], shell="/bin/sh -r"), "not one absolute path"),
        (lambda: Login("tenant", keys=[KEY], commands={"x": "/bin/x"}), "not an argv"),
        (lambda: Login("tenant", keys=[KEY], commands={"X": ["/bin/x"]}), "not a command word"),
        (lambda: Login("tenant", keys=[KEY], commands={"x": ["x"]}), "not an absolute path"),
        (lambda: Login("tenant", keys=[KEY], commands={"x": ["/x", "--n={n:int}"]}), "whole word"),
        (lambda: Login("tenant", keys=[KEY], commands={"x": ["/x", "{n:int:9-1}"]}), "bounds"),
        (lambda: Login("tenant", keys=[KEY], commands={"x": ["/x", "{n:choice:-f}"]}), "offers"),
        (
            lambda: Login("tenant", keys=[KEY], commands={"x": ["/x", "{n:int}", "{n:int}"]}),
            "twice",
        ),
        (lambda: Login("t", keys=[KEY], commands={"x": Sudo(["/x", "*"])}), "wildcard"),
        (lambda: Login("t", keys=[KEY], commands={"x": Sudo(["/x", "a b"])}), "a space"),
        (lambda: Login("t", keys=[KEY], commands={"x": Sudo(["/x", "a=b", "{n:int}"])}), "pattern"),
        (lambda: Sudo("/usr/bin/toggle"), "one string"),
        (lambda: SshAccess(login=["tenant"]), "takes a Login"),  # type: ignore[list-item]
        (
            lambda: SshAccess(
                login=[
                    Login("a", owner=True, commands=STATUS),
                    Login("b", owner=True, commands=STATUS),
                ]
            ).compose(),
            "a and b both say owner=True",
        ),
        (
            lambda: SshAccess(
                login=[Login("a", owner=True, commands=STATUS)], from_delivery=True
            ).compose(),
            "gives it to root",
        ),
    ],
)
def test_a_bad_login_is_refused(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        result = make()  # type: ignore[operator]
        list(result) if not isinstance(result, (Login, Sudo, SshAccess)) else None


def test_an_owner_login_needs_the_attested_endpoint() -> None:
    image = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"])],
        components=[SshAccess(login=Login("tenant", owner=True, commands=STATUS))],
    )
    with pytest.raises(ValidationError, match="runs no attested endpoint"):
        plan(image, "production").files  # noqa: B018
