"""``Component`` exports, ``Profile`` semantics, ``DevAccess`` and the ``Mkosi`` refusals."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tundravm import AptRepository, File, Image, Kernel, Mkosi, Package, Profile, Service, Tree
from tundravm._image import recipe_of
from tundravm._image._values import RESERVED_SETTINGS
from tundravm.components import Component, Diagnostic, Endpoint, Resolved, Rule
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Firewall, Host, Mode, Network
from tundravm.declarative._network import HOSTS_HEADER
from tundravm.errors import ValidationError
from tundravm.ports import TCP, UDP
from tundravm.secrets import Secret
from tundravm.sources import Git, GoBinary, Local
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import Resources, TcpProbe
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")
URL = "https://example.com/linux"


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


class Hinted(Picky):
    """The declared rule, its finding carrying a hint of its own."""

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        return (Diagnostic("picky-privileged-port", "error", "needs root", hint="Use 443."),)


# ── components ──────────────────────────────────────────────────────────


def test_a_component_is_a_frozen_keyword_only_dataclass() -> None:
    assert Database(port=5432) == Database()
    assert "port=5432" in repr(Database())


def test_the_fragment_is_named_after_the_class() -> None:
    assert Database().name == "database"

    class NodeExporter(Component):
        def compose(self) -> Iterator[object]:
            return iter(())

    assert NodeExporter().name == "node-exporter"

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


@pytest.mark.parametrize(
    ("component", "hint"),
    [(Hinted(port=80), "Use 443."), (Picky(port=80), "Use 8080."), (Sloppy(port=80), None)],
)
def test_a_components_finding_without_a_hint_gets_its_rules_fix(
    component: Component, hint: str | None
) -> None:
    from tundravm.declarative import lint

    found = lint(recipe_of(Image("x", components=[component])))
    assert [d.hint for d in found if d.code == "picky-privileged-port"] == [hint]


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


@pytest.mark.parametrize(
    ("profile", "steps"),
    [
        ("production", {"owners": "chown -R app:app /srv/www\nchown nobody:nogroup /etc/a\n"}),
        (
            "staging",
            {
                "owners": "chown -R app:app /srv/www\nchown nobody:nogroup /etc/a\n",
                "owners-staging": "chown app:app '/etc/b c'\n",
            },
        ),
    ],
)
def test_owner_is_a_boot_step_per_profile(
    tmp_path: Path, profile: str, steps: dict[str, str]
) -> None:
    (tmp_path / "index.html").write_text("x\n")
    image = Image(
        "x",
        files=[
            File.text("/etc/a", "a\n", owner="nobody:nogroup"),
            Tree(tmp_path, "/srv/www", owner="app"),
        ],
        services=[Service("app", ["/usr/bin/app"])],
        profiles={"staging": Profile(add=[File.text("/etc/b c", "b\n", owner="app")])},
    )
    planned = plan(image, profile)
    found = {s.name: s.script for s in planned.init_steps if s.name.startswith("owners")}
    assert found == steps
    assert "runtime-init-owners.service" in (planned.service("app").get("Unit", "After") or "")
    assert planned.unit("runtime-init-owners.service").get("Unit", "After") == "local-fs.target"


def test_an_owned_file_beside_a_built_binary_lowers() -> None:
    """A built binary composes its own items, which the owner step's wait copies through."""
    image = Image(
        "x",
        files=[File.text("/srv/x", "x\n", owner="app")],
        services=[Service("app", [GoBinary(Local("."), name="app")])],
    )
    after = plan(image, "production").service("app").get("Unit", "After") or ""
    assert "runtime-init-owners.service" in after


def test_a_file_under_a_volume_is_refused() -> None:
    data = Volume("data", unlock=OperatorKey())
    image = Image(
        "x",
        services=[Service("app", ["/usr/bin/app"], state=data.directory("app"))],
        files=[File.text("/persistent/data/x", "x\n", owner="app")],
    )
    with pytest.raises(ValidationError, match="hides what the image ships"):
        recipe_of(image)


@pytest.mark.parametrize(
    ("declare", "path"),
    [
        (lambda: File.copy("etc/app.conf", "/etc/app.conf").content, "etc/app.conf"),
        (lambda: Tree("www", "/srv/www").src, "www"),
        (
            lambda: Kernel.build("6.12", Git(URL, "v6.12"), config=Path("k.config")).config,
            "k.config",
        ),
        (lambda: AptRepository(URL, "stable", key=Path("v.asc"), sha256="0" * 64).key, "v.asc"),
    ],
    ids=["File.copy", "Tree", "Kernel config", "AptRepository key"],
)
def test_a_relative_host_path_is_beside_the_file_that_declares_it(
    declare: Callable[[], object], path: str
) -> None:
    assert declare() == Path(__file__).parent / path


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
        ("app", {"env": {"STAGE": "staging"}}, "sets: argv, requires, readiness, state"),
        ("app", {"exec_start": ["/usr/bin/app"]}, "sets: argv, requires, readiness, state"),
        ("app", {"expose": [TCP(8080)]}, "changes nothing"),
    ],
)
def test_a_profile_edits_a_service_by_its_public_keywords(
    target: str, fields: dict[str, object], expected: tuple[str, str] | str
) -> None:
    app = Service("app", ["/usr/bin/app"], expose=[TCP(8080)])
    refs = {"app": app.ref, "db": Database().service("db").ref}
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


