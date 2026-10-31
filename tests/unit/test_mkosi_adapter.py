"""tundravm._mkosi: the pinned mkosi symbols, invoke in a real fork, errors, sudo plumbing."""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from mkosi.sandbox import CAP_SYS_ADMIN, CLONE_NEWNS

from tests.helpers import REPO_ROOT
from tundravm import _mkosi
from tundravm.toolchain import MKOSI_PIN

pytestmark = pytest.mark.boundary

SRC = REPO_ROOT / "src" / "tundravm"
IMPORTS_MKOSI = re.compile(r"^\s*(import mkosi\b|from mkosi(\.| )\S* ?import\b)", re.MULTILINE)


def test_only_the_adapter_imports_mkosi() -> None:
    hits = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "_mkosi.py" and IMPORTS_MKOSI.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
    assert IMPORTS_MKOSI.search((SRC / "_mkosi.py").read_text(encoding="utf-8"))


def _parameters(function: Any) -> list[str]:
    return list(inspect.signature(function).parameters)


def test_every_private_mkosi_symbol_the_adapter_uses_exists_in_the_pin() -> None:
    """A mkosi bump that renames or reshapes one of these fails here, not at a first build."""
    import importlib

    import mkosi
    import mkosi.config
    import mkosi.kmod
    import mkosi.log
    import mkosi.resources
    import mkosi.sandbox
    import mkosi.util

    run = importlib.import_module("mkosi.run")  # `mkosi.run` is also mkosi's run() function

    assert mkosi.sandbox.__version__ == MKOSI_PIN.version == _mkosi.PINNED_VERSION
    assert _parameters(mkosi.config.parse_config) == ["argv", "resources"]
    assert _parameters(mkosi.run_verb) == ["args", "tools", "images", "resources"]
    assert _parameters(run.fork_and_wait) == ["target", "args", "sandbox", "kwargs"]
    assert _parameters(mkosi.util.resource_path) == ["mod"]
    assert _parameters(mkosi.log.log_setup) == ["default_log_level"]
    assert _parameters(mkosi.config.Config.to_dict) == ["self"]
    assert _parameters(mkosi.sandbox.acquire_privileges) == [
        "identity",
        "foreign",
        "delegate_ranges",
        "become_root",
        "network",
    ]
    assert _parameters(mkosi.sandbox.have_effective_cap) == ["capability"]
    assert _parameters(mkosi.sandbox.unshare) == ["flags"]
    assert mkosi.sandbox.CAP_SYS_ADMIN == 21
    assert mkosi.sandbox.CLONE_NEWNS == 0x00020000
    assert issubclass(mkosi.sandbox.VarlinkError, Exception)
    assert _parameters(mkosi.kmod.globs_match_module) == ["name", "globs"]
    assert _parameters(mkosi.kmod.normalize_module_name) == ["name"]
    assert _parameters(mkosi.kmod.normalize_module_glob) == ["name"]
    setting = mkosi.config.SETTINGS_LOOKUP_BY_NAME["ToolsTree"]
    for attribute in ("name", "section", "dest", "parse", "scope", "choices", "tools"):
        assert hasattr(setting, attribute)
    assert mkosi.resources.__name__ == "mkosi.resources"


def test_the_pin_matches_pyproject_and_uv_lock() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'mkosi = {{ git = "{MKOSI_PIN.repository}", rev = "{MKOSI_PIN.tag}" }}' in pyproject
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    source = f"{MKOSI_PIN.repository}?rev={MKOSI_PIN.tag}#{MKOSI_PIN.commit}"
    assert f'name = "mkosi"\nversion = "{MKOSI_PIN.version}"\nsource = {{ git = "{source}" }}' in (
        lock
    )


def test_importing_the_adapter_with_another_mkosi_fails_naming_both_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    import mkosi.sandbox

    monkeypatch.setattr(mkosi.sandbox, "__version__", "26")
    monkeypatch.delitem(sys.modules, "tundravm._mkosi")
    try:
        with pytest.raises(ImportError, match=r"needs mkosi 27\.1, but mkosi 26 is installed"):
            importlib.import_module("tundravm._mkosi")
    finally:
        sys.modules["tundravm._mkosi"] = _mkosi


