"""``show``: an image as it resolves, a build read back, and ``show --diff`` of two builds."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.cli.conftest import STARTER, Cli
from tundravm._source import SourceBuild
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_OK
from tundravm.declarative import lifecycle
from tundravm.show import IMAGE_KEYS
from tundravm.testing import UPDATE_GOLDEN_ENV

GOLDEN = Path(__file__).parent
"""``show-<project>.txt``: ``tundravm show`` of what ``new`` writes and of the ``prover``
fixture (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites them)."""

INTERNAL = ("dev-access", "devtools", "app-ready", "SALT=", "mkosi-chroot", "Hooks")
"""What ``show`` never prints: internal fragment and unit names, and shell."""


@pytest.mark.parametrize("which", ["new", "prover"])
def test_show_reads_like_the_image_file(
    which: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: Cli,
    request: pytest.FixtureRequest,
) -> None:
    if which == "new":
        monkeypatch.chdir(tmp_path)
        assert cli("new", "demo").code == EXIT_OK
        monkeypatch.chdir(tmp_path / "demo")
    else:
        request.getfixturevalue("prover")
    result = cli("show")
    assert result.code == EXIT_OK, result.err
    golden = GOLDEN / f"show-{which}.txt"
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        golden.write_text(result.out, encoding="utf-8")
    assert result.out == golden.read_text(encoding="utf-8")
    for internal in INTERNAL:
        assert internal not in result.out, internal


@pytest.mark.parametrize(
    ("keywords", "block"),
    [
        ("", "  app\n    program    /usr/bin/app\n    hardening  static\n    probe      no\n"),
        (
            ", quotes=True",
            "  app\n    program    /usr/bin/app\n    hardening  static\n    probe      no\n"
            "    quotes     yes\n",
        ),
    ],
    ids=["plain", "quotes"],
)
def test_show_says_which_service_asks_for_quotes(
    keywords: str, block: str, tmp_path: Path, cli: Cli
) -> None:
    image = tmp_path / "q.py"
    image.write_text(
        "from tundravm import Image, Service\n"
        f'image = Image("q", services=[Service("app", ["/usr/bin/app"]{keywords})])\n',
        encoding="utf-8",
    )
    result = cli("show", image)
    assert result.code == EXIT_OK, result.err
    assert f"Services (1):\n{block}\n" in result.out


def test_show_selects_profiles(project: Path, cli: Cli) -> None:
    """--profile P picks some, --dev the dev profile, each shown whole."""
    dev = cli("show", "demo.py", "--dev")
    assert dev.code == EXIT_OK, dev.err
    assert dev.out == cli("show", "demo.py", "--profile", "dev").out
    assert "\nBuilt into dev:\n" in dev.out and "Built into production" not in dev.out
    twice = cli("show", "demo.py", "--dev", "--profile", "production")
    assert twice.code == EXIT_ERROR and "twice" in twice.err


def test_show_json_has_the_sections_show_prints(project: Path, cli: Cli) -> None:
    payload = json.loads(cli("show", "demo.py", "--json").out)
    assert set(payload) == set(IMAGE_KEYS)
    assert (payload["schema"], payload["image"], payload["file"]) == (
        "tundravm.show/1",
        "demo",
        "demo.py",
    )
    assert [row["name"] for row in payload["profiles"]] == ["production", "dev"]
    assert set(payload["resolved"]) == {"production", "dev"}


def test_show_json_carries_each_declarations_provenance(project: Path, cli: Cli) -> None:
    payload = json.loads(cli("show", "demo.py", "--json").out)
    rows = {row["declaration"]: row["origins"] for row in payload["provenance"]["production"]}
    (origin,) = rows["File('/srv/www/index.html')"]
    assert origin["action"] == "declared" and origin["location"].startswith("demo.py:")
    assert (origin["profile"], origin["components"]) == ("image", [])


def test_without_a_build_show_reads_the_project_image(project: Path, cli: Cli) -> None:
    assert cli("show").out == cli("show", "demo.py").out


def test_show_the_image_units_and_tree(project: Path, cli: Cli) -> None:
    units = cli("show", "demo.py", "--units", "--profile", "production")
    assert units.code == EXIT_OK
    assert "# payload/mkosi.extra/usr/lib/systemd/system/app.service" in units.out
    assert "ExecStart=/usr/bin/busybox httpd" in units.out
    assert units.out.endswith("\n# egress: not filtered (no Egress)\n")
    filtered = STARTER.replace("import DevAccess", "import Egress, Network")
    filtered = filtered.replace('dev=DevAccess(password="tdx")', "network=Network(egress=Egress())")
    (project / "filtered.py").write_text(filtered, encoding="utf-8")
    assert (
        "\n# egress\ntee-base  4460/tcp, 123/udp\nnetwork   1.1.1.1:853/tcp"
        in cli("show", "filtered.py", "--units").out
    )
    egress = json.loads(cli("show", "filtered.py", "--units", "--json").out)["egress"]
    assert egress["tee-base"][:2] == ["4460/tcp", "123/udp"]
    tree = json.loads(cli("show", "demo.py", "--tree", "--json").out)["tree"]
    paths = {row["path"] for row in tree}
    assert "mkosi.conf" in paths and "payload/mkosi.extra/srv/www/index.html" in paths


def test_an_image_has_no_measurements(project: Path, cli: Cli) -> None:
    result = cli("show", "demo.py", "--measurements")
    assert result.code == EXIT_ERROR
    assert "no measurements until it is built" in result.err


def test_show_a_build(built: Build, cli: Cli) -> None:
    result = cli("show", "latest")
    assert result.code == EXIT_OK, result.err
    first = result.out.splitlines()[0]
    assert first == f"build {built.id}  demo production (production)"
    assert "  signed        no" in result.out
    assert "reproduction  not attempted" in result.out
    report = json.loads(cli("show", built.id, "--json").out)
    assert report["schema"] == "tundravm.show/1"
    assert report["manifest"]["id"] == built.id and report["signed"] is False
    assert report["observations"]["id"] == built.id
    assert [row["name"] for row in report["report"]["artifacts"]] == ["payload.efi", "qemu.raw"]


GO_RELEASE = {
    "name": "go1.25.0",
    "module": "golang.org/toolchain@v0.0.1-go1.25.0.linux-amd64",
    "sum": "h1:toolchain=",
}
RUST_RELEASE = {
    "name": "1.91.0-x86_64-unknown-linux-gnu",
    "channel": "1.91.0",
    "host": "x86_64-unknown-linux-gnu",
    "rustc": "rustc 1.91.0 (f8297e351 2025-10-28)",
    "commit": "f8297e351a40c1439a467bbbb6879088047f50b3",
    "profile": "minimal",
    "components": [],
    "targets": [],
}
PROBE = """
from dataclasses import replace

