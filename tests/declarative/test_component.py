"""@component: plain annotated classes as fragments, and the exports they publish."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError

import pytest

from tundravm import component
from tundravm.declarative import Diagnostic, File, Fragment, Package, Resolved
from tundravm.declarative.utils import Composite, ConfigFile, HttpEndpoint
from tundravm.errors import ValidationError
from tundravm.testing import assert_clean, assert_diagnostic, fragment_plan


@component
class WebApp(Composite):
    """A web app listening on *port*; exports :attr:`http`."""

    port: int = 8080
    version: str
    ratio: float = 1.0
    motd: str | None = None
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = {}  # noqa: RUF012 (a component freezes it)

    @property
    def http(self) -> HttpEndpoint:
        return HttpEndpoint("127.0.0.1", self.port, "/health", provider=self.name)

    def compose(self) -> Iterator[Package | File]:
        yield Package("webapp")
        yield ConfigFile.env("/etc/webapp.env", {"PORT": self.port, "VERSION": self.version})

    def check(self, resolved: Resolved) -> Iterator[Diagnostic]:
        if self.port < 1024:
            yield Diagnostic("webapp-port", "error", f"port {self.port} is privileged")


@component(requires=("base-tools",))
class Probe(Composite):
    target: HttpEndpoint

    def compose(self) -> tuple[Package, ...]:
        return (Package("curl"),)


BASE_TOOLS = Fragment("base-tools", items=(Package("jq"),))


def test_a_component_is_a_frozen_keyword_only_fragment() -> None:
    app = WebApp(version="1.0", flags=["a"], env={"A": "b"})  # type: ignore[arg-type]
    assert isinstance(app, Fragment) and app.name == "web-app"
    assert app.items == (
        Package("webapp"),
        File.text("/etc/webapp.env", "PORT=8080\nVERSION=1.0\n"),
    )
    assert app.flags == ("a",) and dict(app.env) == {"A": "b"}
    assert repr(app).startswith("WebApp(port=8080, version='1.0'")
    assert app == WebApp(version="1.0", flags=("a",), env={"A": "b"}) and hash(app)
    assert not hasattr(app, "__dict__")
    with pytest.raises(FrozenInstanceError):
        app.port = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        WebApp("1.0")  # type: ignore[misc, arg-type, call-arg]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"version": 1}, "version must be str, got 1"),
        ({"version": "1", "port": True}, "port must be int, got True"),
        ({"version": "1", "port": "80"}, "port must be int"),
        ({"version": "1", "motd": 3}, "motd must be str or None"),
    ],
)
def test_primitive_fields_refuse_other_types(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        WebApp(**kwargs)  # type: ignore[arg-type]


def test_float_fields_take_ints() -> None:
    assert WebApp(version="1", ratio=2).ratio == 2


def test_exports_require_their_provider() -> None:
    app = WebApp(version="1.0")
    probe = Probe(target=app.http)
    assert probe.target.url == "http://127.0.0.1:8080/health"
    assert probe.requires == ("base-tools", "web-app")
    assert_clean(fragment_plan(Fragment("both", items=(app, probe, BASE_TOOLS))))
    alone = fragment_plan(Fragment("alone", items=(probe, BASE_TOOLS)))
    assert_diagnostic(alone, "fragment-requires-missing", variant="default")


def test_check_method_is_the_fragments_lint_rule() -> None:
    assert_diagnostic(fragment_plan(WebApp(version="1", port=80)), "webapp-port", variant="default")
    assert_clean(fragment_plan(WebApp(version="1")))


def test_names_come_from_the_decorator_an_id_field_or_the_class() -> None:
    @component(name="named")
    class Named(Composite):
        def compose(self) -> list[Package]:
            return [Package("a")]

    @component
    class Plain:
        id: str

        def compose(self) -> list[Package]:
            return [Package("b")]

    assert Named().name == "named"
    plain: object = Plain(id="worker-1")
    assert isinstance(plain, Composite) and plain.name == "worker-1"


def test_compose_may_still_return_a_fragment() -> None:
    @component
    class Legacy(Composite):
        def compose(self) -> Fragment:
            return Fragment("legacy", items=(Package("a"),), requires=("x",))

    assert (Legacy().name, Legacy().requires) == ("legacy", ("x",))


def test_compose_is_required_and_must_return_items() -> None:
    with pytest.raises(ValidationError, match="has no compose"):

        @component
        class Empty(Composite):
            port: int = 1

    @component
    class Wrong(Composite):
        def compose(self) -> str:  # type: ignore[override]
            return "nope"

    with pytest.raises(ValidationError, match="not items or a Fragment"):
        Wrong()


def test_http_endpoint() -> None:
    assert HttpEndpoint("::1", 443, scheme="https").url == "https://[::1]:443/"
    assert HttpEndpoint("10.0.0.2", 9090, "/metrics").address == "10.0.0.2:9090"
    for host, port, path in (("", 1, "/"), ("a", 0, "/"), ("a", 1, "x"), ("a b", 1, "/")):
        with pytest.raises(ValidationError):
            HttpEndpoint(host, port, path)