# ── invoke, in a real fork ───────────────────────────────────────────────


class _Args:
    debug = False


def _fake_parse(argv: Sequence[str], *, resources: Path) -> tuple[_Args, None, tuple[()]]:
    assert resources.is_dir()
    return _Args(), None, ()


def _patch(monkeypatch: pytest.MonkeyPatch, run_verb: Any) -> None:
    monkeypatch.setattr(_mkosi, "parse_config", _fake_parse)
    monkeypatch.setattr(_mkosi, "run_verb", run_verb)


def test_invoke_runs_the_verb_in_a_child_and_logs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = os.getpid()

    def run_verb(args: Any, tools: Any, images: Any, *, resources: Path) -> None:
        assert os.getpid() != parent
        assert Path.cwd() == tmp_path
        logging.info("Building main image")
        os.write(2, b"apt: unpacking\n")
        os.write(1, b"a build script line\n")

    _patch(monkeypatch, run_verb)
    seen: list[str] = []
    log = tmp_path / "logs" / "mkosi.log"

    result = _mkosi.invoke(["build"], cwd=tmp_path, log=log, on_output=seen.append)

    assert result.argv == ("build",)
    assert result.log == log
    text = log.read_text(encoding="utf-8")
    for line in ("Building main image", "apt: unpacking", "a build script line"):
        assert line in text
        assert any(line in shown for shown in seen)


def test_the_child_gives_scripts_a_real_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkosi runs scripts with ``stdin=sys.stdin``; pytest's capture object has no fileno()."""

    def run_verb(args: Any, tools: Any, images: Any, *, resources: Path) -> None:
        subprocess.run(["cat"], stdin=sys.stdin, check=True)
        assert sys.stdin.read() == ""

    _patch(monkeypatch, run_verb)

    _mkosi.invoke(["build"], cwd=tmp_path, log=tmp_path / "log", on_output=lambda _line: None)


def test_invoke_returns_stdout_bigger_than_a_pipe_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fork_and_wait's own return pipe would deadlock past 64 KiB; stdout goes via a file."""
    pad = "x" * 300_000
    payload = {"Tools": None, "Images": [{"Image": "main", "Pad": pad}]}

    def run_verb(args: Any, tools: Any, images: Any, *, resources: Path) -> None:
        print(json.dumps(payload))

    _patch(monkeypatch, run_verb)

    result = _mkosi.invoke(["summary"], cwd=tmp_path, log=tmp_path / "log", stdout=True)

    assert _mkosi.parse_summary(result.stdout).main["Pad"] == pad


def test_a_die_in_the_child_raises_mkosi_error_with_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mkosi.log import die

    def run_verb(args: Any, tools: Any, images: Any, *, resources: Path) -> None:
        logging.info("Resolving packages")
        die("No configuration found", hint="Run mkosi from a directory with configuration")

    _patch(monkeypatch, run_verb)

    with pytest.raises(_mkosi.MkosiError) as caught:
        _mkosi.invoke(["build"], cwd=tmp_path, log=tmp_path / "log", on_output=lambda _: None)

    assert caught.value.returncode == 1
    assert caught.value.argv == ("build",)
    assert "Resolving packages" in caught.value.log_text
    assert "No configuration found" in caught.value.log_text
    assert "No configuration found" in str(caught.value)


