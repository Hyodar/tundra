"""``File.text``/``File.copy``, ``Unit.from_file``, ``GoBinary``/``CargoBinary``.

Each spelling lowers exactly like the declaration it stands for: equal objects,
equal recipe digests, equal compiled trees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tundravm
from tundravm.declarative import (
    Build,
    Cargo,
    Declaration,
    File,
    Fragment,
    Git,
    Go,
    Http,
    Install,
    Package,
    Recipe,
    Service,
    Unit,
    lower,
)
from tundravm.declarative.utils import CargoBinary, GoBinary
from tundravm.errors import ValidationError

TARBALL = Http("https://example.com/node_exporter-1.8.2.tar.gz")


def _recipe(*items: Declaration | Fragment) -> Recipe:
    return Recipe(name="helpers", items=(Package("systemd"), *items))


def _same_lowering(first: Recipe, second: Recipe) -> None:
    assert lower(first).digest() == lower(second).digest()
    assert tundravm.compile(first).digest == tundravm.compile(second).digest


def test_file_text_and_copy_are_the_constructor(tmp_path: Path) -> None:
    source = tmp_path / "app.conf"
    source.write_text("port = 1\n")
    assert File.text("/etc/motd", "hi\n", mode=0o600) == File("/etc/motd", "hi\n", mode=0o600)
    assert File.copy("/etc/app.conf", source, mode=0o644) == File("/etc/app.conf", source)
    _same_lowering(
        _recipe(File.text("/etc/motd", "hi\n"), File.copy("/etc/a", source, mode=0o644)),
        _recipe(File("/etc/motd", "hi\n"), File("/etc/a", source)),
    )


def test_file_copy_keeps_only_the_execute_bit(tmp_path: Path) -> None:
    script, data = tmp_path / "run.sh", tmp_path / "data"
    script.write_text("#!/bin/sh\n")
    data.write_text("x")
    script.chmod(0o775)
    data.chmod(0o664)
    tree = tundravm.compile(_recipe(File.copy("/usr/bin/run", script), File.copy("/d", data)))
    modes = {entry.path: entry.mode for entry in tree.entries}
    assert modes["default/mkosi.extra/usr/bin/run"] == 0o755
    assert modes["default/mkosi.extra/d"] == 0o644


@pytest.mark.parametrize(
    "build",
    [
        lambda: File.text("/etc/x", b"bytes"),  # type: ignore[arg-type]
        lambda: File.copy("/etc/x", "relative/path"),  # type: ignore[arg-type]
        lambda: File("/etc/x", "text", mode=None),
        lambda: Unit.from_file("app.service", "[Unit]\n"),  # type: ignore[arg-type]
    ],
)
def test_misused_spellings_are_refused(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_unit_from_file_is_the_constructor(tmp_path: Path) -> None:
    source = tmp_path / "app.service"
    source.write_text("[Service]\nExecStart=/bin/true\n")
    assert Unit.from_file("app.service", source, enabled=True) == Unit(
        "app.service", source, enabled=True
    )


def test_go_binary_lowers_like_the_hand_written_build() -> None:
    by_hand = Build(
        "node-exporter",
        TARBALL,
        using=Go(output="node_exporter", package=".", env={"CGO_ENABLED": "0"}),
        install=(Install("build/node_exporter", "/usr/bin/node_exporter"),),
        cache_namespace="node-exporter-1.8.2",
    )
    binary = GoBinary(
        "node-exporter",
        TARBALL,
        package=".",
        install="/usr/bin/node_exporter",
        env={"CGO_ENABLED": "0"},
        cache_namespace="node-exporter-1.8.2",
    )
    assert binary.items == (by_hand,)
    assert binary.executable == "/usr/bin/node_exporter"
    service = Service("node-exporter", f"{binary.executable} --web.listen-address=:9100")
    _same_lowering(_recipe(binary, service), _recipe(by_hand, service))


def test_go_binary_installs_at_usr_bin_name_by_default() -> None:
    binary = GoBinary("tool", TARBALL, package="./cmd/tool", tags=("netgo",))
    (build,) = binary.items
    assert isinstance(build, Build) and build.install == (Install("build/tool", "/usr/bin/tool"),)
    assert binary.executable == "/usr/bin/tool" and binary.name == "tool"
    with pytest.raises(ValidationError, match="absolute file path"):
        GoBinary("tool", TARBALL, package=".", install="bin/tool")


def test_cargo_binary_lowers_like_the_hand_written_build() -> None:
    source = Git("https://github.com/taikoxyz/raiko", "v1.0.0")
    binary = CargoBinary("raiko", source, bin="raiko-host", install="/usr/bin/raiko")
    by_hand = Build(
        "raiko",
        source,
        using=Cargo(output="raiko", bin="raiko-host"),
        install=(Install("target/release/raiko-host", "/usr/bin/raiko"),),
    )
    assert binary.items == (by_hand,)
    _same_lowering(_recipe(binary), _recipe(by_hand))