from tundravm import Service
from tundravm.sources import CargoBinary, Git

probe = CargoBinary("probe", Git("https://example.com/probe.git", "b" * 40))
image = replace(image, services=[*image.services, Service("probe", [probe])])
"""


@pytest.mark.parametrize(
    ("added", "kind", "release", "shown"),
    [
        ("", "go", GO_RELEASE, ("tdxs", "go1.25.0")),
        (PROBE, "cargo", RUST_RELEASE, ("probe", "1.91.0")),
    ],
    ids=["go", "rust"],
)
def test_a_build_records_and_shows_each_sources_toolchain(
    project: Path,
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
    added: str,
    kind: str,
    release: dict[str, object],
    shown: tuple[str, str],
) -> None:
    """The toolchain the host prefetch recorded is the manifest's, by source, and show's
    sources table names its release."""
    with (project / "demo.py").open("a", encoding="utf-8") as demo:
        demo.write(added)

    def prefetch(build: SourceBuild, pin: str, checkout: Path, root: Path, **_: object) -> str:
        toolchain = release if build.build.kind == kind else {"name": "local", "reason": "-"}
        (root / "deps").mkdir(parents=True, exist_ok=True)
        marker = root / "deps" / str(build.deps_marker(pin))
        marker.write_text(json.dumps({"toolchain": toolchain}))
        return "prefetched"

    monkeypatch.setattr(lifecycle, "prefetch_deps", prefetch)
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    key = "go_toolchain" if kind == "go" else "rust_toolchain"
    sources = json.loads(cli("show", "latest", "--json").out)["manifest"]["sources"]
    assert [source[key] for source in sources if key in source] == [release]
    rows = [line.split() for line in cli("show", "latest").out.splitlines()]
    name, toolchain = shown
    assert [row[4] for row in rows if row[:1] == [name]] == [toolchain]


def test_show_a_build_by_path(built: Build, cli: Cli) -> None:
    assert cli("show", str(built.path)).out == cli("show", "latest").out


def test_show_a_builds_units_tree_and_measurements(built: Build, cli: Cli) -> None:
    units = cli("show", "latest", "--units")
    assert "app.service" in units.out and "[Service]" in units.out
    tree = cli("show", "latest", "--tree")
    assert any(
        line.endswith("  mkosi.conf") or " mkosi.conf " in line for line in tree.out.splitlines()
    )
    measured = cli("show", "latest", "--measurements")
    assert "qemu/uki  payload.efi  predicted" in measured.out
    assert "qemu/disk  qemu.raw  predicted" in measured.out
    records = json.loads(cli("show", "latest", "--measurements", "--json").out)
    assert records["measurements"]["schema"] == "tundravm.measurements/1"


def test_profile_does_not_apply_to_a_build(built: Build, cli: Cli) -> None:
    result = cli("show", "latest", "--profile", "dev")
    assert result.code == EXIT_ERROR


def test_no_build_yet(project: Path, cli: Cli) -> None:
    result = cli("show", "latest")
    assert result.code == EXIT_ERROR
    assert "No production build under build/." in result.err
    assert "tundravm build" in result.err


def test_show_the_latest_development_build(dev_built: Build, cli: Cli) -> None:
    result = cli("show", "latest-development")
    assert result.code == EXIT_OK
    assert result.out.startswith(f"build {dev_built.id}  demo dev (development)")
    refused = cli("show", "latest", "--dev")
    assert refused.code == EXIT_ERROR
    assert "`tundravm show latest-development`" in refused.err


def test_diff_of_the_production_and_development_builds(dev_built: Build, cli: Cli) -> None:
    result = cli("show", "--diff", "latest", "latest-development")
    assert result.code == EXIT_OK, result.err
    lines = result.out.splitlines()
    assert lines[0].endswith(f"-> {dev_built.id}")
    assert "Packages:\n  unknown: neither build has a package manifest\n" in result.out
    assert "  + payload/mkosi.profiles/dev/mkosi.conf" in lines
    assert "Boot payload:" in lines and "Policy coverage:" not in lines
    report = json.loads(cli("show", "--diff", "latest", "latest-development", "--json").out)
    assert report["schema"] == "tundravm.show/1"
    diff = report["diff"]
    assert diff["new"]["id"] == dev_built.id
    assert {f["path"] for f in diff["files"]} >= {"payload/mkosi.profiles/dev/mkosi.conf"}
    assert set(diff["boot"]) == {"uki", "cmdline"}
    assert set(diff["measurements"]) == {"qemu/disk", "qemu/uki"}
    assert set(diff["measurements"]["qemu/uki"]["registers"]) == {"RTMR1", "RTMR2"}
    assert diff["coverage"] == {}


def test_a_build_against_itself_differs_in_nothing(built: Build, cli: Cli) -> None:
    diff = json.loads(cli("show", "--diff", built.id, "latest", "--json").out)["diff"]
    assert diff["files"] == [] and diff["boot"] == {} and diff["units"] == []
    assert "Files:" not in cli("show", "--diff", built.id, "latest").out


def test_diff_of_two_image_files(project: Path, cli: Cli) -> None:
    """The sections show prints, only the lines that differ."""
    changed = STARTER.replace('"8080"', '"9090"').replace("TCP(8080)", "TCP(9090)")
    (project / "next.py").write_text(changed, encoding="utf-8")
    result = cli("show", "--diff", "demo.py", "next.py")
    assert result.code == EXIT_OK, result.err
    assert result.out.splitlines()[:4] == ["demo.py -> next.py", "", "Services:", "  app"]
    lines = result.out.splitlines()
    assert {"    - ports  TCP(8080)", "    + ports  TCP(9090)"} <= set(lines)
    assert {"  Firewall", "    - in  8080/tcp  app.service"} <= set(lines)
    assert "Profiles" not in result.out
    assert cli("show", "--diff", "demo.py", "demo.py").out == "demo.py -> demo.py: no difference\n"
    mixed = cli("show", "--diff", "demo.py", "latest")
    assert mixed.code == EXIT_ERROR and "two image files or two builds" in mixed.err