def test_a_failed_fork_becomes_mkosi_error_carrying_its_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fork_and_wait(target: Any, *args: Any, **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(4, ["self"])

    monkeypatch.setattr(_mkosi, "fork_and_wait", fork_and_wait)

    with pytest.raises(_mkosi.MkosiError) as caught:
        _mkosi.invoke(["--force", "build"], cwd=tmp_path, log=tmp_path / "log")

    assert caught.value.returncode == 4
    assert caught.value.argv == ("--force", "build")


def test_summary_passes_force_json_and_no_pager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    tools = {"Distribution": "debian", "Release": "trixie", "Snapshot": "20251020T000000Z"}

    def invoke(argv: Sequence[str], **kwargs: Any) -> _mkosi.MkosiResult:
        calls.append((list(argv), kwargs))
        body = json.dumps({"Tools": tools, "Images": [{"Image": "qemu"}, {"Image": "main"}]})
        return _mkosi.MkosiResult(tuple(argv), body, kwargs["log"])

    monkeypatch.setattr(_mkosi, "invoke", invoke)

    found = _mkosi.summary(tmp_path, extra_argv=["--profile=dev"])

    ((argv, kwargs),) = calls
    assert argv == [
        "--directory",
        str(tmp_path),
        "--force",
        "--no-pager",
        "--profile=dev",
        "summary",
        "--json",
    ]
    assert kwargs["stdout"] is True
    assert found.tools == tools
    assert [image["Image"] for image in found.images] == ["qemu", "main"]
    assert found.main == {"Image": "main"}


def test_summary_of_a_real_tree_needs_no_privileges(tmp_path: Path) -> None:
    """The real parse_config + run_verb in a real fork: summary returns before mkosi asks for
    a user namespace, so it works on any Linux host."""
    (tmp_path / "mkosi.conf").write_text(
        "[Distribution]\nDistribution=debian\nRelease=trixie\n\n"
        "[Build]\nToolsTree=default\nToolsTreeDistribution=debian\nToolsTreeRelease=trixie\n",
        encoding="utf-8",
    )

    found = _mkosi.summary(tmp_path)

    assert found.main["Distribution"] == "debian"
    assert found.main["Release"] == "trixie"
    assert found.tools is not None
    assert (found.tools["Distribution"], found.tools["Release"]) == ("debian", "trixie")


def test_interrupt_signals_reach_the_wait_as_keyboard_interrupt() -> None:
    with _mkosi._forward_signals() as interrupted:
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
        assert interrupted()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


# ── privilege ────────────────────────────────────────────────────────────


def test_root_needs_no_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    _mkosi.needs_sudo.cache_clear()
    monkeypatch.setattr(_mkosi, "have_effective_cap", lambda cap: cap == CAP_SYS_ADMIN)
    monkeypatch.setattr(_mkosi, "fork_and_wait", pytest.fail)
    try:
        assert _mkosi.needs_sudo() is False
    finally:
        _mkosi.needs_sudo.cache_clear()


@pytest.mark.parametrize(("userns", "expected"), [(True, False), (False, True)])
def test_needs_sudo_follows_the_forked_user_namespace_probe(
    monkeypatch: pytest.MonkeyPatch, userns: bool, expected: bool
) -> None:
    _mkosi.needs_sudo.cache_clear()
    monkeypatch.setattr(_mkosi, "have_effective_cap", lambda cap: False)
    probed: list[Any] = []

    def fork_and_wait(target: Any) -> bool:
        probed.append(target)
        return userns

    monkeypatch.setattr(_mkosi, "fork_and_wait", fork_and_wait)
    try:
        assert _mkosi.needs_sudo() is expected
    finally:
        _mkosi.needs_sudo.cache_clear()
    assert probed == [_mkosi._probe_userns]


def test_the_probe_falls_back_from_nsresourced_to_a_plain_user_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def acquire(**kwargs: Any) -> bool:
        calls.append(kwargs)
        if kwargs.get("foreign"):
            raise FileNotFoundError("/run/systemd/io.systemd.NamespaceResource")
        return True

    unshared: list[int] = []
    monkeypatch.delenv("MKOSI_FORCE_USERNS_FALLBACK", raising=False)
    monkeypatch.setattr(_mkosi, "acquire_privileges", acquire)
    monkeypatch.setattr(_mkosi, "unshare", unshared.append)

    assert _mkosi._probe_userns() is True
    assert calls == [{"foreign": True}, {}]
    assert unshared == [CLONE_NEWNS]


def test_the_probe_says_no_when_the_kernel_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**kwargs: Any) -> bool:
        if kwargs:
            raise ConnectionRefusedError()
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(_mkosi, "acquire_privileges", refuse)

    assert _mkosi._probe_userns() is False


