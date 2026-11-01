"""``check`` (declarations, lint, lock drift, tree drift) and ``lock`` (write, ``--check``,
``--update``)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.cli.conftest import STARTER, Cli
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
from tundravm.templates import render_recipe_template

BROKEN = """\
from tundravm.declarative import Disk, Key, Recipe

recipe = Recipe("b", items=(Disk("d", "/data", key=Key("k")),))
"""


def _change(project: Path) -> None:
    (project / "demo.py").write_text(
        STARTER.replace('packages=["busybox"]', 'packages=["busybox", "jq"]'), encoding="utf-8"
    )


def test_a_clean_image_checks_clean_and_says_there_is_no_lock(project: Path, cli: Cli) -> None:
    result = cli("check")
    assert result.code == EXIT_OK, result.out
    assert "0 errors" in result.out
    assert "no lockfile at build/tundravm.lock" in result.err


def test_errors_exit_1_in_text_and_json(
    tmp_path: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")
    result = cli("check", "broken.py")
    assert result.code == EXIT_FAILURE
    assert "error disk-key-undefined [default]" in result.out
    assert result.out.splitlines()[-1] == "explain: tundravm check --explain disk-key-undefined"
    result = cli("check", "broken.py", "--json")
    assert result.code == EXIT_FAILURE
    payload = json.loads(result.out)
    assert payload["schema"] == "tundravm.check/1" and payload["passed"] is False
    assert payload["summary"]["errors"] >= 1 and payload["tree"] is None
    assert [d["code"] for d in payload["diagnostics"]][0] == "disk-key-undefined"


def test_explain_prints_what_a_code_checks_and_its_fix(cli: Cli) -> None:
    result = cli("check", "--explain", "source-unpinned", "--explain", "tree-drift", "demo.py")
    assert result.code == EXIT_OK, result.err
    assert result.out.startswith("source-unpinned: warning (error when the policy refuses")
    assert "\n    fix: Run `tundravm lock IMAGE`" in result.out
    assert "\n\ntree-drift: error\n" in result.out
    unknown = cli("check", "--explain", "source-unpined")
    assert unknown.code == EXIT_SDK_ERROR
    assert "Did you mean 'source-unpinned'? docs/reference/lint.md lists" in unknown.err


def test_a_components_code_is_explained_through_the_image_that_declares_it(
    project: Path, cli: Cli
) -> None:
    source = render_recipe_template(
        title="demo", filename="demo.py", release="trixie", template="cloud"
    )
    (project / "demo.py").write_text(source.replace("APP_PORT = 8080", "APP_PORT = 80"))
    report = cli("check")
    assert report.code == EXIT_FAILURE
    assert report.out.splitlines()[-1] == "explain: tundravm check --explain app-privileged-port"
    explained = cli("check", "--explain", "app-privileged-port")
    assert explained.code == EXIT_OK, explained.err
    assert explained.out.splitlines()[:2] == [
        "app-privileged-port: error",
        "    declared by App in demo.py",
    ]


def test_an_unknown_profile_is_refused(project: Path, cli: Cli) -> None:
    result = cli("check", "--profile", "staging")
    assert result.code == EXIT_SDK_ERROR
    assert "Unknown profile(s): staging" in result.err


def test_a_drifted_lock_fails_check(project: Path, cli: Cli) -> None:
    assert cli("lock").code == EXIT_OK
    assert cli("check").code == EXIT_OK
    _change(project)
    result = cli("check")
    assert result.code == EXIT_FAILURE
    assert "lock-changed" in result.out


def test_tree_drift(project: Path, cli: Cli) -> None:
    written = cli("build", "--tree-only", "mkosi")
    assert written.code == EXIT_OK, written.err
    assert "wrote mkosi" in written.out and "production: mkosi -C mkosi build" in written.out
    assert "dev: mkosi -C mkosi --profile=dev build" in written.out
    assert not Path("build/demo").exists()
    clean = cli("check", "--tree", "mkosi")
    assert clean.code == EXIT_OK and "tree mkosi is up to date" in clean.out
    page = project / "mkosi/payload/mkosi.extra/srv/www/index.html"
    page.write_text("<h1>edited</h1>\n", encoding="utf-8")
    drifted = cli("check", "--tree", "mkosi", "--json")
    assert drifted.code == EXIT_FAILURE
    payload = json.loads(drifted.out)
    assert payload["tree"]["clean"] is False
    assert {d["code"] for d in payload["diagnostics"]} >= {"tree-drift"}
    text = cli("check", "--tree", "mkosi")
    assert "payload/mkosi.extra/srv/www/index.html: modified" in text.out


def test_lock_writes_then_check_reports_drift(project: Path, cli: Cli) -> None:
    result = cli("lock")
    assert result.code == EXIT_OK
    assert result.out.startswith("locked build/tundravm.lock")
    assert (project / "build" / "tundravm.lock").is_file()
    clean = cli("lock", "--check")
    assert clean.code == EXIT_OK and "up to date" in clean.out
    _change(project)
    before = (project / "build" / "tundravm.lock").read_bytes()
    stale = cli("lock", "--check")
    assert stale.code == EXIT_FAILURE
    assert "packages" in stale.out
    assert (project / "build" / "tundravm.lock").read_bytes() == before
    report = json.loads(cli("lock", "--check", "--json").out)
    assert report["schema"] == "tundravm.lock/1" and report["clean"] is False


def test_lock_check_without_a_lock_is_drift(project: Path, cli: Cli) -> None:
    result = cli("lock", "--check")
    assert result.code == EXIT_FAILURE
    assert result.out == "no lockfile at build/tundravm.lock: run `tundravm lock` and commit it\n"


def test_lock_update_resolves_again(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli("lock").code == EXIT_OK
    import tundravm._source as source_module

    monkeypatch.setattr(source_module, "default_resolver", lambda source: "2" * 40)
    kept = cli("lock")
    assert kept.code == EXIT_OK
    assert "1" * 40 in (project / "build" / "tundravm.lock").read_text()
    named = json.loads(cli("lock", "--update", "tdxs", "--json").out)
    assert named["updated"] == ["tdxs"]
    assert "2" * 40 in (project / "build" / "tundravm.lock").read_text()


def test_lock_update_without_names_updates_every_input(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli("lock").code == EXIT_OK
    import tundravm._source as source_module

    monkeypatch.setattr(source_module, "default_resolver", lambda source: "3" * 40)
    result = cli("lock", "--update", "--json")
    assert result.code == EXIT_OK, result.err
    assert json.loads(result.out)["updated"]
    assert "3" * 40 in (project / "build" / "tundravm.lock").read_text()


def test_lock_and_check_together_is_a_usage_error(project: Path, cli: Cli) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli("lock", "--check", "--update")
    assert exit_.value.code == EXIT_SDK_ERROR


LOCAL = """\
from tundravm import Image, Service
from tundravm.sources import GoBinary, Local

