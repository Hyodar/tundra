"""``SshAccess(login=Login(...))``: a non-root login into its program or a shell, keys
baked, delivered or the owner's, its commands argparse-like (``Command``, ``CommandParam``,
a ``ParamType``) and offered by firewall mode and by build, the Go program the tree holds
for them, its ``sudo=True`` commands as the sudoers lines of every argv they produce, what
``show`` says of it, its probe rows and its lint."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from tundravm import Image, Profile, Service
from tundravm._describe import declared, declared_sections, render_sections
from tundravm.components.access import (
    Command,
    CommandParam,
    Login,
    ParamType,
    SshAccess,
)
from tundravm.components.network import Firewall, Mode
from tundravm.declarative import _login
from tundravm.declarative.state import source_build
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
STATUS = {"status": Command(runs=["/usr/bin/systemctl", "status", "app"])}
CONFIG = "/etc/ssh/sshd_config.d/60-tundravm-login-tenant.conf"
SSHD = "/etc/ssh/sshd_config.d/50-tundravm.conf"
SUDOERS = "/etc/sudoers.d/tenant"
PROGRAM = "/usr/libexec/tundravm/cli/tenant"
BUILD = "tundravm-cli-tenant"
DATA = Volume("data", unlock=OperatorKey())
MODES = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[TCP(22)])})
LINES = CommandParam("--lines", type=int, min=1, max=1000, default=100, help="lines")
FOLLOW = CommandParam("--follow", type=bool)
MODE = CommandParam("mode", choices=["a", "b"])


@dataclass(frozen=True)
class Hex(ParamType):
    digits: int

    def usage(self) -> str:
        return f"{self.digits} hex digits"

    def check(self) -> str:
        return f'Match(value, "{self.pattern()}")'

    def pattern(self) -> str:
        return f"^[0-9a-f]{{{self.digits}}}$"


@dataclass(frozen=True)
class Pattern(ParamType):
    regex: str

    def usage(self) -> str:
        return self.regex

    def check(self) -> str:
        return f"Match(value, {json.dumps(self.regex)})"

    def pattern(self) -> str:
        return self.regex


@dataclass(frozen=True)
class Unbounded(ParamType):
    def usage(self) -> str:
        return "anything"

    def check(self) -> str:
        return "nil"

    def pattern(self) -> None:
        return None


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
                "/etc/ssh/authorized_keys.d/tenant": f'restrict,command="{PROGRAM}" {KEY}\n',
                CONFIG: f"    PermitTTY no\n    ForceCommand {PROGRAM}\n",
            },
            [SUDOERS],
            PROGRAM,
        ),
        (
            Login("tenant", keys=[KEY], shell="/usr/bin/menu", home=True),
            {
                "/etc/ssh/authorized_keys.d/tenant": f"restrict,pty {KEY}\n",
                CONFIG: "    AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u\n"
                "    DisableForwarding yes\n",
            },
            [SUDOERS],
            "/usr/bin/menu",
        ),
        (
            Login(
                "tenant",
                owner=True,
                commands={"web": Command(runs=["/usr/bin/systemctl", "restart"], sudo=True)},
            ),
            {SUDOERS: "tenant ALL=(root) NOPASSWD: /usr/bin/systemctl restart\n"},
            ["/etc/ssh/authorized_keys.d/tenant"],
            PROGRAM,
        ),
    ],
)
def test_a_login_lowers_to_its_account_keys_program_and_rules(
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
    assert (BUILD in planned.builds) == (login.commands is not None)
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
        (Command(runs=["/usr/bin/toggle"], sudo=True), ['/usr/bin/toggle ""']),
        (Command(runs=["/usr/bin/x", "a=b,c"], sudo=True), ["/usr/bin/x a\\=b\\,c"]),
        (Command(runs=["/usr/bin/x", "a#b", "#c"], sudo=True), ["/usr/bin/x a\\#b \\#c"]),
        (Command(runs=["/usr/bin/x", "^a", "b$"], sudo=True), ["/usr/bin/x \\^a b$"]),
        (
            Command(params=[LINES], runs=["/usr/bin/a=b#c", "{{lines}}"], sudo=True),
            ["/usr/bin/a\\=b\\#c ^[0-9]+$"],
        ),
        (
            Command(params=[MODE], runs=["/usr/bin/x", "to", "{{mode}}"], sudo=True),
            ["/usr/bin/x to a", "/usr/bin/x to b"],
        ),
        (
            Command(params=[FOLLOW], runs=["/usr/bin/x", "{{#follow}}-f{{/follow}}"], sudo=True),
            ["/usr/bin/x -f", '/usr/bin/x ""'],
        ),
        (
            Command(params=[LINES], runs=["/usr/bin/x", "-n", "{{lines}}", "a.log"], sudo=True),
            ["/usr/bin/x ^-n [0-9]+ a[.]log$"],
        ),
        (
            Command(
                params=[CommandParam("digest", type=Hex(64))],
                runs=["/usr/bin/x", "{{digest}}"],
                sudo=True,
            ),
            ["/usr/bin/x ^[0-9a-f]{64}$"],
        ),
        (
            Command(
                params=[CommandParam("unit", type=Pattern("^[a-z][a-z.-]*$"))],
                runs=["/usr/bin/x", "{{unit}}"],
                sudo=True,
            ),
            ["/usr/bin/x ^[a-z][a-z.-]*$"],
        ),
        (Command(runs=["/usr/bin/x"], modes=["maintenance"], sudo=True), ['/usr/bin/x ""']),
        (Command(runs=["/usr/bin/x"]), []),
    ],
)
def test_a_root_command_is_the_sudoers_lines_of_every_argv(
    command: Command, rules: list[str]
) -> None:
    login = Login("tenant", keys=[KEY], commands={"run": command, **STATUS})
    assert login.sudo_rules() == tuple(f"tenant ALL=(root) NOPASSWD: {r}" for r in rules)


@pytest.mark.parametrize(
    ("words", "params", "go", "shown"),
    [
        (["/x", "lit"], [], '"/x", "lit"', "/x lit"),
        (["/x", "{{mode}}"], [MODE], '"/x", v.Get("mode")', "/x <mode>"),
        (["/x", "--n={{lines}}"], [LINES], '"/x", "--n="+v.Get("lines")', "/x --n=<lines>"),
        (["/x", "{{#follow}}-f{{/follow}}"], [FOLLOW], '"/x", v.If("follow", "-f")', "/x [-f]"),
    ],
)
def test_each_template_form_is_a_word_of_the_program(
    words: list[str], params: list[CommandParam], go: str, shown: str
) -> None:
    command = Command(params=params, runs=words)
    main = Login("tenant", keys=[KEY], commands={"run": command}).program()["main.go"]
    assert f"return Words({go})" in main
    assert command.shown() == shown


@dataclass(frozen=True)
class Checked(ParamType):
    check_text: str

    def usage(self) -> str:
        return "a value"

    def check(self) -> str:
        return self.check_text

    def pattern(self) -> None:
        return None


@pytest.mark.parametrize(
    ("check", "written"),
    [
        ('Match(value, "^[0-9a-f]{64}$")', True),
        ("Int(value, 1, 100)", True),
        ('OneOf(value, "a", "say \\"hi\\"")', True),
        ("nil", False),
        (
            'Match(value, "^a$") }}; func init() { panic("x") }; var _ = func() error { return nil',
            False,
        ),
        ('Match(value, "a\nb")', False),
        ("Match(value, `^a$`)", False),
        ('os.RemoveAll("/")', False),
    ],
)
def test_a_params_check_is_one_runtime_call_on_literals(check: str, written: bool) -> None:
    command = Command(params=[CommandParam("v", type=Checked(check))], runs=["/x", "{{v}}"])
    login = Login("tenant", keys=[KEY], commands={"run": command})
    if written:
        assert f"return {check} }}" in login.program()["main.go"]
    else:
        with pytest.raises(ValidationError, match="not one call"):
            login.program()


@pytest.mark.parametrize(
    ("source", "named"),
    [("image.py", "image.py"), ('a\n}\nfunc init() { panic("x") }\n//.py', "a\\n}\\nfunc init")],
)
def test_the_declaring_file_stays_in_the_programs_first_comment(source: str, named: str) -> None:
    main, _ = _login.program("tenant", None, STATUS, source)
    first, second = main.splitlines()[:2]
    assert first.startswith(f"// Code generated by tundravm from {named}")
    assert second == "package main"


def test_the_program_is_the_four_files_the_build_compiles() -> None:
    login = Login(
        "tenant",
        owner=True,
        commands={**STATUS, "shell": Command(runs=["/bin/sh"], dev=True)},
    )
    planned = plan(_image(login), "production")
    tree = {e.path for e in planned.tree.entries}
    names = ["cli.go", "dev.go", "go.mod", "main.go"]
    assert {f"payload/tundravm-cli/tenant/{name}" for name in names} <= tree
    build = planned.builds[BUILD]
    assert [step.destination for step in build.install] == [PROGRAM]
    hook = source_build(build).render("0" * 64)
    developed, production = hook.split("\nelse\n")
    assert hook.split("\n")[1] == 'if [[ " ${PROFILES:-} " == *" dev "* ]]; then'
    assert "-tags dev" in developed and "-tags" not in production
    assert '"$SRCDIR/payload/tundravm-cli/tenant"' in production


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
    command = Command(runs=["/x"], modes=modes, sudo=True)
    login = Login("tenant", owner=True, commands={**STATUS, "x": command})
    messages = [
        d.message
        for d in plan(_image(login, firewall=firewall), "production").lint()
        if d.code == "login-mode-undeclared"
    ]
    assert [str(found) in message for message in messages] == ([] if found is None else [True])


@pytest.mark.parametrize(
    ("profile", "sudoers", "terminal"),
    [
        ("production", None, False),
        ("staging", None, False),
        ("dev", 'tenant ALL=(root) NOPASSWD: /usr/bin/debug ""\n', True),
    ],
)
def test_a_dev_command_exists_in_development_builds_only(
    profile: str, sudoers: str | None, terminal: bool
) -> None:
    login = Login(
        "tenant",
        owner=True,
        commands={
            **STATUS,
            "shell": Command(runs=["/usr/bin/bash"], dev=True),
            "debug": Command(runs=["/usr/bin/debug"], dev=True, sudo=True),
        },
    )
    baked = Login(
        "guest", keys=[KEY], commands={**STATUS, "sh": Command(runs=["/bin/sh"], dev=True)}
    )
    planned = plan(_image(login, baked), profile)
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
    shell = Command(runs=["/bin/sh"], dev=True)
    login = Login("tenant", owner=True, commands={**STATUS, "shell": shell})
    image = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"], state=DATA.directory("app"))],
        components=[SshAccess(login=login)],
    )
    with pytest.raises(ValidationError, match="no development profile"):
        plan(image, "production").files  # noqa: B018


def test_show_prints_each_logins_listing_and_what_each_command_runs() -> None:
    login = Login(
        "tenant",
        owner=True,
        help="run the app",
        commands={
            "logs": Command(
                params=[LINES, FOLLOW],
                runs=["/usr/bin/tail", "-n", "{{lines}}", "{{#follow}}-f{{/follow}}", "/a.log"],
                sudo=True,
                help="the app's log",
            ),
            "shell": Command(runs=["/usr/bin/bash"], dev=True, help="a shell"),
            "toggle": MODES.commands.switch,
            "debug": Command(runs=["/usr/bin/debug"], modes=["maintenance"], sudo=True),
        },
    )
    report = declared(_image(login), ["production", "dev", "staging"], {"firewall": MODES})
    shown = render_sections(declared_sections(report))
    assert '"toggle": firewall.commands.switch' in shown
    assert shown.split("Logins (1):\n")[1] == (
        f"  tenant  the owner's key, {PROGRAM}\n"
        "    tenant: run the app\n"
        "    ssh tenant@HOST COMMAND\n"
        "      debug                        (as root, maintenance only)\n"
        "      logs [--lines N] [--follow]  the app's log (as root)\n"
        "      shell                        a shell (profiles dev)\n"
        "      toggle <mode>                switch the firewall mode (as root)\n"
        "    debug   runs: /usr/bin/debug (as root)\n"
        "    logs    runs: /usr/bin/tail -n <lines> [-f] /a.log (as root)\n"
        "    shell   runs: /usr/bin/bash\n"
        "    toggle  runs: /usr/bin/mode-switch to <mode> (as root)"
    )
    rows: Any = report["logins"]
    assert rows[0]["commands"][1] == {
        "name": "logs",
        "help": "the app's log",
        "params": [
            {
                "name": "lines",
                "option": "--lines",
                "type": "int",
                "usage": "1-1000",
                "default": "100",
                "help": "lines",
            },
            {
                "name": "follow",
                "option": "--follow",
                "type": "bool",
                "usage": "",
                "default": None,
                "help": None,
            },
        ],
        "runs": ["/usr/bin/tail", "-n", "{{lines}}", "{{#follow}}-f{{/follow}}", "/a.log"],
        "sudo": True,
        "modes": [],
        "profiles": ["production", "dev", "staging"],
        "usage": "logs [--lines N] [--follow]",
        "shown": "/usr/bin/tail -n <lines> [-f] /a.log (as root)",
    }


def test_the_dev_probe_checks_each_login_its_program_and_root() -> None:
    logs = Command(params=[LINES], runs=["/bin/x", "{{lines}}"], sudo=True)
    login = Login("tenant", owner=True, commands={"logs": logs})
    probe = plan(_image(login), "dev").file("/usr/lib/tundravm/probe").text
    assert f"probe_login tenant /etc/sudoers.d/tenant '-' '{PROGRAM}'\n" in probe
    assert f"probe_menu tenant {PROGRAM} /etc/sudoers.d/tenant 'logs --lines 1001' logs\n" in probe
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


def _choices(count: int) -> CommandParam:
    return CommandParam("--m", choices=[f"v{n}" for n in range(count)], default="v0")


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
            lambda: Login("t", keys=[KEY], commands={"x": ["/bin/x", "a"]}),  # type: ignore[dict-item]
            r'Command\(runs=\["/bin/x", "a"\]\) runs it as the login',
        ),
        (lambda: Login("t", keys=[KEY], commands={"X": STATUS["status"]}), "not a command word"),
        (lambda: Login("t", keys=[KEY], commands={"help": STATUS["status"]}), "program's own"),
        (
            lambda: Login("t", keys=[KEY], commands={"x": Command(runs=["/x"], dev=True)}),
            "empty program",
        ),
        (
            lambda: Command("/usr/bin/x", "reboot", sudo=True),  # type: ignore[arg-type]
            r'Command\(runs=\["/usr/bin/x", "reboot"\], sudo=True\)',
        ),
        (lambda: Command(), "runs nothing"),
        (lambda: Command(runs=[]), "runs= is empty"),
        (lambda: Command(runs=["x"]), "not the program's absolute path"),
        (lambda: Command(params=[MODE], runs=["{{mode}}"]), "not the program's absolute path"),
        (lambda: Command(runs=["/x", "a b"]), "never split"),
        (lambda: Command(params=[MODE], runs=["/x"]), "login-param-unused"),
        (lambda: Command(runs=["/x", "{{mode}}"]), "login-param-unknown"),
        (lambda: Command(params=[MODE, MODE], runs=["/x", "{{mode}}"]), "twice"),
        (lambda: Command(params=[FOLLOW], runs=["/x", "{{follow}}"]), "is a flag"),
        (lambda: Command(params=[MODE], runs=["/x", "{{#mode}}-m{{/mode}}"]), "no flag"),
        (lambda: Command(params=[FOLLOW], runs=["/x", "-{{#follow}}f{{/follow}}"]), "flag's word"),
        (lambda: Command(runs=["/x", "a}}"]), "brace"),
        (lambda: Command(runs=["/x"], modes="maintenance"), "not a list"),
        (lambda: Command(runs=["/x"], modes=["tdx"]), "security mode"),
        (lambda: Command(runs=["/x"], modes=["Maint"]), "not a firewall mode"),
        (lambda: Command(runs=["/x", "*"], sudo=True), "wildcard"),
        (
            lambda: Command(params=[LINES], runs=["/x", "--n={{lines}}"], sudo=True),
            "part of a sudoers pattern",
        ),
        *(
            (
                lambda pattern=pattern: Command(
                    params=[CommandParam("v", type=Pattern(pattern))],
                    runs=["/x", "{{v}}"],
                    sudo=True,
                ),
                "of one word",
            )
            for pattern in ("[0-9]+", "^[0-9]+", "^a$|^b$", "^.+$", "^[^/]+$", "^a\tb$", "^é$")
        ),
        (
            lambda: Command(
                params=[CommandParam("--v", type=Unbounded(), default="a")],
                runs=["/x", "{{v}}"],
                sudo=True,
            ),
            "has no pattern",
        ),
        (
            lambda: Command(
                params=[_choices(9), CommandParam("--n", choices=[*"abcdefgh"], default="a")],
                runs=["/x", "{{m}}", "{{n}}"],
                sudo=True,
            ),
            "login-sudo-lines",
        ),
        (lambda: CommandParam("n", type=int), "takes min= and max="),
        (lambda: CommandParam("n", type=int, min=9, max=1), "min first"),
        (lambda: CommandParam("n"), "takes choices="),
        (lambda: CommandParam("n", choices=["-f"]), "offer '-f'"),
        (lambda: CommandParam("n", choices=["a", "a"]), "a value twice"),
        (lambda: CommandParam("f", type=bool), "is an option"),
        (lambda: CommandParam("--f", type=bool, default=True), "has no default"),
        (lambda: CommandParam("n", choices=["a"], default="a"), "is required"),
        (lambda: CommandParam("--n", choices=["a"]), "needs default="),
        (lambda: CommandParam("--n", type=int, min=1, max=9, default=10), "from 1 to 9"),
        (lambda: CommandParam("--help", type=bool), "program's own help"),
        (lambda: CommandParam("-h", type=bool), "program's own help"),
        (lambda: CommandParam("-n", type=bool), "neither a positional"),
        (lambda: CommandParam("n", type=float), "not int, str, bool"),
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
        list(result) if not isinstance(result, (Login, Command, CommandParam, SshAccess)) else None


def test_an_owner_login_needs_the_attested_endpoint() -> None:
    image = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"])],
        components=[SshAccess(login=Login("tenant", owner=True, commands=STATUS))],
    )
    with pytest.raises(ValidationError, match="runs no attested endpoint"):
        plan(image, "production").files  # noqa: B018