@pytest.mark.parametrize(
    ("target", "fields", "expected"),
    [
        ("app", {"name": "web"}, "cannot set Service name=: it names the service"),
        ("bin", {"argv": [GoBinary(Local("."), name="other")]}, "changes the built binary"),
        ("app", {"argv": ["/usr/bin/app", Secret("jwt")]}, "unbinds jwt through argv="),
        ("app", {"environment": {"JWT": Secret("jwt")}}, "unbinds jwt through argv="),
        ("vol", {"user": "www"}, "prepares its directory on volume 'data' at boot for 'vol'"),
        ("db", {"readiness": None}, "requires 'db.service', which declares no readiness"),
        ("db", None, "requires 'db.service', which the profile removes"),
        ("ghost", {"user": "www"}, "edits Service('ghost'), which it does not inherit"),
        (
            "app",
            {"requires": [Service("cache", ["/usr/bin/cache"], readiness=TcpProbe(6379)).ready]},
            'Service("app") requires Service("cache"), which the image does not declare',
        ),
    ],
)
def test_what_a_service_edit_still_refuses(
    target: str, fields: dict[str, object] | None, expected: str
) -> None:
    db = Service("db", ["/usr/bin/db"], readiness=TcpProbe(5432))
    api = Service("api", ["/usr/bin/api"], requires=[db.ready])
    vol = Service(
        "vol", ["/usr/bin/vol"], state=Volume("data", unlock=OperatorKey()).directory("vol")
    )
    app = Service("app", ["/usr/bin/app"])
    built = Service("bin", [GoBinary(Local("."), name="bin")])
    ghost = Service("ghost", ["/usr/bin/ghost"])
    refs = {"app": app.ref, "bin": built.ref, "db": db.ref, "vol": vol.ref, "ghost": ghost.ref}
    try:
        change = refs[target].remove() if fields is None else refs[target].edit(**fields)
        image = Image("x", services=[db, api, vol, app], profiles={"s": Profile(changes=[change])})
        found = [d.message for d in plan(image, "s").lint() if d.severity == "error"]
    except ValidationError as caught:
        found = [str(caught)]
    assert any(expected in message for message in found), found


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
    egress = Egress(ports=[TCP(443)], hosts=["10.0.0.5"])
    image = Image("x", network=Network(egress=egress), targets=("qemu", "azure"))
    rules = plan(image, "production").file("/etc/nftables.conf").text
    assert "hook output priority filter; policy drop" in rules
    assert 'tcp dport 443 accept comment "image"' in rules
    assert 'ip daddr 10.0.0.5 accept comment "image"' in rules
    # what the baseline sends: NTS time and DNS over TLS; the wireserver is the glue's
    nts = 'ip daddr @host4_time.cloudflare.com tcp dport 4460 accept comment "tee-base"'
    assert nts in rules and 'meta skuid "_chrony" udp dport 123 accept comment "tee-base"' in rules
    assert [line for line in rules.splitlines() if "udp dport 123 accept" in line] == [
        '\t\tip daddr 169.254.169.254 udp dport 123 accept comment "metadata time"',
        '\t\tmeta skuid "_chrony" udp dport 123 accept comment "tee-base"',
    ]
    assert 'ip daddr 1.1.1.1 tcp dport 853 accept comment "network"' in rules
    assert "ip daddr 1.0.0.1 tcp dport 853 accept" in rules and "dport 53 " not in rules
    glue = 'meta skuid 0 meta skgid "azure-complete-provisioning" ip daddr 168.63.129.16 accept'
    assert glue in rules and 'ip daddr 168.63.129.16 accept comment "azure"' not in rules


class Sink(Component):
    """A component whose outbound() is a peer port and the host it ships to."""

    host: str | Host = "logs.example:19532"

    def compose(self) -> Iterator[object]:
        yield from ()

    def outbound(self) -> Egress:
        return Egress(ports=[UDP(30303)], hosts=[self.host])


