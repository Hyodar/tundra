"""@component: plain annotated classes as fragments, and the exports they publish."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError

import pytest

from tundravm.declarative import (
    Diagnostic,
    File,
    Fragment,
    Package,
    Recipe,
    Resolved,
    Variant,
    component,
)
from tundravm.declarative.resolve import lint
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


@component
class Bound(Composite):
    """A provider whose export names the instance itself."""

    port: int = 8080

    @property
    def http(self) -> HttpEndpoint:
        return HttpEndpoint("127.0.0.1", self.port, provider=self)

    def compose(self) -> tuple[File, ...]:
        return (File("/etc/bound.port", f"{self.port}\n"),)


BOUND = Bound()
WEB_APP = WebApp(version="1.0")


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


def test_check_method_is_the_fragments_lint_rule() -> None:
    assert_diagnostic(fragment_plan(WebApp(version="1", port=80)), "webapp-port", profile="default")
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


def _export(provider: WebApp | Bound) -> tuple[object, ...]:
    """What a probe of *provider*'s export holds: the target, its url and origin, requires."""
    probe = Probe(target=provider.http)
    return probe.target, probe.target.url, probe.target.origin is provider, probe.requires


def _findings(*items: Fragment) -> list[tuple[str, str, str]]:
    """The error and warning findings of *items* planned together."""
    plan = fragment_plan(Fragment("exports", items=items))
    findings = plan.lint()
    return [(d.variant, d.code, d.severity) for d in findings if d.severity != "info"]


def _stale() -> dict[str, Diagnostic]:
    """``export-stale`` by profile, for a probe of :data:`BOUND` under three profiles."""
    edit = File.ref("/etc/bound.port").edit(content="9090\n")
    recipe = Recipe(
        "r",
        items=(BASE_TOOLS, Probe(target=BOUND.http)),
        variants=(
            Variant("same", add=BOUND),
            Variant("other", add=Bound(port=9090)),
            Variant("edited", inherits="same", changes=(edit,)),
        ),
    )
    return {d.variant: d for d in lint(recipe) if d.code == "export-stale"}


@pytest.mark.parametrize(
    ("observe", "expected"),
    [
        (lambda: HttpEndpoint("::1", 443, scheme="https").url, "https://[::1]:443/"),
        (lambda: HttpEndpoint("10.0.0.2", 9090, "/metrics").address, "10.0.0.2:9090"),
        (lambda: HttpEndpoint("", 1, "/"), ValidationError),
        (lambda: HttpEndpoint("a", 0, "/"), ValidationError),
        (lambda: HttpEndpoint("a", 1, "x"), ValidationError),
        (lambda: HttpEndpoint("a b", 1, "/"), ValidationError),
        (
            lambda: _export(WEB_APP),
            (
                HttpEndpoint("127.0.0.1", 8080, "/health", provider="web-app"),
                "http://127.0.0.1:8080/health",
                False,
                ("base-tools", "web-app"),
            ),
        ),
        (lambda: _findings(WEB_APP, Probe(target=WEB_APP.http), BASE_TOOLS), []),
        (
            lambda: _findings(Probe(target=WEB_APP.http), BASE_TOOLS),
            [("default", "fragment-requires-missing", "error")],
        ),
        (
            lambda: _export(BOUND),
            (
                HttpEndpoint("127.0.0.1", 8080, provider="bound"),
                "http://127.0.0.1:8080/",
                True,
                ("base-tools", "bound"),
            ),
        ),
        (
            lambda: {variant: d.severity for variant, d in _stale().items()},
            {"other": "error", "edited": "warning"},
        ),
        (lambda: "Bound(port=9090)" in _stale()["other"].message, True),
        (
            lambda: "changes File(extra, /etc/bound.port) inside it" in _stale()["edited"].message,
            True,
        ),
    ],
    ids=[
        "url",
        "address",
        "refuses-empty-host",
        "refuses-port-0",
        "refuses-relative-path",
        "refuses-host-with-space",
        "export",
        "provider-present-is-clean",
        "provider-missing",
        "bound-export",
        "stale-severities",
        "stale-for-another-instance",
        "stale-for-an-edit-inside-it",
    ],
)
def test_exports_follow_their_provider(observe: Callable[[], object], expected: object) -> None:
    if expected is ValidationError:
        with pytest.raises(ValidationError):
            observe()
    else:
        assert observe() == expected