app = GoBinary("app", Local("app"), package=".")

image = Image("demo", services=[Service("app", [app])])
"""


def test_a_local_source_is_pinned_by_its_content(project: Path, cli: Cli) -> None:
    (project / "demo.py").write_text(LOCAL, encoding="utf-8")
    (project / "app").mkdir()
    main = project / "app" / "main.go"
    main.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    unpinned = cli("check")
    assert unpinned.code == EXIT_OK
    assert "source-unpinned [production] app: source build 'app' (local app)" in unpinned.out
    assert cli("lock").code == EXIT_OK
    assert cli("lock", "--check").out == "lock is up to date\n"
    main.write_text("package main\n\nfunc main() { println() }\n", encoding="utf-8")
    drift = cli("lock", "--check")
    assert drift.code == EXIT_FAILURE
    assert drift.out.startswith("~ sources.app: local-changed: app changed since the lock (")
    checked = cli("check")
    assert "error local-changed [image] sources.app: app changed since the lock (" in checked.out
    assert cli("lock").code == EXIT_OK and cli("build").code == EXIT_OK
    pin = json.loads((project / "build" / "tundravm.lock").read_text())["fetches"][0]
    assert (pin["name"], pin["kind"], pin["source"]) == ("app", "local", "app")
    shown = cli("show", "latest").out
    assert re.search(rf"\n  app +local +- +{pin['digest'][:12]} ", shown), shown


REPINNED = "lock: app re-pinned, local source changed\n"


@pytest.mark.parametrize(
    ("args", "edit", "code", "said"),
    [
        (("--dev",), True, EXIT_OK, REPINNED),
        ((), True, EXIT_SDK_ERROR, "local-changed: app changed since the lock"),
        (("--dev",), False, EXIT_OK, ""),
    ],
    ids=["dev re-pins", "production refuses", "unchanged says nothing"],
)
def test_a_dev_build_re_pins_a_changed_local_source(
    project: Path, cli: Cli, args: tuple[str, ...], edit: bool, code: int, said: str
) -> None:
    dev = 'services=[Service("app", [app])], dev=DevAccess(password="tdx"))'
    source = LOCAL.replace('services=[Service("app", [app])])', dev)
    (project / "demo.py").write_text(
        "from tundravm.components import DevAccess\n" + source, encoding="utf-8"
    )
    (project / "app").mkdir()
    main = project / "app" / "main.go"
    main.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    assert cli("lock").code == EXIT_OK
    if edit:
        main.write_text("package main\n\nfunc main() { println() }\n", encoding="utf-8")
    result = cli("build", *args)
    assert result.code == code, result.err
    if code == EXIT_OK:
        assert ("re-pinned" in result.out) == bool(said) and result.out.startswith(said)
        assert cli("lock", "--check").out == "lock is up to date\n"
    else:
        assert said in result.err and "run `tundravm lock`" in result.err