def test_sudo_argv_reexecutes_this_interpreter_with_the_adapter(tmp_path: Path) -> None:
    argv = _mkosi.sudo_argv(
        ["--directory=/t", "build"],
        cwd=tmp_path,
        log=tmp_path / "mkosi.log",
        result=tmp_path / "result.json",
    )

    assert argv == [
        "sudo",
        "-E",
        sys.executable,
        "-m",
        "tundravm._mkosi",
        "--cwd",
        str(tmp_path),
        "--log",
        str(tmp_path / "mkosi.log"),
        "--result",
        str(tmp_path / "result.json"),
        "--",
        "--directory=/t",
        "build",
    ]
    assert "--stdout" in _mkosi.sudo_argv(
        ["summary"], cwd=tmp_path, log=tmp_path / "l", result=tmp_path / "r", stdout=True
    )


def test_the_module_entry_runs_invoke_and_writes_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[list[str], dict[str, Any]]] = []

    def invoke(argv: Sequence[str], **kwargs: Any) -> _mkosi.MkosiResult:
        ran.append((list(argv), kwargs))
        return _mkosi.MkosiResult(tuple(argv), '{"Images": []}', kwargs["log"])

    monkeypatch.setattr(_mkosi, "invoke", invoke)
    result = tmp_path / "result.json"
    log = tmp_path / "mkosi.log"
    entry = ["--cwd", str(tmp_path), "--log", str(log), "--result", str(result), "--stdout"]

    code = _mkosi.main([*entry, "--", "--force", "summary", "--json"])

    assert code == 0
    assert ran == [
        (["--force", "summary", "--json"], {"cwd": tmp_path, "log": log, "stdout": True})
    ]
    assert _mkosi.read_result(result) == _mkosi.SudoOutcome(0, '{"Images": []}')


def test_the_module_entry_reports_mkosis_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invoke(argv: Sequence[str], **kwargs: Any) -> _mkosi.MkosiResult:
        raise _mkosi.MkosiError(argv, "‣ boom\n", 2)

    monkeypatch.setattr(_mkosi, "invoke", invoke)
    result = tmp_path / "result.json"
    entry = ["--cwd", str(tmp_path), "--log", str(tmp_path / "l"), "--result", str(result)]

    assert _mkosi.main([*entry, "--", "build"]) == 2
    assert _mkosi.read_result(result) == _mkosi.SudoOutcome(2, "")


def test_read_result_is_none_when_nothing_ran(tmp_path: Path) -> None:
    assert _mkosi.read_result(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("sudo: a password is required", encoding="utf-8")
    assert _mkosi.read_result(tmp_path / "bad.json") is None


# ── read-only helpers ────────────────────────────────────────────────────


def test_setting_reports_section_and_parser() -> None:
    found = _mkosi.setting("ToolsTreeSnapshot")
    assert found is not None
    assert (found.name, found.section, found.dest) == (
        "ToolsTreeSnapshot",
        "Build",
        "tools_tree_snapshot",
    )
    assert _mkosi.setting("NoSuchSetting") is None
    output = _mkosi.setting("Format")
    assert output is not None and "uki" in output.choices


def test_kernel_module_globs_match_like_mkosi() -> None:
    match = _mkosi.kernel_module_globs_match
    assert match("kernel/drivers/net/virtio_net.ko.zst", ["virtio_net"])
    assert match("kernel/drivers/net/virtio_net.ko.zst", ["virtio-net"])
    assert match("kernel/drivers/net/virtio_net.ko", ["drivers/net/"])
    assert not match("kernel/drivers/net/virtio_net.ko", ["virtio_net", "-virtio_net"])
    assert not match("kernel/fs/ext4/ext4.ko", ["virtio_*"])
