"""``convert PATH --from mkosi|systemd``."""

from __future__ import annotations

import ast
import json
import shutil
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import tundravm
from tests.cli.conftest import Cli
from tests.helpers import REPO_ROOT
from tundravm import _mkosi
from tundravm.cli import EXIT_ERROR, EXIT_FAILURE, EXIT_OK
from tundravm.compiler.emit_mkosi import MAIN_IMAGE_SETTINGS
from tundravm.convert import (
    DEFAULTED,
    DERIVED,
    MAPPED,
    REFUSED,
    VERBATIM,
    Verdict,
    implied_packages,
)
from tundravm.systemd import Calendar, Hardening

pytestmark = pytest.mark.boundary  # convert reads the tree through mkosi's own parser

TREE = REPO_ROOT / "tests" / "fixtures" / "native-tree" / "tree-initramfs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mkosi-trees"
"""Real mkosi trees (each with a SOURCE file naming where it came from)."""

SERVICE = """\
[Unit]
Description=Web

[Service]
Type=simple
ExecStart=/usr/bin/web --port 8080
User=web

[Install]
WantedBy=multi-user.target
"""


def _image_call(source: str) -> ast.Call:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Image":
            return node
    raise AssertionError("no Image(...) in the source")


@dataclass(frozen=True)
class Row:
    """What a fixture tree's report must say: how many lines of each kind, and the lines
    (by their start) that pin each setting the tree exercises."""

    counts: dict[str, int]
    lines: tuple[str, ...]


ROWS = {
    "mkosi": Row(
        {"mapped": 7, "kept": 5, "defaulted": 9, "refused": 5},
        (
            '# mapped: Release=testing -> base=Debian("testing")',
            "# mapped: Repositories=non-free-firmware -> Debian(repositories=[AptRepository(",
            "# mapped: KernelCommandLine=enforcing=0 systemd.log_ratelimit_kmsg=0 ",
            '# mapped: PostInstallationScripts=mkosi.postinst -> mkosi=Mkosi(scripts=[("postinst",',
            "# kept: Include=mkosi-vm: resolved by mkosi (from mkosi.conf)",
            "# kept: OutputDirectory=mkosi.output",
            "# defaulted: Distribution: the tree names none, so mkosi would build the host's",
            "# defaulted: Autologin=yes: a production image has no console login",
            "# defaulted: Incremental=yes: tundravm build runs mkosi in its own build directory",
            "# refused: Format=directory: tundravm builds the payload as a UKI inside a disk",
            "# refused: RAM=4294967296: a `mkosi vm` setting",
            "# refused: mkosi.conf.d/centos.conf: mkosi did not read it (Distribution=|centos ",
        ),
    ),
    "systemd": Row(
        {"mapped": 7, "kept": 3, "defaulted": 9, "refused": 10},
        (
            "# mapped: VolatilePackages=libnss-myhostname ",
            "# mapped: PrepareScripts=mkosi.conf.d/debian-ubuntu/systemd.prepare -> mkosi=Mkosi(",
            '# mapped: WithTests=no -> mkosi=Mkosi(settings={"WithTests": "no"})',
            "# kept: MinimumVersion=27.1",
            "# defaulted: SecureBoot=yes: tundravm measures the UKI",
            "# defaulted: OutputDirectory=../build/mkosi.output: tundravm build runs mkosi",
            "# refused: Environment=GIT_BRANCH=debian/master ",
            "# refused: PassEnvironment=NO_SYNC NO_BUILD ",
            "# refused: Credentials=journal.storage=persistent ",
            "# refused: mkosi.conf.d/debian-ubuntu/mkosi.conf.d/network.conf: mkosi did not read "
            "it (Environment=NO_BUILD=1)",
        ),
    ),
    "debian-cloud": Row(
        {"mapped": 11, "kept": 7, "defaulted": 6, "refused": 3},
        (
            '# mapped: Snapshot=20250915T000000Z -> Debian(snapshot="20250915T000000Z")',
            "# mapped: KernelModules=virtio_net virtio_blk nvme* -> kernel=Kernel.default(modules=",
            "# mapped: KernelCommandLine=net.ifnames=0 rw -> kernel=Kernel.default(cmdline=[...])",
            '# mapped: Locale=C.UTF-8 -> mkosi=Mkosi(settings={"Locale": "C.UTF-8"})',
            "# kept: Format=disk: the main image is the disk around the UKI payload",
            "# kept: Bootable=enabled: the payload is a UKI",
            "# defaulted: Mirror=https://deb.debian.org/debian: the build reads snapshot.debian",
            "# defaulted: RootPassword=(set): a production image has no console login",
            "# defaulted: RepartDirectories=mkosi.repart: tundravm writes the partitions",
            "# refused: KernelModules=nvme*: a glob; Kernel(modules=) takes module names",
            "# refused: KernelCommandLine=console=ttyS0: ",
            "# refused: CPUs=2: a `mkosi vm` setting; tundravm run boots the build itself (run "
            "--cpus)",
        ),
    ),
    "extra-postinst": Row(
        {"mapped": 8, "kept": 2, "refused": 1},
        (
            "# mapped: ExtraTrees=mkosi.extra/usr/lib/systemd/system/greeter.service -> "
            "services=[Service(...)] (left out of the Tree)",
            '# mapped: ExtraTrees=mkosi.extra:/ -> files=[Tree(HERE / "mkosi.extra", "/", '
            'exclude=["usr/lib/systemd/system/greeter.service"])]',
            "# mapped: Packages=busybox linux-image-amd64 openssl systemd udev -> packages= (every "
            "image installs linux-image-amd64, openssl, systemd, udev: left out)",
            '# mapped: FinalizeScripts=mkosi.finalize -> mkosi=Mkosi(scripts=[("finalize", ...)])',
            "# refused: SkeletonTrees=mkosi.skeleton:/: a second tree at / (mkosi.extra:/ is the "
            "first); an Image holds one Tree per target: merge them",
        ),
    ),
    "profiles": Row(
        {"mapped": 5, "kept": 2, "refused": 2},
        (
            '# mapped: mkosi.profiles/debug -> profiles={"debug": Profile(add=[Tree(HERE / '
            '"mkosi.profiles/debug/mkosi.extra", "/"), "gdb", "strace"])}',
            "# refused: mkosi.profiles/debug: KernelCommandLine=systemd.log_level=debug: a Profile "
            "adds packages, files and services",
            "# refused: mkosi.conf.d/20-ci.conf: mkosi did not read it (Profiles=ci)",
        ),
    ),
}
"""One row per fixture tree: what its report says."""

