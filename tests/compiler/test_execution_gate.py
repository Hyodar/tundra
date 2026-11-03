"""The execution gate (plan 6.1): ``tdx-evidence.service``, what it gates and each profile's mode.

The gate's script runs here too, against stand-ins for ``/proc/cpuinfo``, the guest device
and the mode file: without the ``tdx_guest`` CPU flag a production profile exits 1 with
:data:`~tundravm._modules.execution.REFUSED`, a development one records ``simulated``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tundravm import Image, Profile
from tundravm import Service as ImageService
from tundravm._modules import execution as gate
from tundravm._modules import tdxs as issuers
from tundravm.components import DevAccess
from tundravm.declarative import (
    BootStep,
    Calendar,
    Debian,
    Fragment,
    Mount,
    Recipe,
    Service,
    Task,
    Unit,
    Variant,
    lower,
)
from tundravm.declarative.systemd import Hardening
from tundravm.declarative.utils import Tdxs
from tundravm.testing import plan
from tundravm.testing._model import compile_tree

OFF = Hardening.none()
UNIT = "[Unit]\nDescription=raw\n\n[Service]\nExecStart=/usr/bin/raw\n"


def _recipe(*items: object, variants: tuple[Variant, ...] = ()) -> Recipe:
    return Recipe(
        name="gate",
        platform=Debian("trixie", boot=None),
        items=items,  # type: ignore[arg-type]
        variants=variants or (Variant("default", outputs=("qemu",)),),
    )


def _gated(text: str) -> bool:
    lines = text.splitlines()
    after = next((line for line in lines if line.startswith("After=")), "")
    requires = next((line for line in lines if line.startswith("Requires=")), "")
    return gate.GATE_UNIT in after.split("=", 1)[-1].split() and gate.GATE_UNIT in requires


def test_every_service_and_scheduled_task_requires_the_gate() -> None:
    step = BootStep("seed", "true")
    tree = compile_tree(
        _recipe(
            step,
            Service("app", "/usr/bin/app"),
            Service("early", "/usr/bin/early", needs=()),
            Task("nightly", "/usr/bin/job", schedule=Calendar("daily"), hardening=OFF),
            Task("probe", "/usr/bin/true", hardening=OFF, needs=()),
            Unit("raw.service", UNIT, enabled=True),
            Unit("ssh.service", enabled=True, needs=(step.ready,)),
            Mount("/var/lib/app", "tmpfs", type="tmpfs"),
        )
    )
    assert _gated(tree.unit("app")) and _gated(tree.unit("early")) and _gated(tree.unit("nightly"))
    for ungated in ("probe", "raw", "var-lib-app.mount", "runtime-init.service"):
        assert gate.GATE_UNIT not in tree.unit(ungated), ungated
    needs = tree.path("mkosi.extra/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf")
    assert gate.GATE_UNIT not in needs.read_text()
    assert gate.GATE_UNIT not in tree.unit(gate.GATE_UNIT).split("[Service]")[0]


def test_the_cloud_glue_is_not_gated() -> None:
    variants = (
        Variant("default", outputs=("qemu",)),
        Variant("azure", outputs=("azure",)),
        Variant("gcp", outputs=("gcp",)),
    )
    tree = compile_tree(_recipe(Service("app", "/usr/bin/app", needs=()), variants=variants))
    assert gate.GATE_UNIT not in tree.unit("gcp-hostname")
    assert gate.GATE_UNIT not in tree.unit("azure-complete-provisioning")


@pytest.mark.parametrize(
    ("default", "other", "dropin"),
    [
        (True, False, "development"),  # a dev variant over a production payload
        (False, True, "production"),  # a production variant over a dev payload
        (True, True, None),
    ],
)
def test_a_profile_whose_mode_differs_sets_it_through_the_drop_in(
    default: bool, other: bool, dropin: str | None
) -> None:
    recipe = _recipe(
        Service("app", "/usr/bin/app", needs=()),
        variants=(
            Variant("default", outputs=("qemu",), release=default),
            Variant("other", release=other),
        ),
    )
    tree = compile_tree(recipe)
    payload = tree.unit(gate.GATE_UNIT, "default")
    mode = gate.execution(default)
    assert f"Environment=TUNDRAVM_EXECUTION={mode}" in payload
    overlay = tree.path(f"mkosi.extra{gate.GATE_DROPIN}", "other")
    if dropin is None:
        assert not overlay.exists()
    else:
        assert overlay.read_text() == f"[Service]\nEnvironment=TUNDRAVM_EXECUTION={dropin}\n"
        assert "mkosi.profiles/other/" in str(overlay)
    assert not tree.path(f"mkosi.extra{gate.GATE_DROPIN}", "default").exists()


def test_a_standalone_profile_shares_the_gate_and_sets_its_own_mode() -> None:
    recipe = _recipe(
        Service("app", "/usr/bin/app", needs=()),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "alone",
                inherits=None,
                add=Fragment("alone", items=(Service("tool", "/usr/bin/tool", needs=()),)),
                release=False,
            ),
        ),
    )
    lowered = lower(recipe)
    assert "alone" in lowered.overlays
    tree = compile_tree(recipe)
    assert _gated(tree.unit("tool", "alone"))
    assert (
        "TUNDRAVM_EXECUTION=development"
        in tree.path(f"mkosi.extra{gate.GATE_DROPIN}", "alone").read_text()
    )


def test_an_image_gates_tdxs_and_its_services_and_its_dev_profile_runs_simulated() -> None:
    image = Image(
        "demo",
        services=[ImageService("app", ["/usr/bin/app"])],
        profiles={"staging": Profile(production=False)},
        dev=DevAccess(password="tdx"),
    )
    production = plan(image, "production")
    for unit in ("app", "tdxs"):
        assert gate.GATE_UNIT in (production.unit(unit).get("Unit", "Requires") or "").split()
    assert "TUNDRAVM_EXECUTION=production" in production.unit(gate.GATE_UNIT).text
    with pytest.raises(KeyError):
        production.file(gate.GATE_DROPIN)
    for name in ("dev", "staging"):
        dropin = plan(image, name).file(gate.GATE_DROPIN).text
        assert "TUNDRAVM_EXECUTION=development" in dropin, name


REAL = ("/usr/bin/tdxs", "--config", issuers.TDXS_DEFAULT_CONFIG_PATH, "--log-level", "info")
PICK = (issuers.ISSUER_SCRIPT, "--log-level", "info")
OWN = (issuers.SIMULATOR_CONFIG_PATH, issuers.ISSUER_SCRIPT)


@pytest.mark.parametrize(
    ("default", "other", "starts", "own"),
    [
        (True, False, {"default": REAL, "other": PICK}, {"default": (), "other": OWN}),
        (False, True, {"default": PICK, "other": REAL}, {"default": OWN, "other": ()}),
        (False, False, {"default": PICK, "other": PICK}, {"default": OWN, "other": ()}),
    ],
    ids=["production-default-dev-overlay", "dev-default-production-overlay", "dev-both"],
)
def test_the_issuer_follows_the_build(
    default: bool,
    other: bool,
    starts: dict[str, tuple[str, ...]],
    own: dict[str, tuple[str, ...]],
) -> None:
    """A development profile's tdxs starts through the pick with the simulator's config
    beside the real one; a production profile's starts on the real config, also over a
    development default."""
    recipe = _recipe(
        Tdxs(),
        Service("app", "/usr/bin/app", needs=()),
        variants=(
            Variant("default", outputs=("qemu",), release=default),
            Variant(
                "other", add=Fragment("other", items=(Unit("x.service", UNIT),)), release=other
            ),
        ),
    )
    state = lower(recipe).state
    for name in ("default", "other"):
        tdxs = next(s for s in state.effective_profile(name).services if s.name == "tdxs.service")
        assert tdxs.command == starts[name], name
        files = {e.path for e in state.ensure_profile(name).files}
        assert tuple(p for p in OWN if p in files) == own[name], name


@pytest.mark.parametrize(
    ("mode", "line", "config"),
    [
        ("simulated", issuers.ISSUER_LINE, issuers.SIMULATOR_CONFIG_PATH),
        ("tdx", "tdxs-issuer: TDX quote path; issuing with the tdx issuer", REAL[2]),
    ],
)
def test_the_issuer_script_picks_by_the_gate_mode(
    tmp_path: Path, mode: str, line: str, config: str
) -> None:
    recorded = tmp_path / "mode"
    recorded.write_text(f"{mode}\n")
    script = (
        issuers.issuer_script("tdx")
        .replace(gate.MODE_FILE, str(recorded))
        .replace("exec /usr/bin/tdxs", "exec echo tdxs")
    )
    done = subprocess.run(
        ["/bin/sh", "-c", script, "tdxs-issuer", "--log-level", "info"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.splitlines() == [line, f"tdxs --config {config} --log-level info"]


# ── the script itself ────────────────────────────────────────────────────


def _run(tmp_path: Path, execution: str, *, cpu_flag: bool, report: bool) -> tuple[int, str, Path]:
    """The gate script with its paths moved under *tmp_path*."""
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("flags\t\t: fpu vme" + (" tdx_guest" if cpu_flag else "") + "\n")
    tsm = tmp_path / "tsm" / "report"
    if report:
        tsm.mkdir(parents=True)
    run = tmp_path / "run"
    script = (
        gate.SCRIPT.replace("/proc/cpuinfo", str(cpuinfo))
        .replace(gate.TSM_REPORT, str(tsm))
        .replace(gate.TDX_GUEST, str(tmp_path / "tdx_guest"))
        .replace("/run/tundravm", str(run))
        .replace(f"-lt {gate.WAIT_SECONDS}", "-lt 1")
        .replace("sleep 1", "true")
    )
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": "/usr/bin:/bin", gate.EXECUTION_ENV: execution},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout, run / "mode"


QUOTE_PATH = "tdx-evidence: TDX quote path {report}"
"""The script's line for a found quote path; ``{report}`` is the stand-in TSM report dir."""