@pytest.mark.parametrize(
    ("host", "rule"),
    [
        (
            "logs.example:19532",
            'ip daddr @host4_logs.example tcp dport 19532 accept comment "sink"',
        ),
        (
            "logs.example:19532",
            'ip6 daddr @host6_logs.example tcp dport 19532 accept comment "sink"',
        ),
        ("10.0.0.5:8545", 'ip daddr 10.0.0.5 tcp dport 8545 accept comment "sink"'),
        ("[fd00::5]:8545", 'ip6 daddr fd00::5 tcp dport 8545 accept comment "sink"'),
        ("10.0.0.0/8", 'ip daddr 10.0.0.0/8 accept comment "sink"'),
        (
            Host("logs.example", ["10.0.0.5", "fd00::5"], port=19532),
            'ip daddr 10.0.0.5 tcp dport 19532 accept comment "sink (logs.example)"',
        ),
        (
            Host("logs.example", ["10.0.0.5", "fd00::5"], port=19532),
            'ip6 daddr fd00::5 tcp dport 19532 accept comment "sink (logs.example)"',
        ),
        (
            Host("logs.example", ["10.0.0.5"]),
            'ip daddr 10.0.0.5 accept comment "sink (logs.example)"',
        ),
    ],
)
def test_a_components_outbound_joins_the_image_egress(host: str | Host, rule: str) -> None:
    egress = Network(egress=Egress(ports=[UDP(30303)]))
    filtered = Image("x", components=[Sink(host=host)], network=egress)
    rules = plan(filtered, "production").file("/etc/nftables.conf").text
    assert rule in rules and 'udp dport 30303 accept comment "image, sink"' in rules
    assert ("host4_logs.example" in rules) == (host == "logs.example:19532")
    unfiltered = plan(Image("x", components=[Sink(host=host)]), "production")
    assert "30303" not in unfiltered.file("/etc/nftables.conf").text


def test_a_host_name_is_reached_only_at_the_addresses_it_resolves_to() -> None:
    planned = plan(Image("x", components=[Sink()], network=Network(egress=Egress())), "production")
    rules = planned.file("/etc/nftables.conf").text
    assert "set host4_logs.example {\n\t\ttype ipv4_addr\n\t}" in rules
    assert [
        line for line in rules.splitlines() if "dport 19532" in line and "@host" not in line
    ] == []
    assert planned.file("/etc/tundravm/egress-hosts").text.splitlines() == [
        "time.cloudflare.com",
        "nts.netnod.se",
        "logs.example",
    ]
    timer = planned.file("/usr/lib/systemd/system/egress-resolve.timer").text
    assert "OnUnitActiveSec=30s" in timer
    unfiltered = plan(Image("x", components=[Sink()]), "production")
    assert "host4_" not in unfiltered.file("/etc/nftables.conf").text


@pytest.mark.parametrize(
    ("host", "waits"),
    [
        ("api.example:443", True),
        ("10.0.0.5:443", False),
        (Host("api.example", ["10.0.0.5"], port=443), False),
    ],
)
def test_a_service_whose_egress_names_a_host_starts_after_the_first_resolution(
    host: str | Host, waits: bool
) -> None:
    app = Service("app", ["/usr/bin/app"], egress=Egress(hosts=[host]))
    unit = plan(Image("x", services=[app], network=Network(egress=Egress())), "production")
    wants, after = (unit.service("app").get("Unit", key) or "" for key in ("Wants", "After"))
    assert ("egress-resolve.service" in wants) == waits == ("egress-resolve.service" in after)
    assert "egress-resolve.service" not in (unit.service("app").get("Unit", "Requires") or "")


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


