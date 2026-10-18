"""``tundravm.testing.boot`` against a fake QEMU that prints a console transcript.

The real boot (QEMU + OVMF booting a baked UKI) runs in CI's ``boot`` job:
``tests/integration/test_templates_boot.py``.
"""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tundravm import testing
from tundravm.testing import BootResult, BootUnavailable, boot, boot_unavailable, scan_console
from tundravm.testing._boot import CONSOLE_CMDLINE, find_ovmf, qemu_command
from tundravm.testing.pytest_plugin import BootRunner

FAKE_QEMU = """\
#!{python}
import json, os, sys, time
argv = sys.argv[1:]
log = argv[argv.index("-serial") + 1].removeprefix("file:")
with open(os.environ["FAKE_QEMU_ARGV"], "w") as out:
    json.dump(argv, out)
sys.stderr.write(os.environ.get("FAKE_QEMU_STDERR", ""))
with open(log, "w") as console:
    for line in open(os.environ["FAKE_QEMU_TRANSCRIPT"]):
        console.write(line)
        console.flush()
time.sleep(float(os.environ.get("FAKE_QEMU_HANG", "0")))
sys.exit(int(os.environ.get("FAKE_QEMU_EXIT", "0")))
"""

OK = "[\x1b[0;32m  OK  \x1b[0m] "
BOOTED = (
    "Linux version 6.12.57+deb13-amd64\r\n",
    f"{OK}Reached target sysinit.target.\r\n",
    f"{OK}Finished hello.service.\r\n",
    f"{OK}Started app.service.\r\n",
    f"{OK}Reached target minimal.target.\r\n",
)