@pytest.mark.parametrize(
    ("execution", "cpu_flag", "report", "code", "stdout", "recorded"),
    [
        ("production", False, False, 1, gate.REFUSED, None),
        # A tdx_guest CPU flag without a quote path is still refused.
        ("production", True, False, 1, gate.REFUSED, None),
        ("development", False, False, 0, gate.ALLOWED, gate.SIMULATED),
        ("production", True, True, 0, QUOTE_PATH, gate.TDX),
        ("development", True, True, 0, QUOTE_PATH, gate.TDX),
    ],
    ids=[
        "production-without-tdx-refuses",
        "production-cpu-flag-only-refuses",
        "development-without-tdx-simulated",
        "production-quote-path-tdx",
        "development-quote-path-tdx",
    ],
)
def test_the_gate_script_records_or_refuses_per_mode(
    tmp_path: Path,
    execution: str,
    cpu_flag: bool,
    report: bool,
    code: int,
    stdout: str,
    recorded: str | None,
) -> None:
    returncode, out, mode = _run(tmp_path, execution, cpu_flag=cpu_flag, report=report)
    expected = stdout.format(report=tmp_path / "tsm" / "report")
    assert (returncode, out.strip()) == (code, expected)
    if recorded is None:
        assert not mode.exists()
    else:
        assert mode.read_text() == f"{recorded}\n"
