"""``dev``: build the dev profile, boot it under the fake QEMU, probe it, follow the
console, stop on Ctrl-C."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cli.conftest import Cli, receipt
from tundravm import instances
from tundravm.builds import Build
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR


def _probe_lines(build: Build, failing: str | None = None) -> str:
    """What the probe of *build* prints when every check passes but *failing*."""
    checks = [str(row["check"]) for row in build.manifest["probe"]]
    lines = [
        f"tundravm-probe: {name} {'fail bad' if name == failing else 'ok fine'}" for name in checks
    ]
    failed = int(failing is not None)
    lines.append(f"tundravm-probe: done {len(checks) - failed} ok {failed} fail")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("failing", "code"),
    [
        (None, EXIT_OK),
        ("service:app.service", EXIT_FAILURE),
        ("the probe did not finish", EXIT_FAILURE),
    ],
    ids=["every-check-ok", "a-failed-check", "the-probe-did-not-finish"],
)
def test_no_follow_exits_with_the_probes_verdict(
    dev_built: Build,
    project: Path,
    cli: Cli,
    qemu: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str | None,
    code: int,
) -> None:
    if failing == "the probe did not finish":
        monkeypatch.setattr(instances, "PROBE_TIMEOUT", 0.05)
    else:
        monkeypatch.setenv("FAKE_QEMU_CONSOLE", _probe_lines(dev_built, failing))
    result = cli("dev", "--no-follow")
    assert result.code == code, result.err
    assert result.out.startswith(
        f"already built (identical; left untouched) build/demo/development/{dev_built.id}\n"
    )
    assert "instance demo-1: simulated (development build" in result.out
    assert result.out.splitlines()[-1] == "stop it with `tundravm run stop demo-1`"
    assert failing is None or failing in result.out
    assert receipt(project, "demo-1")["state"] == "running"
    assert cli("run", "stop", "demo-1").out.strip() == "stopped demo-1"


def test_ctrl_c_stops_the_instance_and_keeps_the_build(
    dev_built: Build, project: Path, cli: Cli, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_QEMU_CONSOLE", _probe_lines(dev_built))
    real_follow, real_alive = instances.follow, instances.alive

    def follow(instance: instances.Instance, out: object, **kwargs: object) -> bool:
        def interrupted(instance: instances.Instance) -> bool:
            monkeypatch.setattr(instances, "alive", real_alive)
            raise KeyboardInterrupt

        monkeypatch.setattr(instances, "alive", interrupted)
        return real_follow(instance, out, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(instances, "follow", follow)
    result = cli("dev")
    assert result.code == EXIT_OK, result.err
    assert "following the console (`tundravm run logs demo-1` has it from boot)" in result.out
    assert result.out.splitlines()[-1] == "stopped demo-1"
    assert receipt(project, "demo-1")["state"] == "stopped"
    assert dev_built.path.is_dir()


def test_a_host_that_cannot_run_the_mode_is_refused_before_the_build(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "tdx_refusal", lambda: "no Intel TDX host: kvm_intel tdx is off")
    result = cli("dev", "--mode", "tdx")
    assert result.code == EXIT_SDK_ERROR
    assert "run --mode tdx cannot run here" in result.err and "doctor --for tdx" in result.err
    assert not (project / "build").exists()