class Fake:
    """A fake ``qemu-system-x86_64``, OVMF pair and UKI under ``tmp_path``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.qemu = tmp_path / "qemu-system-x86_64"
        self.qemu.write_text(FAKE_QEMU.format(python=sys.executable), encoding="utf-8")
        self.qemu.chmod(0o755)
        self.code = tmp_path / "OVMF_CODE.fd"
        self.code.write_bytes(b"code")
        (tmp_path / "OVMF_VARS.fd").write_bytes(b"vars")
        self.image = tmp_path / "node.efi"
        self.image.write_bytes(b"MZ")
        self.argv = tmp_path / "argv.json"
        self.transcript = tmp_path / "transcript.txt"
        self.log = tmp_path / "boot" / "console.log"
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("FAKE_QEMU_ARGV", str(self.argv))
        monkeypatch.setenv("FAKE_QEMU_TRANSCRIPT", str(self.transcript))

    def run(self, *lines: str, hang: float = 30, exit_code: int = 0, **kwargs: Any) -> BootResult:
        self.transcript.write_text("".join(lines), encoding="utf-8")
        self.monkeypatch.setenv("FAKE_QEMU_HANG", str(hang))
        self.monkeypatch.setenv("FAKE_QEMU_EXIT", str(exit_code))
        options: dict[str, Any] = {"settle": 0, "timeout": 20, "kvm": False, "log": self.log}
        options.update(kwargs)
        return boot(self.image, qemu=str(self.qemu), firmware=self.code, **options)

    def command(self) -> list[str]:
        return list(json.loads(self.argv.read_text(encoding="utf-8")))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    return Fake(tmp_path, monkeypatch)


def test_boot_reaches_the_target_and_the_expected_units(fake: Fake) -> None:
    result = fake.run(*BOOTED, expect_units=("app", "hello.service"))
    result.assert_booted()
    assert result.ok and not result.timed_out
    assert result.returncode is None  # still running: the harness stopped it
    assert {"sysinit.target", "hello.service", "app.service", "minimal.target"} <= set(
        result.started
    )
    assert result.failed == () and result.fatal == () and result.missing == ()
    assert "\x1b" not in result.console and "\r" not in result.console
    assert result.log == fake.log and "Reached target minimal.target." in fake.log.read_text()
    assert result.duration < 20


def test_boot_command_boots_the_uki_like_the_deploy_adapter(fake: Fake) -> None:
    fake.run(*BOOTED)
    argv = fake.command()
    assert argv[argv.index("-kernel") + 1] == str(fake.image)
    assert f"if=pflash,format=raw,readonly=on,file={fake.code}" in argv
    store = fake.log.with_name("console.ovmf-vars.fd")
    assert f"if=pflash,format=raw,file={store}" in argv and store.read_bytes() == b"vars"
    assert argv[argv.index("-machine") + 1] == "q35,accel=tcg"
    assert argv[argv.index("-cpu") + 1] == "max"
    assert "-no-reboot" in argv and argv[argv.index("-display") + 1] == "none"
    extra = "type=11,value=io.systemd.stub.kernel-cmdline-extra=" + " ".join(CONSOLE_CMDLINE)
    assert argv[argv.index("-smbios") + 1] == extra
    assert argv[argv.index("-nic") + 1] == "user,model=virtio-net-pci"


def test_a_failed_unit_fails_the_boot(fake: Fake) -> None:
    failed = "[\x1b[0;1;31mFAILED\x1b[0m] Failed to start app.service.\r\n"
    result = fake.run(*BOOTED[:3], failed, BOOTED[4], expect_units=("app",))
    assert not result.ok
    assert result.failed == ("app.service",) and result.missing == ("app.service",)
    with pytest.raises(AssertionError, match="failed units: app.service"):
        result.assert_booted()


def test_a_kernel_panic_ends_the_boot_at_once(fake: Fake) -> None:
    panic = "Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)\n"
    result = fake.run(BOOTED[0], panic, hang=60, timeout=50)
    assert result.fatal == (panic.strip(),) and not result.timed_out
    assert result.duration < 30
    assert "Reached target minimal.target" in result.missing


def test_emergency_mode_is_fatal(fake: Fake) -> None:
    result = fake.run(BOOTED[0], "You are in emergency mode. After logging in, type ...\n")
    assert not result.ok and result.fatal


def test_a_boot_that_never_reaches_the_target_times_out(fake: Fake) -> None:
    result = fake.run(*BOOTED[:2], timeout=1)
    assert result.timed_out and not result.ok
    assert result.missing == ("Reached target minimal.target",)
    assert result.duration < 15
    assert "timed out" in result.summary()


def test_qemu_exiting_early_reports_its_stderr(fake: Fake) -> None:
    fake.monkeypatch.setenv("FAKE_QEMU_STDERR", "qemu: could not load kernel\n")
    result = fake.run(hang=0, exit_code=1)
    assert result.returncode == 1 and not result.ok and not result.timed_out
    assert "could not load kernel" in result.stderr
    assert "qemu exited 1" in result.summary()


def test_scan_console_reads_status_lines_in_every_unit_format() -> None:
    scan = scan_console(
        "[  OK  ] Started app.service - app 0.1.0.\n"
        "[  OK  ] Mounted tmp.mount.\n"
        "[  OK  ] Listening on systemd-journald.socket - Journal Socket.\n"
        "[DEPEND] Dependency failed for web.service.\n"
        "[FAILED] Failed to mount data.mount - /data.\n"
    )
    assert scan.started == ("app.service", "tmp.mount", "systemd-journald.socket")
    assert scan.failed == ("web.service", "data.mount") and scan.fatal == ()


def test_a_unit_that_fails_or_exits_after_starting_is_caught() -> None:
    scan = scan_console(
        "[  OK  ] Started app.service - App.\n"
        "[  OK  ] Started worker.service.\n"
        "[    1.9] systemd[1]: hello.service: Deactivated successfully.\n"
        "[  OK  ] Finished hello.service.\n"
        "[    2.1] systemd[1]: app.service: Main process exited, code=exited, status=1/FAILURE\n"
        "[    2.1] systemd[1]: app.service: Failed with result 'exit-code'.\n"
        "[    2.2] systemd[1]: worker.service: Deactivated successfully.\n"
    )
    assert scan.failed == ("app.service",)
    assert scan.exited == ("worker.service",)  # a finished oneshot is done, not exited
    assert {"app.service", "worker.service", "hello.service"} <= set(scan.started)


def test_an_expected_unit_that_exits_fails_the_boot(fake: Fake) -> None:
    gone = "[   3.0] systemd[1]: app.service: Deactivated successfully.\r\n"
    result = fake.run(*BOOTED, gone, expect_units=("app", "hello"))
    assert not result.ok and result.exited == ("app.service",) and result.missing == ()
    assert "exited units: app.service" in result.summary()


def test_the_summary_shows_a_failed_units_own_lines(fake: Fake) -> None:
    lines = (
        "[    1.87] systemd[1]: Starting nftables.service...\n",
        "[    1.90] nft[155]: /etc/nftables.conf:7:3-30: Error: Could not process rule\n",
        "[    1.91] systemd[1]: nftables.service: Failed with result 'exit-code'.\n",
        "[FAILED] Failed to start nftables.service.\n",
        "[    1.92] chronyd[160]: chronyd version 4.6 starting\n",
    )
    result = fake.run(*BOOTED, *lines)
    summary = result.summary()
    assert result.failed == ("nftables.service",)
    section = summary.split("--- nftables.service ---\n", 1)[1].split("\n---", 1)[0]
    assert "nft[155]: /etc/nftables.conf:7:3-30: Error" in section
    assert "Failed with result 'exit-code'" in section and "chronyd" not in section


def test_qemu_exiting_with_an_error_after_the_markers_fails_the_boot(fake: Fake) -> None:
    result = fake.run(*BOOTED, hang=0, exit_code=3, settle=5)
    assert result.missing == () and result.returncode == 3 and not result.ok


def test_the_console_carries_the_journal() -> None:
    assert "systemd.journald.forward_to_console=1" in CONSOLE_CMDLINE


def test_qemu_command_boots_a_disk_on_a_snapshot_with_a_single_firmware(tmp_path: Path) -> None:
    firmware = tmp_path / "OVMF.fd"
    firmware.write_bytes(b"fw")
    code, store = find_ovmf(firmware)
    assert (code, store) == (firmware, None)  # no OVMF_VARS sibling: -bios
    argv = qemu_command(
        tmp_path / "disk.qcow2", tmp_path / "log", code=code, store=store, kvm=True, network=False
    )
    assert argv[argv.index("-bios") + 1] == str(firmware) and "-kernel" not in argv
    assert f"file={tmp_path / 'disk.qcow2'},format=qcow2,if=virtio,snapshot=on" in argv
    assert argv[argv.index("-machine") + 1] == "q35,accel=kvm"
    assert argv[argv.index("-cpu") + 1] == "host"
    assert argv[argv.index("-nic") + 1] == "none"


def test_cmdline_commas_are_escaped_for_smbios(tmp_path: Path) -> None:
    argv = qemu_command(
        tmp_path / "a.efi", tmp_path / "log", code=tmp_path / "c", store=None, cmdline=("a=1,2",)
    )
    assert argv[argv.index("-smbios") + 1].endswith("kernel-cmdline-extra=a=1,,2")
    assert "-smbios" not in qemu_command(
        tmp_path / "a.efi", tmp_path / "log", code=tmp_path / "c", store=None, cmdline=()
    )


def test_boot_takes_an_artifact_or_the_directory_holding_its_uki(fake: Fake) -> None:
    class Artifact:
        path = fake.image

    for artifact in (Artifact(), fake.image.parent, str(fake.image)):
        assert boot(artifact, qemu=str(fake.qemu), firmware=fake.code, timeout=0).command
    (fake.image.parent / "other.efi").write_bytes(b"MZ")
    with pytest.raises(ValueError, match="2 .efi files"):
        boot(fake.image.parent, qemu=str(fake.qemu), firmware=fake.code)
    with pytest.raises(FileNotFoundError):
        boot(fake.image.parent / "missing.efi", qemu=str(fake.qemu), firmware=fake.code)


def test_boot_without_qemu_is_unavailable(fake: Fake, boot: BootRunner) -> None:
    missing = str(fake.image.parent / "no-qemu")
    assert boot_unavailable(missing) == f"{missing} is not installed"
    assert "OVMF firmware not found" in str(
        boot_unavailable(str(fake.qemu), fake.code.parent / "x")
    )
    assert boot_unavailable(str(fake.qemu), fake.code) is None
    with pytest.raises(BootUnavailable):
        testing.boot(fake.image, qemu=missing)
    with pytest.raises(pytest.skip.Exception, match="cannot boot here"):
        boot(fake.image, qemu=missing)


def test_boot_fixture_logs_under_tmp_path(fake: Fake, boot: BootRunner, tmp_path: Path) -> None:
    fake.transcript.write_text("".join(BOOTED), encoding="utf-8")
    fake.monkeypatch.setenv("FAKE_QEMU_HANG", "30")
    result = boot(fake.image, qemu=str(fake.qemu), firmware=fake.code, settle=0, kvm=False)
    assert result.ok and result.log == tmp_path / "boot-0" / "console.log"


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        body = b"<h1>node 0.1.0</h1>\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def test_boot_waits_for_each_http_url_and_keeps_the_answer(fake: Fake) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        result = fake.run(*BOOTED, forward={18080: 8080, 2222: 22}, wait_http=(url,))
    finally:
        server.shutdown()
    result.assert_booted()
    assert result.http_get(url) == "<h1>node 0.1.0</h1>\n"
    argv = fake.command()
    assert argv[argv.index("-nic") + 1] == (
        "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:18080-:8080,hostfwd=tcp:127.0.0.1:2222-:22"
    )


def test_an_http_url_that_never_answers_fails_the_boot(fake: Fake) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{probe.getsockname()[1]}/"
    result = fake.run(*BOOTED, wait_http=(url,), timeout=3)
    assert not result.ok and result.timed_out and result.missing == (url,)
    with pytest.raises(AssertionError, match="did not answer"):
        result.http_get(url)
