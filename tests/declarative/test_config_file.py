"""ConfigFile renders data deterministically and lowers to the hand-written File."""

from __future__ import annotations

import json
import tomllib

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
    assert lock(_recipe(rendered)).recipe_digest == lock(_recipe(by_hand)).recipe_digest


def test_yaml_quotes_strings_another_type_would_claim() -> None:
    words = ["yes", "On", "8080", "1e3", "0x1F", "2001-12-14", ".inf", "", "a b", "x:", "-v"]
    plain = ["15s", "v1.2", "/usr/bin/app", "node", "localhost:9100"]
    text = ConfigFile.yaml("/etc/a.yml", {"q": words, "p": plain}).content
    quoted = ", ".join(json.dumps(word) for word in words)
    assert text == f"p: [{', '.join(plain)}]\nq: [{quoted}]\n"


def test_yaml_scalars_and_nesting() -> None:
    data: dict[str, object] = {
        "f": 1e-05,
        "big": 1e16,
        "t": True,
        "nil": None,
        "e": {},
        "l": [],
        "nested": [[1, 2], {}],
    }
    assert ConfigFile.yaml("/a.yml", data).content == (
        "big: 1.0e+16\ne: {}\nf: 1.0e-05\nl: []\nnested:\n  - [1, 2]\n  - {}\nnil: null\nt: true\n"
    )


def test_insertion_order_on_request() -> None:
    data = {"b": 1, "a": 2}
    assert ConfigFile.yaml("/a.yml", data).content == "a: 2\nb: 1\n"
    assert ConfigFile.yaml("/a.yml", data, sort_keys=False).content == "b: 1\na: 2\n"
    assert (
        ConfigFile.json("/a.json", data, sort_keys=False).content == '{\n  "b": 1,\n  "a": 2\n}\n'
    )


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


def test_json_ini_and_env() -> None:
    assert ConfigFile.json("/a.json", {"b": [1.5, None], "a": "é"}).content == (
        '{\n  "a": "é",\n  "b": [\n    1.5,\n    null\n  ]\n}\n'
    )
    unit = {"Unit": {"Description": "app", "After": ["a.service", "b.service"]}, "Install": {}}
    assert ConfigFile.ini("/a.conf", unit, sort_keys=False).content == (
        "[Unit]\nDescription = app\nAfter = a.service\nAfter = b.service\n\n[Install]\n"
    )
    env = {"ARGS": "--listen=127.0.0.1:9090", "NOTE": 'say "hi" $USER', "N": 3, "ON": True}
    assert ConfigFile.env("/etc/default/app", env, sort_keys=False).content == (
        'ARGS="--listen=127.0.0.1:9090"\nNOTE="say \\"hi\\" \\$USER"\nN=3\nON=true\n'
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
