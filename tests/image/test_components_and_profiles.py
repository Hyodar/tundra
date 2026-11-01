"""``Component`` exports, ``Profile`` semantics, ``DevAccess`` and the ``Mkosi`` refusals."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tundravm import File, Image, Mkosi, Package, Profile, Service
from tundravm._image import recipe_of
from tundravm._image._values import RESERVED_SETTINGS
from tundravm.components import (
    Component,
    DevAccess,
    Diagnostic,
    Egress,
    Endpoint,
    Network,
    Resolved,
    Rule,
)
from tundravm.errors import ValidationError
from tundravm.ports import TCP, UDP
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import Resources, TcpProbe
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")


class Database(Component):
    """A component with three exports: its readiness, its endpoint and its service."""

    port: int = 5432

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint("127.0.0.1", self.port, ready=self.service("db").ready, provider=self)

    def compose(self) -> Iterator[object]:
        yield Package("postgresql")
        yield Service("db", ["/usr/bin/postgres"], readiness=TcpProbe(self.port))


class Api(Component):
    """A consumer: holding the export requires its provider without naming the fragment."""

    database: Endpoint

    def compose(self) -> Iterator[object]:
        yield Service("api", ["/usr/bin/api", "--db", self.database], requires=[self.database])


class Sloppy(Component):
    """A component with a lint rule whose code it does not declare."""

    port: int = 80

    def compose(self) -> Iterator[object]:
        yield File.text("/usr/bin/picky", "#!/bin/sh\n", mode=0o755)
        yield Service("picky", ["/usr/bin/picky"], expose=[TCP(self.port)])

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        if self.port >= 1024:
            return ()
        return (Diagnostic("picky-privileged-port", "error", f"port {self.port} needs root"),)


class Picky(Sloppy):
    """The same lint rule, declared."""

    rules = (Rule("picky-privileged-port", "error", "Below 1024 needs root.", "Use 8080."),)


# ── components ──────────────────────────────────────────────────────────


def test_a_component_is_a_frozen_keyword_only_dataclass() -> None:
    assert Database(port=5432) == Database()
    assert "port=5432" in repr(Database())
    with pytest.raises(TypeError):
        Database(5432)  # type: ignore[misc]


def test_the_fragment_is_named_after_the_class() -> None:
    assert Database().name == "database"

    class TaikoClient(Component):
        def compose(self) -> Iterator[object]:
            return iter(())

    assert TaikoClient().name == "taiko-client"

    class Named(Component, name="given"):
        def compose(self) -> Iterator[object]:
            return iter(())

    assert Named().name == "given"


def test_a_component_without_compose_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:

        class Empty(Component):
            pass

    assert "compose()" in str(caught.value)


def test_exports_resolve_to_the_units_behind_them() -> None:
    database = Database()
    assert database.endpoint.url == "http://127.0.0.1:5432/"
    assert database.service("db").unit == "db.service"
    assert database.ready.name == "database"

    planned = plan(Image("stack", components=[database, Api(database=database.endpoint)]))
    unit = planned.service("api")
    assert unit.get("Service", "ExecStart") == "/usr/bin/api --db http://127.0.0.1:5432/"
    assert unit.get("Unit", "Requires") == "db-ready.service tdx-evidence.service"


def test_naming_a_service_the_component_does_not_compose_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Database().service("nope")
    assert "db.service" in str(caught.value)


def test_component_ready_stands_for_every_service_that_declares_readiness() -> None:
    database = Database()
    app = Service("app", ["/usr/bin/app"], requires=[database.ready])
    planned = plan(Image("stack", components=[database], services=[app]))
    assert planned.service("app").get("Unit", "Requires") == "db-ready.service tdx-evidence.service"


def test_a_component_with_no_readiness_cannot_be_waited_for() -> None:
    class Quiet(Component):
        def compose(self) -> Iterator[object]:
            yield Service("quiet", ["/usr/bin/quiet"])

    with pytest.raises(ValidationError) as caught:
        Service("app", ["/usr/bin/app"], requires=[Quiet().ready])
    assert "declares no readiness" in str(caught.value)


@pytest.mark.parametrize(
    ("component", "errors"),
    [
        (Picky(port=8080), []),
        (Picky(port=80), ["picky-privileged-port"]),
        (Sloppy(port=80), ["picky-privileged-port", "rule-undeclared"]),
    ],
)
def test_a_components_check_is_its_lint_rule_of_the_codes_it_declares(
    component: Component, errors: list[str]
) -> None:
    from tundravm.declarative import lint

    found = [
        d for d in lint(recipe_of(Image("x", components=[component]))) if d.severity == "error"
    ]
    assert sorted(d.code for d in found) == errors
    undeclared = "Sloppy reports 'picky-privileged-port', which its class does not declare"
    assert (undeclared in {d.message for d in found}) == ("rule-undeclared" in errors)


def test_a_nested_component_is_composed_too() -> None:
    class Stack(Component):
        def compose(self) -> Iterator[object]:
            yield Database()

    planned = plan(Image("x", components=[Stack()]))
    assert planned.packages["postgresql"] == "runtime"
    assert Stack().services()[0].name == "db"


# ── profiles ────────────────────────────────────────────────────────────


def test_a_profile_adds_and_changes_by_reference() -> None:
    motd = File.text("/etc/motd", "production\n")
    image = Image(
        "x",
        files=[motd],
        profiles={"staging": Profile(add=["strace"], changes=[motd.ref.edit(content="staging\n")])},
    )
    assert plan(image, "production").file("/etc/motd").text == "production\n"
    staging = plan(image, "staging")
    assert staging.file("/etc/motd").text == "staging\n"
    assert staging.packages["strace"] == "runtime"
    assert "strace" not in plan(image, "production").packages


def test_a_profile_owns_the_files_it_adds() -> None:
    image = Image(
        "x",
        files=[File.text("/etc/a", "a\n", owner="nobody")],
        profiles={"staging": Profile(add=[File.text("/etc/b", "b\n", owner="nobody")])},
    )
    owned = "/etc/tmpfiles.d/50-tundravm-owned"
    assert "z /etc/a - nobody nobody -" in plan(image, "staging").file(f"{owned}.conf").text
    assert (
        plan(image, "staging")
        .file(f"{owned}-staging.conf")
        .text.endswith("z /etc/b - nobody nobody -\n")
    )
    assert f"{owned}-staging.conf" not in plan(image, "production").files


def test_a_profile_removes_a_service_by_reference() -> None:
    app = Service("app", ["/usr/bin/app"], expose=[TCP(8080)])
    image = Image("x", services=[app], profiles={"slim": Profile(changes=[app.ref.remove()])})
    assert plan(image, "production").service("app").get("Service", "ExecStart")
    units = {
        getattr(item, "name", "")
        for item in plan(image, "slim").resolved.items
        if type(item).__name__ == "Service"
    }
    assert "app" not in units


@pytest.mark.parametrize(
    ("target", "fields", "expected"),
    [
        ("app", {"environment": {"STAGE": "staging"}}, ("Environment", "STAGE=staging")),
        ("app", {"argv": ["/usr/bin/app", "--verbose"]}, ("ExecStart", "/usr/bin/app --verbose")),
        ("app", {"resources": Resources(memory_max="1G")}, ("MemoryMax", "1G")),
        ("db", {"user": "postgres"}, ("User", "postgres")),
        ("app", {"env": {"STAGE": "staging"}}, "sets: argv, requires, state, expose"),
        ("app", {"exec_start": ["/usr/bin/app"]}, "sets: argv, requires, state, expose"),
        ("app", {"readiness": TcpProbe(8080)}, "cannot set Service readiness="),
        ("app", {"expose": [TCP(8080)]}, "changes nothing"),
        ("app", {"argv": ["/usr/bin/app", Secret("jwt")]}, "changes the credentials it binds"),
        ("vol", {"user": "www"}, "prepares its directory on volume 'data' at boot for 'vol'"),
    ],
)
def test_a_profile_edits_a_service_by_its_public_keywords(
    target: str, fields: dict[str, object], expected: tuple[str, str] | str
) -> None:
    app = Service("app", ["/usr/bin/app"], expose=[TCP(8080)])
    vol = Service(
        "vol", ["/usr/bin/vol"], state=Volume("data", unlock=OperatorKey()).directory("vol")
    )
    refs = {"app": app.ref, "db": Database().service("db").ref, "vol": vol.ref}
    ref = refs[target]
    if isinstance(expected, str):
        with pytest.raises(ValidationError) as caught:
            ref.edit(**fields)
        assert expected in str(caught.value)
        assert "env_file" not in str(caught.value)  # the internal unit's fields stay unnamed
        return
    image = Image(
        "x",
        services=[app],
        components=[Database()],
        profiles={"staging": Profile(changes=[ref.edit(**fields)])},
    )
    key, value = expected
    assert value in plan(image, "staging").service(target).values("Service", key)
    assert value not in plan(image, "production").service(target).values("Service", key)


def test_a_profile_that_is_not_production_is_marked() -> None:
    image = Image("x", profiles={"bench": Profile(add=["fio"], production=False)})
    recipe = recipe_of(image)
    assert {v.name: v.release for v in recipe.variants} == {"production": True, "bench": False}


def test_extending_a_profile_that_is_not_declared_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Image("x", profiles={"a": Profile(extends="ghost")})
    assert "Declared profiles" in str(caught.value) or "production" in str(caught.value)


def test_the_image_itself_cannot_be_redeclared_as_a_profile() -> None:
    with pytest.raises(ValidationError) as caught:
        Image("x", profiles={"production": Profile()})
    assert "the image itself" in str(caught.value)


def test_a_change_must_be_an_edit_or_a_removal() -> None:
    with pytest.raises(ValidationError) as caught:
        Profile(changes=["motd"])  # type: ignore[list-item]
    assert "value.ref.edit" in str(caught.value)


# ── dev access ──────────────────────────────────────────────────────────

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyForTheTests me@host"


def test_dev_access_is_a_non_production_profile_with_public_keys_only() -> None:
    image = Image("x", dev=DevAccess(ssh_keys=[KEY]))
    recipe = recipe_of(image)
    assert {v.name: v.release for v in recipe.variants} == {"production": True, "dev": False}

    dev = plan(image, "dev")
    assert dev.file("/etc/ssh/authorized_keys.d/root").text.strip() == KEY
    config = dev.file("/etc/ssh/sshd_config.d/50-tundravm.conf").text
    assert "PasswordAuthentication no" in config
    assert "PermitRootLogin prohibit-password" in config
    assert "openssh-server" in dev.packages
    assert "openssh-server" not in plan(image, "production").packages


def test_dev_access_reads_a_public_key_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "id_ed25519.pub"
    path.write_text(f"{KEY}\n", encoding="utf-8")
    assert DevAccess(ssh_keys=[str(path)]).public_keys() == (KEY,)


def test_dev_access_refuses_a_private_key() -> None:
    with pytest.raises(ValidationError) as caught:
        DevAccess(ssh_keys=["-----BEGIN OPENSSH PRIVATE KEY-----"])
    assert "private key" in str(caught.value)


def test_dev_access_that_grants_nothing_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        DevAccess()
    assert "grants nothing" in str(caught.value)


def test_a_dev_profile_and_dev_access_together_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Image("x", dev=DevAccess(ssh_keys=[KEY]), profiles={"dev": Profile()})
    assert "both declare one" in str(caught.value)


# ── network and mkosi ───────────────────────────────────────────────────


def test_an_egress_filters_the_output_chain() -> None:
    egress = Egress(ports=[TCP(443)], hosts=["1.1.1.1"])
    image = Image("x", network=Network(egress=egress), targets=("qemu", "azure"))
    rules = plan(image, "production").file("/etc/nftables.conf").text
    assert "hook output priority filter; policy drop" in rules
    assert 'tcp dport 443 accept comment "image"' in rules
    assert 'ip daddr 1.1.1.1 accept comment "image, tee-base"' in rules
    # what the baseline sends: NTS time, TeeBase's resolvers, Azure's wireserver
    assert 'tcp dport 4460 accept comment "tee-base"' in rules and "udp dport 123 accept" in rules
    assert "ip daddr 1.0.0.1 accept" in rules
    assert 'ip daddr 168.63.129.16 accept comment "azure"' in rules


class Sink(Component):
    """A component whose outbound() is a peer port and the host it ships to."""

    host: str = "logs.example:19532"

    def compose(self) -> Iterator[object]:
        yield from ()

    def outbound(self) -> Egress:
        return Egress(ports=[UDP(30303)], hosts=[self.host])


@pytest.mark.parametrize(
    ("host", "rule"),
    [
        ("logs.example:19532", 'tcp dport 19532 accept comment "sink (logs.example)"'),
        ("10.0.0.5:8545", 'ip daddr 10.0.0.5 tcp dport 8545 accept comment "sink"'),
        ("[fd00::5]:8545", 'ip6 daddr fd00::5 tcp dport 8545 accept comment "sink"'),
        ("10.0.0.0/8", 'ip daddr 10.0.0.0/8 accept comment "sink"'),
    ],
)
def test_a_components_outbound_joins_the_image_egress(host: str, rule: str) -> None:
    egress = Network(egress=Egress(ports=[UDP(30303)]))
    filtered = Image("x", components=[Sink(host=host)], network=egress)
    rules = plan(filtered, "production").file("/etc/nftables.conf").text
    assert rule in rules and 'udp dport 30303 accept comment "image, sink"' in rules
    unfiltered = plan(Image("x", components=[Sink(host=host)]), "production")
    assert "30303" not in unfiltered.file("/etc/nftables.conf").text


@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("logs.example", "host name without a port"),
        ("10.0.0.0/8:443", "gives a CIDR block a port"),
        ("logs.example:0", "no port in 1..65535"),
        ("not a host", "is not an address"),
    ],
)
def test_an_egress_host_is_an_address_a_block_or_host_port(host: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Egress(hosts=[host])


def test_a_network_takes_dhcp_or_a_static_address() -> None:
    with pytest.raises(ValidationError) as caught:
        Network(dhcp=False)
    assert "neither is set" in str(caught.value)


def test_mkosi_settings_and_scripts_are_recorded() -> None:
    image = Image(
        "x",
        mkosi=Mkosi(settings={"Hostname": "demo"}, scripts=[("postinst", "echo hi")]),
    )
    planned = plan(image, "production")
    assert planned.settings("Content")["Hostname"] == ("demo",)
    assert [hook.script for hook in planned.hooks("postinst")] == ["echo hi"]


@pytest.mark.parametrize("key", sorted(RESERVED_SETTINGS))
def test_a_setting_that_contradicts_an_invariant_is_refused(key: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Mkosi(settings={key: "x"})
    assert key in str(caught.value)
    assert RESERVED_SETTINGS[key].split(",")[0][:20] in str(caught.value)


def test_an_unknown_mkosi_script_stage_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Mkosi(scripts=[("whenever", "echo hi")])
    assert "postinst" in str(caught.value)
