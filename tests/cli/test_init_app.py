"""`tundravm init --app go`: a Go program as a service, and tests over the planned image."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import tundravm
from tests.helpers import REPO_ROOT, run_main
from tundravm import Disk, ExternalDelivery, Git, Secrets
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.testing import UPDATE_GOLDEN_ENV, plan

SOURCE = ("--source", "https://github.com/acme/hello@v1.4.0")
NO_DOCTOR = ("--backend", "inprocess", "--no-doctor")


def scaffold(project: Path, *flags: str) -> tundravm.Recipe:
    code, out = run_main("init", str(project), "--name", "hello", "--app", "go", *NO_DOCTOR, *flags)
    assert code == EXIT_OK, out
    assert "run test_hello.py on the planned image" in out
    return tundravm.load_recipe(project / "hello.py")


def generated_tests(project: Path) -> str:
    env = {key: value for key, value in os.environ.items() if key != UPDATE_GOLDEN_ENV}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(project)]
    done = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
    return done.stdout


def ruff_clean(project: Path) -> None:
    ruff = Path(sys.executable).parent / "ruff"
    for args in (("check", "--no-cache"), ("format", "--check", "--no-cache")):
        done = subprocess.run([ruff, *args, str(project)], capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr


def test_app_recipe_builds_the_binary_and_runs_it_as_a_service(tmp_path: Path) -> None:
    recipe = scaffold(tmp_path, *SOURCE, "--package", "./cmd/hello", "--port", "9000")
    image = plan(recipe)
    build = image.builds["hello"]
    assert build.source == Git("https://github.com/acme/hello", ref="v1.4.0")
    assert [install.destination for install in build.install] == ["/usr/bin/hello"]
    service = image.service("hello")
    assert service.exec_start == "/usr/bin/hello --addr=:9000"
    assert service.user == "hello" and service.enabled
    # StateDirectory= (systemd sets $STATE_DIRECTORY) under the strict sandbox, not a mkdir.
    assert service.values("Service", "StateDirectory") == ("hello",)
    assert service.values("Service", "ExecStartPre") == ()
    assert service.values("Service", "ProtectSystem") == ("strict",)
    assert {d.code for d in image.lint() if d.severity != "info"} == {"source-unpinned"}
    assert "6 passed" in generated_tests(tmp_path)
    ruff_clean(tmp_path)


def test_disk_and_external_secret(tmp_path: Path) -> None:
    flags = ("--disk", "data", "--secret", "api-token", "--delivery", "external:vault-agent")
    recipe = scaffold(tmp_path, *SOURCE, *flags)
    image = plan(recipe)
    (disk,) = [item for item in image.resolved.items if isinstance(item, Disk)]
    assert (disk.name, disk.device, disk.mount) == ("data", "/dev/vdb", "/persistent")
    (secrets,) = [item for item in image.resolved.items if isinstance(item, Secrets)]
    token = "/run/hello/api-token"
    assert secrets.delivery == ExternalDelivery("vault-agent", supports=("file",), ready_path=token)
    after = " ".join(image.service("hello").values("Unit", "After"))
    assert "runtime-init-delivery-vault-agent.service" in after
    assert "runtime-init-secrets.service" not in after  # not the SSH-key step
    assert secrets.port == 8081  # the app has 8080
    environment = image.service("hello").environment
    assert environment == {
        "API_TOKEN_FILE": "/run/credentials/hello.service/api-token",
        "STATE_DIRECTORY": "/persistent/hello",
    }
    unit = image.service("hello")
    assert unit.values("Service", "LoadCredential") == (f"api-token:{token}",)
    assert unit.values("Service", "ReadWritePaths") == ("/persistent/hello",)
    assert "DISK_DEVICE = " in (tmp_path / "hello.py").read_text(encoding="utf-8")
    assert "8 passed" in generated_tests(tmp_path)
    ruff_clean(tmp_path)


def test_external_secret_without_a_disk_waits_for_its_delivery(tmp_path: Path) -> None:
    recipe = scaffold(tmp_path, *SOURCE, "--secret", "token", "--delivery", "external:writer")
    image = plan(recipe)
    after = " ".join(image.service("hello").values("Unit", "After"))
    assert after.startswith("runtime-init-delivery-writer.service")
    text = (tmp_path / "hello.py").read_text(encoding="utf-8")
    assert "YOU PROVIDE IT" in text and "needs=(delivery.ready,)" in text
    assert "7 passed" in generated_tests(tmp_path)
    ruff_clean(tmp_path)


def test_path_builds_the_checkout_branch(tmp_path: Path) -> None:
    checkout = tmp_path / "src"
    checkout.mkdir()
    git = ("git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@example.com")
    subprocess.run([*git, "init", "-q", "-b", "trunk"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    recipe = scaffold(tmp_path / "project", "--path", str(checkout))
    assert plan(recipe).builds["hello"].source == Git(str(checkout.resolve()), ref="trunk")


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ((), "--app go needs the program's source"),
        (("--source", "https://github.com/acme/hello"), "names no ref"),
        (("--path", "."), "is not a git checkout"),
        ((*SOURCE, "--secret", "token"), "needs --delivery external:NAME"),
        ((*SOURCE, "--secret", "token", "--delivery", "tundra-tools"), "external:NAME"),
        ((*SOURCE, "--delivery", "external:vault"), "--delivery needs --secret"),
        ((*SOURCE, "--port", "80"), "outside 1024-65535"),
        ((*SOURCE, "--disk", "bad name"), "is not a valid name"),
        ((*SOURCE, "--template", "minimal"), "choose different starter recipes"),
    ],
)
def test_app_options_are_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _ = run_main("init", ".", "--app", "go", *NO_DOCTOR, *flags)
    assert code == EXIT_SDK_ERROR
    assert message in capsys.readouterr().err
    assert not (tmp_path / f"{tmp_path.name}.py").exists()


def test_app_options_need_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run_main("init", str(tmp_path), *NO_DOCTOR, *SOURCE)
    assert code == EXIT_SDK_ERROR
    assert "--source configures the app recipe and needs --app go" in capsys.readouterr().err
