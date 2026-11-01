"""ConfigFile renders data deterministically and lowers to the hand-written File."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable

import pytest

from tundravm.declarative import File, Fragment, Package, Recipe, Variant, compile, lock
from tundravm.declarative.utils import ConfigFile
from tundravm.errors import ValidationError

PROMETHEUS = {
    "scrape_configs": [{"job_name": "node", "static_configs": [{"targets": ["127.0.0.1:9100"]}]}],
    "global": {"scrape_interval": "15s", "evaluation_interval": 30},
}
PROMETHEUS_YAML = """\
global:
  evaluation_interval: 30
  scrape_interval: 15s
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["127.0.0.1:9100"]
"""
AMBIGUOUS = ["yes", "On", "8080", "1e3", "0x1F", "2001-12-14", ".inf", "", "a b", "x:", "-v"]
PLAIN = ["15s", "v1.2", "/usr/bin/app", "node", "localhost:9100"]
UNIT = {"Unit": {"Description": "app", "After": ["a.service", "b.service"]}, "Install": {}}
ENV = {"ARGS": "--listen=127.0.0.1:9090", "NOTE": 'say "hi" $USER', "N": 3, "ON": True}
UNORDERED = {"b": 1, "a": 2}
INSERTION_ORDER = {"sort_keys": False}


def _recipe(item: File) -> Recipe:
    return Recipe(
        name="config",
        items=(Package("linux-image-amd64"), Fragment("config", items=(item,))),
        variants=(Variant("default", outputs=("qemu",)),),
    )


def test_yaml_is_the_hand_written_file() -> None:
    rendered = ConfigFile.yaml("/etc/prometheus/prometheus.yml", PROMETHEUS)
    by_hand = File.text("/etc/prometheus/prometheus.yml", PROMETHEUS_YAML)
    assert rendered == by_hand
    assert compile(_recipe(rendered)).digest == compile(_recipe(by_hand)).digest
    assert lock(_recipe(rendered)).image_digest == lock(_recipe(by_hand)).image_digest


@pytest.mark.parametrize(
    ("render", "path", "data", "options", "text"),
    [
        (
            ConfigFile.yaml,
            "/etc/a.yml",
            {"q": AMBIGUOUS, "p": PLAIN},
            {},
            f"p: [{', '.join(PLAIN)}]\nq: [{', '.join(json.dumps(word) for word in AMBIGUOUS)}]\n",
        ),
        (
            ConfigFile.yaml,
            "/a.yml",
            {
                "f": 1e-05,
                "big": 1e16,
                "t": True,
                "nil": None,
                "e": {},
                "l": [],
                "nested": [[1, 2], {}],
            },
            {},
            "big: 1.0e+16\ne: {}\nf: 1.0e-05\nl: []\n"
            "nested:\n  - [1, 2]\n  - {}\nnil: null\nt: true\n",
        ),
        (ConfigFile.yaml, "/a.yml", UNORDERED, {}, "a: 2\nb: 1\n"),
        (ConfigFile.yaml, "/a.yml", UNORDERED, INSERTION_ORDER, "b: 1\na: 2\n"),
        (ConfigFile.json, "/a.json", UNORDERED, INSERTION_ORDER, '{\n  "b": 1,\n  "a": 2\n}\n'),
        (
            ConfigFile.json,
            "/a.json",
            {"b": [1.5, None], "a": "é"},
            {},
            '{\n  "a": "é",\n  "b": [\n    1.5,\n    null\n  ]\n}\n',
        ),
        (
            ConfigFile.ini,
            "/a.conf",
            UNIT,
            INSERTION_ORDER,
            "[Unit]\nDescription = app\nAfter = a.service\nAfter = b.service\n\n[Install]\n",
        ),
        (
            ConfigFile.env,
            "/etc/default/app",
            ENV,
            INSERTION_ORDER,
            'ARGS="--listen=127.0.0.1:9090"\nNOTE="say \\"hi\\" \\$USER"\nN=3\nON=true\n',
        ),
    ],
    ids=[
        "yaml-quotes-ambiguous-strings",
        "yaml-scalars-and-nesting",
        "yaml-sorted-keys",
        "yaml-insertion-order",
        "json-insertion-order",
        "json-unicode-and-null",
        "ini-repeated-keys",
        "env-quoting",
    ],
)
def test_each_format_renders_its_text(
    render: Callable[..., File],
    path: str,
    data: object,
    options: dict[str, bool],
    text: str,
) -> None:
    assert render(path, data, **options).content == text


def test_toml_tables_and_round_trip() -> None:
    data = {
        "title": "node",
        "server": {"port": 8080, "tls": {"enabled": False}},
        "peers": [{"host": "a", "weight": 0.5}, {"host": "b", "tags": ["x"]}],
        "k.dotted": [1, {"inline": "yes"}],
    }
    text = ConfigFile.toml("/etc/node.toml", data, mode=0o600).content
    assert text == (
        '"k.dotted" = [1, { inline = "yes" }]\ntitle = "node"\n\n'
        '[[peers]]\nhost = "a"\nweight = 0.5\n\n[[peers]]\nhost = "b"\ntags = ["x"]\n\n'
        "[server]\nport = 8080\n\n[server.tls]\nenabled = false\n"
    )
    assert isinstance(text, str) and tomllib.loads(text) == data


def test_component_mappings_render_like_dicts() -> None:
    from types import MappingProxyType

    from tundravm._source import FrozenMap

    nested = FrozenMap({"b": MappingProxyType({"c": (1, 2)}), "a": FrozenMap({"d": True})})
    expected = {"b": {"c": [1, 2]}, "a": {"d": True}}
    assert json.loads(str(ConfigFile.json("/a.json", nested).content)) == expected
    assert json.loads(str(ConfigFile.json("/a.json", ({"x": nested},)).content)) == [
        {"x": expected}
    ]
    plain = {"b": {"c": [1, 2]}, "a": {"d": True}}
    for render in (ConfigFile.yaml, ConfigFile.toml):
        assert render("/a", nested).content == render("/a", plain).content
    sections = FrozenMap({"Unit": FrozenMap({"After": ("a.service", "b.service")})})
    assert (
        ConfigFile.ini("/a.conf", sections).content
        == ConfigFile.ini("/a.conf", {"Unit": {"After": ["a.service", "b.service"]}}).content
    )


@pytest.mark.parametrize(
    ("render", "message"),
    [
        (lambda: ConfigFile.toml("/a.toml", {"a": None}), "TOML has no null"),
        (lambda: ConfigFile.yaml("/a.yml", {"a": {1, 2}}), "cannot render"),
        (lambda: ConfigFile.json("/a.json", {1: "a"}), "must be a non-empty string"),  # type: ignore[dict-item]
        (lambda: ConfigFile.yaml("/a.yml", {"a": float("nan")}), "not a finite number"),
        (lambda: ConfigFile.env("/a", {"A-B": "x"}), "invalid variable name"),
        (lambda: ConfigFile.env("/a", {"A": "x\ny"}), "cannot render"),
        (lambda: ConfigFile.yaml("a.yml", {}), "must be an absolute path"),
        (lambda: ConfigFile.yaml("/a.yml", "text"), "must be a mapping or a list"),
    ],
)
def test_refuses_what_it_cannot_render(render: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        render()  # type: ignore[operator]