PEER = Host("api.example", ["203.0.113.7", "203.0.113.8"], port=443, aliases=["rpc.example"])


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Host("api example", ["203.0.113.7"]), "not a host name"),
        (lambda: Host("203.0.113.7", ["203.0.113.7"]), "not a host name"),
        (lambda: Host("api.example", "203.0.113.7"), "takes a list of addresses"),
        (lambda: Host("api.example", []), "addresses= is empty"),
        (lambda: Host("api.example", ["203.0.113.0/24"]), "not an IP address"),
        (lambda: Host("api.example", ["peer.example"]), "not an IP address"),
        (lambda: Host("api.example", ["203.0.113.7", "203.0.113.7"]), "an address twice"),
        (lambda: Host("api.example", ["203.0.113.7"], port=0), "not a port in 1..65535"),
        (lambda: Host("api.example", ["203.0.113.7"], port=True), "not a port in 1..65535"),
        (lambda: Host("api.example", ["203.0.113.7"], aliases="rpc.example"), "takes a list"),
        (lambda: Host("api.example", ["203.0.113.7"], aliases=["api.example"]), "not another"),
        (lambda: Host("api.example", ["203.0.113.7"], aliases=["10.0.0.5"]), "not another"),
        (
            lambda: Egress(hosts=[Host("metadata.google.internal", ["169.254.169.254"])]),
            "cloud metadata server",
        ),
        (lambda: Egress(hosts=[443]), "not a host"),  # type: ignore[list-item]
    ],
)
def test_a_host_pins_a_name_to_addresses_or_is_refused(
    make: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as raised:
        make()
    assert raised.value.hint
    assert PEER.names == ("api.example", "rpc.example")
    assert PEER.entries == ("203.0.113.7:443", "203.0.113.8:443")


OTHER = Host("api.example", ["203.0.113.9"], port=443)
OTHER_PEER = Host("logs.example", ["fd00::9"], port=19532)
WINDOW = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[TCP(22)])})


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (
            lambda: Image(
                "x",
                network=Network(egress=Egress(hosts=[PEER])),
                services=[Service("app", ["/usr/bin/app"], egress=Egress(hosts=[OTHER]))],
            ),
            "Service\\('app'\\) pins api.example to 203.0.113.9, and Network\\(egress=\\) pins it",
        ),
        (
            lambda: Image(
                "x",
                network=Network(egress=Egress(hosts=[PEER])),
                components=[Firewall(modes={"production": Mode(egress=Egress(hosts=[OTHER]))})],
            ),
            "Firewall mode 'production' pins api.example",
        ),
        (
            lambda: Image(
                "x",
                network=Network(egress=Egress(hosts=["api.example:443"])),
                services=[Service("app", ["/usr/bin/app"], egress=Egress(hosts=[PEER]))],
            ),
            "names it as 'api.example:443', which DNS would resolve",
        ),
        (
            lambda: Image(
                "x",
                network=Network(egress=Egress(hosts=[PEER])),
                profiles={"ops": Profile(add=[Sink(host="rpc.example:19532")])},
            ),
            "names it as 'rpc.example:19532'",
        ),
    ],
)
def test_one_image_pins_a_name_once_and_never_leaves_it_to_dns(
    image: Callable[[], Image], message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as raised:
        plan(image())
    assert raised.value.hint


def test_pinned_hosts_resolve_in_the_images_hosts_file() -> None:
    mode = Mode(egress=Egress(hosts=[Host("deb.example", ["203.0.113.20"], port=443)]))
    image = Image(
        "x",
        network=Network(egress=Egress(hosts=[PEER, "10.0.0.5:8545"])),
        components=[Firewall(modes={"production": Mode(), "maintenance": mode})],
        services=[Service("app", ["/usr/bin/app"], egress=Egress(hosts=[PEER, OTHER_PEER]))],
    )
    planned = plan(image, "production")
    assert planned.file("/etc/hosts").text == "".join(f"{line}\n" for line in HOSTS_HEADER) + (
        "203.0.113.7 api.example rpc.example\n"
        "203.0.113.8 api.example rpc.example\n"
        "203.0.113.20 deb.example\n"
        "fd00::9 logs.example\n"
    )
    assert (
        "ip daddr 203.0.113.20 tcp dport 443 ct mark set 0x00000002 accept comment "
        '"maintenance mode (deb.example)"'
    ) in planned.file("/etc/nftables.conf").text
    assert (
        "/etc/hosts" not in plan(Image("x", network=Network(egress=Egress())), "production").files
    )


def test_a_network_takes_dhcp_or_a_static_address() -> None:
    with pytest.raises(ValidationError) as caught:
        Network(dhcp=False)
    assert "neither is set" in str(caught.value)


@pytest.mark.parametrize(
    ("key", "section"),
    [("Hostname", "Content"), ("ImageVersion", "Output"), ("CacheOnly", "Build"),
     ("Checksum", "Validation")],
)  # fmt: skip
def test_mkosi_settings_go_to_their_section_and_scripts_are_recorded(
    key: str, section: str
) -> None:
    image = Image("x", mkosi=Mkosi(settings={key: "demo"}, scripts=[("postinst", "echo hi")]))
    planned = plan(image, "production")
    assert planned.settings(section)[key] == ("demo",)
    assert [hook.script for hook in planned.hooks("postinst")][-1:] == ["echo hi"]


@pytest.mark.parametrize(
    ("key", "slot"),
    [
        ("KernelCommandLine", "Kernel(cmdline="),
        ("Packages", "Image(packages="),
        ("Seed", "tundravm sets"),
        ("ExtraTrees", "Image(files="),
        ("PostInstallationScripts", "Mkosi(scripts="),
        ("Hostnme", "spelling"),
    ],
)
def test_a_setting_tundravm_writes_names_the_public_slot(key: str, slot: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Mkosi(settings={key: "x"})
    assert key in str(caught.value) and slot in (caught.value.hint or "")
    assert not [hint for hint in RESERVED_SETTINGS.values() if "Setting(" in hint]


def test_an_unknown_mkosi_script_stage_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Mkosi(scripts=[("whenever", "echo hi")])
    assert "postinst" in str(caught.value)