BUILT_ELSEWHERE = {
    ("Format", "disk"),
    ("Format", "uki"),
    ("Bootable", "enabled"),
    ("Bootable", "auto"),
}
"""Values a converted image carries in the other image of its UKI-in-a-disk layout, which the
report keeps although the payload compared resolves them differently."""


def _normalised(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(root), "<tree>").replace(str(root.parent), "<parent>")
    if isinstance(value, list):
        return [_normalised(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _normalised(item, root) for key, item in value.items()}
    return value


def _resolved(tree: Path, *argv: str) -> dict[str, dict[str, Any]]:
    """Each image of *tree* as mkosi resolves it, by name, host paths normalised."""
    summary = _mkosi.summary(tree, extra_argv=list(argv))
    return {str(image.get("Image")): _normalised(image, tree) for image in summary.images}


@pytest.fixture(scope="module")
def mkosi_defaults(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Every setting's value in an empty Debian tree."""
    tree = tmp_path_factory.mktemp("empty")
    (tree / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\n")
    yield _resolved(tree)["main"]


@pytest.mark.parametrize("name", sorted(ROWS))
def test_a_real_tree_converts_and_its_report_is_the_whole_difference(
    cli: Cli, tmp_path: Path, name: str, mkosi_defaults: dict[str, Any]
) -> None:
    tree = tmp_path / name
    shutil.copytree(FIXTURES / name, tree)
    result = cli("convert", tree, "--from", "mkosi", "--json")
    assert result.code == EXIT_OK, result.err
    data = json.loads(result.out)
    report = [Verdict(**item) for item in data["report"]]
    assert Counter(verdict.kind for verdict in report) == ROWS[name].counts
    lines = [verdict.line for verdict in report]
    for start in ROWS[name].lines:
        assert any(line.startswith(start) for line in lines), f"{start!r} not in:\n" + "\n".join(
            lines
        )
    assert str(tree) not in result.out  # paths are relative to the image file
    image_file = tree / "image.py"
    image_file.write_text(data["source"], encoding="utf-8")
    image = tundravm.load(image_file)
    checked = cli("check", image_file)
    assert checked.code == EXIT_OK, checked.out
    before = _resolved(tree, "--distribution=debian")["main"]
    lowered = tmp_path / "lowered"
    tundravm.compile(image).write(lowered)
    after = _resolved(lowered)
    carried = {verdict.setting: verdict for verdict in report}
    for key, value in before.items():
        if key in DERIVED or key not in mkosi_defaults:
            continue
        verdict = carried.get(key)
        ours = after["main" if key in MAIN_IMAGE_SETTINGS else "payload"]
        same = value == ours.get(key)
        if verdict is None:
            assert same or value == mkosi_defaults[key], f"{key}={value!r} dropped without a line"
        elif verdict.kind != "mapped" and verdict.value:
            kept = same or (key, str(value)) in BUILT_ELSEWHERE
            assert kept == (verdict.kind == "kept"), verdict.line


def test_every_mkosi_setting_has_a_verdict() -> None:
    from mkosi.config import SETTINGS

    opinions, refusals, verbatim = set(DEFAULTED), set(REFUSED), set(VERBATIM)
    assert opinions | refusals | verbatim | set(MAPPED) == {s.name for s in SETTINGS}
    assert not (opinions & refusals or (opinions | refusals) & verbatim)


@pytest.mark.parametrize(
    ("wrappers", "targets"),
    [((), ("qemu",)), (("gcp",), ("qemu", "gcp")), (("azure", "gcp"), ("qemu", "azure", "gcp"))],
)
def test_a_tree_tundravm_wrote_converts_its_payload_and_targets_and_checks(
    cli: Cli, tmp_path: Path, wrappers: tuple[str, ...], targets: tuple[str, ...]
) -> None:
    tree = tmp_path / "mkosi"
    shutil.copytree(REPO_ROOT / "examples" / "attested-node" / "mkosi", tree, symlinks=True)
    for cloud in {"azure", "gcp"} - set(wrappers):
        shutil.rmtree(tree / "mkosi.images" / cloud)
    result = cli("convert", tree, "--from", "mkosi")
    assert result.code == EXIT_OK, result.err
    image_file = tree / "image.py"
    image_file.write_text(result.out, encoding="utf-8")
    assert tuple(tundravm.load(image_file).targets) == targets
    checked = cli("check", image_file)
    assert checked.code == EXIT_OK, checked.out
    packages = next(k.value for k in _image_call(result.out).keywords if k.arg == "packages")
    listed = set(ast.literal_eval(ast.unparse(packages)))
    assert {"cryptsetup", "socat"} <= listed and "systemd-boot-efi" not in listed
    assert not listed & implied_packages("trixie", verity=True, targets=targets)
    lines = (
        "# mapped: Packages=ca-certificates chrony ",
        "# refused: ExtraTrees=payload/mkosi.extra: generated by the Image file that wrote this ",
        "# refused: payload/mkosi.profiles/dev: generated by the Image file",
        "# note: main image (disk)",
        *(f"# note: subimage {w}" for w in wrappers),
    )
    for line in lines:
        assert line in result.out, line
    assert "no Image slot holds it" not in result.out


def test_a_signed_dated_apt_source_becomes_an_apt_repository(cli: Cli, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    sources = tree / "mkosi.sandbox" / "etc" / "apt" / "sources.list.d"
    sources.mkdir(parents=True)
    (tree / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\nRelease=trixie\n")
    (tree / "mkosi.sandbox" / "etc" / "apt" / "vendor.asc").write_text("key\n")
    (sources / "extra.sources").write_text(
        "Types: deb\nURIs: https://snapshot.debian.org/archive/debian-security/20261018T082309Z/\n"
        "Suites: trixie-security\nComponents: main\nSigned-By: /etc/apt/vendor.asc\n\n"
        "Types: deb\nURIs: https://apt.example.com\nSuites: stable\nTrusted: yes\n"
    )
    result = cli("convert", tree, "--from", "mkosi")
    assert result.code == EXIT_OK, result.err
    (tree / "image.py").write_text(result.out, encoding="utf-8")
    (repository,) = tundravm.load(tree / "image.py").base.repositories
    assert (repository.suite, repository.key) == (
        "trixie-security",
        tree / "mkosi.sandbox/etc/apt/vendor.asc",
    )
    assert "# refused: SandboxTrees=" in result.out
    assert "https://apt.example.com: unsigned" in result.out
    assert "# mapped: SandboxTrees=" in result.out


def test_a_tree_that_runs_scripts_while_parsing_needs_allow_scripts(
    cli: Cli, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(TREE, tree, symlinks=True)
    version = tree / "mkosi.version"
    version.write_text("#!/bin/sh\necho 1.2.3\n")
    version.chmod(0o755)
    refused = cli("convert", tree, "--from", "mkosi")
    assert refused.code == EXIT_ERROR
    assert "Parsing" in refused.err and "runs mkosi.version" in refused.err
    assert "--allow-scripts" in refused.err
    allowed = cli("convert", tree, "--from", "mkosi", "--allow-scripts")
    assert allowed.code == EXIT_OK, allowed.err
    assert "# note: ran while parsing: mkosi.version" in allowed.out


def test_a_directory_without_mkosi_conf_is_refused(cli: Cli, tmp_path: Path) -> None:
    result = cli("convert", tmp_path, "--from", "mkosi")
    assert result.code == EXIT_ERROR and "has no mkosi.conf" in result.err


def test_allow_scripts_applies_to_mkosi_only(cli: Cli, tmp_path: Path) -> None:
    unit = tmp_path / "web.service"
    unit.write_text(SERVICE)
    result = cli("convert", unit, "--from", "systemd", "--allow-scripts")
    assert result.code == EXIT_ERROR


WORKER = """\
[Unit]
Description=Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/python3 -m worker --queue jobs
Environment=MODE=batch
StateDirectory=worker
MemoryMax=1G
ProtectSystem=strict
OOMScoreAdjust=-500
"""

BACKUP = "[Service]\nType=oneshot\nExecStart=/usr/bin/backup\n"
BACKUP_TIMER = "[Timer]\nOnCalendar=daily\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"


def test_units_become_an_image_file_that_loads_and_checks(cli: Cli, tmp_path: Path) -> None:
    units = tmp_path / "units"
    (units / "worker.service.d").mkdir(parents=True)
    (units / "web.service").write_text(SERVICE)
    (units / "worker.service").write_text(WORKER)
    (units / "worker.service.d" / "10-trace.conf").write_text("[Service]\nEnvironment=TRACE=1\n")
    (units / "backup.service").write_text(BACKUP)
    (units / "backup.timer").write_text(BACKUP_TIMER)
    (units / "data.mount").write_text("[Mount]\nWhat=/dev/vdb\nWhere=/data\n")
    (units / "exporter.service.d").mkdir()
    (units / "exporter.service.d" / "10-args.conf").write_text("[Service]\nEnvironment=A=1\n")
    result = cli("convert", units, "--from", "systemd")
    assert result.code == EXIT_OK, result.err
    imports = [line for line in result.out.splitlines() if line.startswith(("from ", "import "))]
    assert imports == [
        "from tundravm import Image, Service",
        "from tundravm.systemd import Calendar, DropIn, Notify, Resources",
    ]
    assert "# unsupported: data.mount: no Image slot holds it" in result.out
    assert "in place of the unit's ProtectSystem" in result.out
    image_file = tmp_path / "image.py"
    image_file.write_text(result.out, encoding="utf-8")
    image = tundravm.load(image_file)
    services = {service.name: service for service in image.services}
    assert services["web"].argv == ("/usr/bin/web", "--port", "8080")
    assert dict(services["worker"].environment) == {"MODE": "batch", "TRACE": "1"}
    assert services["backup"].timer == Calendar("daily")
    assert services["exporter"].package == "exporter"
    assert services["exporter"].unit is not None and services["exporter"].unit.env == {"A": "1"}
    checked = cli("check", image_file)
    assert checked.code == EXIT_FAILURE
    assert [line.split(" runs ")[1].split(",")[0] for line in _missing(checked.out)] == [
        "/usr/bin/backup",
        "/usr/bin/web",
        "/usr/bin/python3",
    ]
    assert "packages=['python3']" in checked.out
    image_file.write_text(
        result.out.replace("    services=[", '    packages=["python3"],\n    services=[', 1)
    )
    assert len(_missing(cli("check", image_file).out)) == 2


def _missing(report: str) -> list[str]:
    return [
        line for line in report.splitlines() if line.startswith("error service-program-missing")
    ]


@pytest.mark.parametrize(
    ("sandbox", "written"),
    [
        (Hardening.preset("dotnet"), 'hardening="dotnet"'),
        (Hardening.preset("static"), "exactly the 'static' preset"),
        (Hardening.preset("jit", devices=["/dev/kvm"]), "in place of the unit's"),
    ],
)
def test_a_sandbox_that_is_exactly_a_preset_converts_to_its_name(
    cli: Cli, tmp_path: Path, sandbox: Hardening, written: str
) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in sandbox.entries())
    (tmp_path / "app.service").write_text(f"[Service]\nExecStart=/usr/bin/app\n{lines}")
    result = cli("convert", tmp_path / "app.service", "--from", "systemd")
    assert result.code == EXIT_OK, result.err
    assert written in result.out
    assert ('hardening="' in result.out) == written.startswith("hardening=")


def test_a_directory_without_units_is_refused(cli: Cli, tmp_path: Path) -> None:
    result = cli("convert", tmp_path, "--from", "systemd")
    assert result.code == EXIT_ERROR and "holds no unit file" in result.err


@pytest.mark.parametrize("source", ["mkosi", "systemd"])
def test_from_is_required_and_closed(
    cli: Cli, source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        cli("convert", "x")
    with pytest.raises(SystemExit):
        cli("convert", "x", "--from", "docker")
    assert "invalid choice: 'docker'" in capsys.readouterr().err
