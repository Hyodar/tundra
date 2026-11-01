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
    assert gate.GATE_UNIT not in tree.unit("gcp-metadata-dns")
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
