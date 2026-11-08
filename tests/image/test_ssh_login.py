"""``SshAccess(login=Login(...))``: a non-root login into a command menu or a shell, keys
baked, delivered or the owner's, its commands typed (``Command``, ``Int``, ``Choice``),
offered by firewall mode and by build, its ``sudo=True`` commands as exact sudoers lines,
what ``show`` says of it, its probe rows and its lint."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tundravm import Image, Profile, Service
from tundravm._describe import declared, declared_sections, render_sections
from tundravm.components.access import Choice, Command, DevAccess, Int, Login, SshAccess
from tundravm.components.network import Firewall, Mode
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
STATUS = {"status": Command("/usr/bin/systemctl", "status", "app")}
CONFIG = "/etc/ssh/sshd_config.d/60-tundravm-login-tenant.conf"
SSHD = "/etc/ssh/sshd_config.d/50-tundravm.conf"
MENU = "/etc/tundravm/logins/tenant.json"
SUDOERS = "/etc/sudoers.d/tenant"
LOGIN_MENU = "/usr/bin/login-menu"
DATA = Volume("data", unlock=OperatorKey())
MODES = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[TCP(22)])})


def _image(*logins: Login, from_delivery: bool = False, firewall: Firewall = MODES) -> Image:
    return Image(
        "x",
        services=[Service("app", ["/usr/bin/app"], state=DATA.directory("app"))],
        components=[SshAccess(login=list(logins), from_delivery=from_delivery), firewall],
        profiles={"dev": Profile(production=False), "staging": Profile()},
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
            [SUDOERS],
            "/usr/bin/login-menu",
        ),
        (
            Login("tenant", keys=[KEY], shell="/usr/bin/menu", home=True),
            {
                "/etc/ssh/authorized_keys.d/tenant": f"restrict,pty {KEY}\n",
                CONFIG: "    AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u\n"
                "    DisableForwarding yes\n",
            },
            [MENU, SUDOERS],
            "/usr/bin/menu",
        ),
        (
            Login(
                "tenant",
                owner=True,
                commands={"web": Command("/usr/bin/systemctl", "restart", sudo=True)},
            ),
            {SUDOERS: "tenant ALL=(root) NOPASSWD: /usr/bin/systemctl restart\n"},
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
    assert "systemd-user-sessions.service" in _postinst(planned)
    if MENU in planned.files:
        assert planned.file(MENU).mode == 0o444
        assert "login-menu" in planned.builds
    if login.sudoers_file:
        assert planned.file(SUDOERS).mode == 0o440
        assert f"mkosi-chroot visudo -c -f {SUDOERS}" in _postinst(planned)


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
    ("command", "rules"),
    [
        (Command("/usr/bin/toggle", sudo=True), ['/usr/bin/toggle ""']),
        (Command("/usr/bin/x", "a=b,c", sudo=True), ["/usr/bin/x a\\=b\\,c"]),
        (
            Command(
                "/usr/bin/mode-switch", "to", Choice("mode", "production", "maintenance"), sudo=True
            ),
            ["/usr/bin/mode-switch to production", "/usr/bin/mode-switch to maintenance"],
        ),
        (
            Command(
                "/usr/bin/journalctl", "-u", "a.service", "-n", Int("lines", 1, 1000), sudo=True
            ),
            ["/usr/bin/journalctl ^-u a[.]service -n [0-9][0-9]?[0-9]?[0-9]?$"],
        ),
        (Command("/usr/bin/x", modes=["maintenance"], sudo=True), ['/usr/bin/x ""']),
        (Command("/usr/bin/x"), []),
    ],
)
def test_a_root_command_is_exact_sudoers_lines(command: Command, rules: list[str]) -> None:
    login = Login("tenant", keys=[KEY], commands={"run": command, "status": STATUS["status"]})
    assert login.sudo_rules() == tuple(f"tenant ALL=(root) NOPASSWD: {r}" for r in rules)


@pytest.mark.parametrize(
    ("command", "entry"),
    [
        (
            Command("/bin/x", Int("n", 0, 65535), Choice("m", "a", "b")),
            {
                "argv": [
                    {"word": "/bin/x"},
                    {"arg": "n", "int": [0, 65535]},
                    {"arg": "m", "choice": ["a", "b"]},
                ],
                "root": False,
                "modes": [],
            },
        ),
        (
            Command("/bin/x", modes=["maintenance"], sudo=True),
            {"argv": [{"word": "/bin/x"}], "root": True, "modes": ["maintenance"]},
        ),
    ],
)
def test_the_menu_types_each_word_and_names_its_modes(
    command: Command, entry: dict[str, object]
) -> None:
    menu = json.loads(Login("tenant", keys=[KEY], commands={"logs": command}).menu())
    assert menu["entries"] == [{"name": "logs", **entry}]


@pytest.mark.parametrize(
    ("modes", "firewall", "found"),
    [
        (["maintenance"], MODES, None),
        (["debug"], MODES, "offered in debug, which the image's Firewall does not declare"),
        (["maintenance"], Firewall(), "(it has no modes)"),
    ],
)
def test_a_command_names_only_modes_the_firewall_declares(
    modes: list[str], firewall: Firewall, found: str | None
) -> None:
    login = Login(
        "tenant", owner=True, commands={**STATUS, "x": Command("/x", modes=modes, sudo=True)}
    )
    messages = [
        d.message
        for d in plan(_image(login, firewall=firewall), "production").lint()
        if d.code == "login-mode-undeclared"
    ]
    assert [str(found) in message for message in messages] == ([] if found is None else [True])


@pytest.mark.parametrize(
    ("profile", "menu", "sudoers", "terminal"),
    [
        ("production", ["status"], None, False),
        ("staging", ["status"], None, False),
        (
            "dev",
            ["debug", "shell", "status"],
            'tenant ALL=(root) NOPASSWD: /usr/bin/debug ""\n',
            True,
        ),
    ],
)
def test_a_dev_command_exists_in_development_builds_only(
    profile: str, menu: list[str], sudoers: str | None, terminal: bool
) -> None:
    login = Login(
        "tenant",
        owner=True,
        commands={
            **STATUS,
            "shell": Command("/usr/bin/bash", dev=True),
            "debug": Command("/usr/bin/debug", dev=True, sudo=True),
        },
    )
    baked = Login("guest", keys=[KEY], commands={**STATUS, "sh": Command("/bin/sh", dev=True)})
    planned = plan(_image(login, baked), profile)
    names = [entry["name"] for entry in json.loads(planned.file(MENU).text)["entries"]]
    assert names == menu
    pty = ",pty" if terminal else ""
    assert f"    PermitTTY {'yes' if terminal else 'no'}\n" in planned.file(CONFIG).text
    assert planned.file("/etc/ssh/authorized_keys.d/guest").text.startswith(f"restrict{pty},")
    assert ("  pty: true\n" in planned.file("/etc/tdx/secrets.yaml").text) == terminal
    if sudoers is None:
        assert SUDOERS not in planned.files
    else:
        assert sudoers in planned.file(SUDOERS).text
        assert "sudo" in planned.packages


def test_a_dev_command_needs_a_development_profile() -> None:
    login = Login("tenant", owner=True, commands={**STATUS, "shell": Command("/bin/sh", dev=True)})
    image = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"], state=DATA.directory("app"))],
        components=[SshAccess(login=login)],
    )
    with pytest.raises(ValidationError, match="no development profile"):
        plan(image, "production").files  # noqa: B018
    with_dev = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"], state=DATA.directory("app"))],
        components=[SshAccess(login=login)],
        dev=DevAccess(password="x"),
    )
    assert "/bin/sh" in plan(with_dev, "dev").file("/etc/tundravm/logins/tenant.json").text


def test_show_prints_each_command_who_runs_it_its_argv_modes_and_profiles() -> None:
    login = Login(
        "tenant",
        owner=True,
        commands={
            "logs": Command(
                "/usr/bin/tail", "-n", Int("lines", 1, 1000), "/var/log/app", sudo=True
            ),
            "shell": Command("/usr/bin/bash", dev=True),
            "toggle": MODES.commands.switch,
            "debug": Command("/usr/bin/debug", modes=["maintenance"], sudo=True),
        },
    )
    report = declared(_image(login), ["production", "dev", "staging"], {"firewall": MODES})
    shown = render_sections(declared_sections(report))
    assert '"toggle": firewall.commands.switch' in shown
    assert shown.split("Logins (1):\n")[1] == (
        "  tenant  the owner's key, a menu\n"
        "    debug   root    /usr/bin/debug  (modes maintenance)\n"
        "    logs    root    /usr/bin/tail -n <lines 1-1000> /var/log/app\n"
        "    shell   tenant  /usr/bin/bash  (profiles dev)\n"
        "    toggle  root    /usr/bin/mode-switch to <mode production|maintenance>"
    )
    rows: Any = report["logins"]
    assert rows[0]["commands"][2] == {
        "name": "shell",
        "runs_as": "tenant",
        "argv": [{"word": "/usr/bin/bash"}],
        "shown": "/usr/bin/bash",
        "modes": [],
        "profiles": ["dev"],
    }


def test_the_dev_probe_checks_each_login_its_menu_and_root() -> None:
    login = Login(
        "tenant", owner=True, commands={"logs": Command("/bin/x", Int("n", 1, 9), sudo=True)}
    )
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
        (
            lambda: Login("t", keys=[KEY], commands={"x": ["/bin/x", "{n:int:1-9}"]}),  # type: ignore[dict-item]
            r'Command\("/bin/x", Int\("n", 1, 9\)\) runs it as the login',
        ),
        (lambda: Login("t", keys=[KEY], commands={"X": Command("/bin/x")}), "not a command word"),
        (
            lambda: Login("t", keys=[KEY], commands={"x": Command("/x", dev=True)}),
            "empty menu",
        ),
        (lambda: Command("x"), "not an absolute path"),
        (lambda: Command(), "runs nothing"),
        (lambda: Command("/x", "--n={n:int}"), r'Write Int\("n", 0, 65535\)'),
        (lambda: Command("/x", "{m:choice:a|b}"), r'Write Choice\("m", "a", "b"\)'),
        (
            lambda: Command(["/x", "{n:int:1-9}"], sudo=True),  # type: ignore[arg-type]
            r'Command\("/x", Int\("n", 1, 9\), sudo=True\)',
        ),
        (lambda: Command("/x", Int("n", 0, 1), Int("n", 0, 1)), "twice"),
        (lambda: Command("/x", modes="maintenance"), "not a list"),
        (lambda: Command("/x", modes=["tdx"]), "security mode"),
        (lambda: Command("/x", modes=["Maint"]), "not a firewall mode"),
        (lambda: Int("n", 9, 1), "low one first"),
        (lambda: Int("n", -1, 1), "from 0"),
        (lambda: Int("N", 0, 1), "not an argument name"),
        (lambda: Choice("m"), "offers no value"),
        (lambda: Choice("m", "-f"), "offers '-f'"),
        (lambda: Choice("m", "a", "a"), "a value twice"),
        (lambda: Command("/x", "*", sudo=True), "wildcard"),
        (lambda: Command("/x", "a b", sudo=True), "a space"),
        (lambda: Command("/x", "a=b", Int("n", 0, 1), sudo=True), "pattern"),
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
        list(result) if not isinstance(result, (Login, Command, SshAccess, Int, Choice)) else None


def test_an_owner_login_needs_the_attested_endpoint() -> None:
    image = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"])],
        components=[SshAccess(login=Login("tenant", owner=True, commands=STATUS))],
    )
    with pytest.raises(ValidationError, match="runs no attested endpoint"):
        plan(image, "production").files  # noqa: B018
