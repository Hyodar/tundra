"""``File.text``/``File.copy``, ``Unit.from_file``, ``GoBinary``/``CargoBinary``.

Each spelling lowers exactly like the declaration it stands for: equal objects,
equal recipe digests, equal compiled trees.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import tundravm.declarative
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
    Local,
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
    assert tundravm.declarative.compile(first).digest == tundravm.declarative.compile(second).digest


@pytest.mark.parametrize(
    ("content", "spelled", "built"),
    [
        (
            "port = 1\n",
            lambda source: File.text("/etc/motd", "hi\n", mode=0o600),
            lambda source: File("/etc/motd", "hi\n", mode=0o600),
        ),
        (
            "port = 1\n",
            lambda source: File.text("/etc/motd", "hi\n"),
            lambda source: File("/etc/motd", "hi\n"),
        ),
        (
            "port = 1\n",
            lambda source: File.copy("/etc/a", source, mode=0o644),
            lambda source: File("/etc/a", source),
        ),
        (
            "[Service]\nExecStart=/bin/true\n",
            lambda source: Unit.from_file("app.service", source, enabled=True),
            lambda source: Unit("app.service", source, enabled=True),
        ),
    ],
    ids=["file-text-mode", "file-text", "file-copy", "unit-from-file"],
)
def test_spellings_are_the_constructor(
    tmp_path: Path,
    content: str,
    spelled: Callable[[Path], Declaration],
    built: Callable[[Path], Declaration],
) -> None:
    source = tmp_path / "source"
    source.write_text(content)
    assert spelled(source) == built(source)
    _same_lowering(_recipe(spelled(source)), _recipe(built(source)))


def test_file_copy_keeps_only_the_execute_bit(tmp_path: Path) -> None:
    script, data = tmp_path / "run.sh", tmp_path / "data"
    script.write_text("#!/bin/sh\n")
    data.write_text("x")
    script.chmod(0o775)
    data.chmod(0o664)
    tree = tundravm.declarative.compile(
        _recipe(File.copy("/usr/bin/run", script), File.copy("/d", data))
    )
    modes = {entry.path: entry.mode for entry in tree.entries}
    assert modes["payload/mkosi.extra/usr/bin/run"] == 0o755
    assert modes["payload/mkosi.extra/d"] == 0o644


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


def test_go_binary_lowers_like_the_hand_written_build() -> None:
    by_hand = Build(
        "node-exporter",
        TARBALL,
        using=Go(output="node_exporter", package=".", env={"CGO_ENABLED": "0"}),
        install=(Install("build/node_exporter", "/usr/bin/node_exporter"),),
        cache_namespace="node-exporter-1.8.2",
    )
    binary = GoBinary(
        TARBALL,
        name="node-exporter",
        install="/usr/bin/node_exporter",
        env={"CGO_ENABLED": "0"},
        cache_namespace="node-exporter-1.8.2",
    )
    assert binary.items == (by_hand,)
    assert binary.executable == "/usr/bin/node_exporter"
    service = Service("node-exporter", f"{binary.executable} --web.listen-address=:9100")
    _same_lowering(_recipe(binary, service), _recipe(by_hand, service))


def test_go_binary_installs_at_usr_bin_name_by_default() -> None:
    binary = GoBinary(TARBALL, package="./cmd/tool", tags=("netgo",))
    (build,) = binary.items
    assert isinstance(build, Build) and build.install == (Install("build/tool", "/usr/bin/tool"),)
    assert binary.executable == "/usr/bin/tool" and binary.name == "tool"
    with pytest.raises(ValidationError, match="absolute file path"):
        GoBinary(TARBALL, name="tool", install="bin/tool")


def test_editing_the_build_behind_an_executable_is_reported() -> None:
    from tundravm.declarative import Variant
    from tundravm.declarative.resolve import lint

    binary = GoBinary(TARBALL, name="tool")
    service = Service("tool", f"{binary.executable} --serve")
    moved = Install("build/tool", "/opt/tool")
    variants = (
        Variant("default"),
        Variant("moved", changes=(Build.ref("tool").edit(install=(moved,)),)),
        Variant("gone", changes=(Build.ref("tool").remove(),)),
        Variant(
            "followed",
            changes=(
                Build.ref("tool").edit(install=(moved,)),
                service.ref.edit(exec_start="/opt/tool --serve"),
            ),
        ),
    )
    recipe = Recipe("r", items=(binary, service), variants=variants)
    stale = {d.profile: d.message for d in lint(recipe) if d.code == "executable-stale"}
    assert set(stale) == {"moved", "gone"}
    assert "runs /usr/bin/tool, which Build 'tool' no longer installs" in stale["moved"]
    assert "edited in profile 'moved'" in stale["moved"]
    assert "removed in profile 'gone'" in stale["gone"]


def test_cargo_binary_lowers_like_the_hand_written_build() -> None:
    source = Git("https://github.com/example/prover", "v1.0.0")
    binary = CargoBinary(source, bin="prover-host", name="prover", install="/usr/bin/prover")
    by_hand = Build(
        "prover",
        source,
        using=Cargo(output="prover", bin="prover-host"),
        install=(Install("target/release/prover-host", "/usr/bin/prover"),),
    )
    assert binary.items == (by_hand,)
    _same_lowering(_recipe(binary), _recipe(by_hand))


SINK = Local("../fixtures/sink")
APP = Git("https://github.com/acme/app.git", "v1.0.0")


@pytest.mark.parametrize(
    ("kind", "source", "keywords", "name", "refused"),
    [
        (GoBinary, SINK, {}, "sink", None),
        (GoBinary, SINK, {"package": "./cmd/delay-pipe"}, "delay-pipe", None),
        (GoBinary, APP, {}, "app", None),
        (GoBinary, Git("git@github.com:acme/app.git", "v1.0.0"), {}, "app", None),
        (GoBinary, Git("https://github.com/acme/tools", "v1", subdir="cmd/sync"), {}, "sync", None),
        (GoBinary, APP, {"package": "github.com/acme/app/cmd/tool/v2"}, "tool", None),
        (GoBinary, SINK, {"package": "./cmd/x", "name": "other"}, "other", None),
        (CargoBinary, APP, {"bin": "app-cli"}, "app-cli", None),
        (CargoBinary, APP, {"package": "app-core"}, "app-core", None),
        (CargoBinary, SINK, {}, "sink", None),
        (GoBinary, TARBALL, {}, None, "a download names no directory"),
        (GoBinary, Local("."), {}, None, "build a named directory"),
        (GoBinary, "app", {}, None, 'GoBinary(Local("app"))'),
    ],
)
def test_a_built_binary_names_itself_after_its_package_or_source(
    kind: type[GoBinary | CargoBinary],
    source: object,
    keywords: dict[str, str],
    name: str | None,
    refused: str | None,
) -> None:
    if refused is not None:
        with pytest.raises(ValidationError) as caught:
            kind(source, **keywords)  # type: ignore[arg-type]
        assert refused in str(caught.value)
        return
    binary = kind(source, **keywords)  # type: ignore[arg-type]
    assert (binary.name, binary.executable) == (name, f"/usr/bin/{name}")
