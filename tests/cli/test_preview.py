"""`tundravm preview`: the explore page served from memory, re-rendered on every change."""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import cli
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.errors import ValidationError
from tundravm.explore import Exploration
from tundravm.preview import Preview, is_loopback, serve, url_of

RECIPE = """
from tundravm import File, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()


def build() -> Recipe:
    items = (Package("linux-image-amd64"), File("/etc/motd", "first-motd\\n"))
    return Recipe("previewed", items=items, variants=(Variant("default"),))
"""
LINE = re.compile(r"^\d\d:\d\d:\d\d (.*)$")
BANNER = 'id="preview-problems"'


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


@dataclass
class FakeServer:
    """What ``serve`` returns, minus the socket: records the preview and the stop."""

    server_address: tuple[str, int] = ("127.0.0.1", 8765)
    preview: Preview | None = None
    stopped: list[str] = field(default_factory=list)

    def shutdown(self) -> None:
        self.stopped.append("shutdown")

    def server_close(self) -> None:
        self.stopped.append("close")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    server = FakeServer()

    def fake_serve(preview: Preview, host: str, port: int) -> FakeServer:
        server.preview = preview
        server.server_address = (host, port)
        return server

    monkeypatch.setattr(cli, "serve", fake_serve)
    return server


def _preview(*argv: str, ticks: Iterator[object]) -> tuple[int, str, list[str]]:
    """Run ``tundravm preview *argv``; return the exit code, the URL line and the verdicts."""
    out = io.StringIO()
    code = main(["preview", *argv, "--interval", "0.001"], stdout=out, ticks=ticks)
    header, *lines = out.getvalue().splitlines()
    verdicts = []
    for line in lines:
        match = LINE.match(line)
        if match is not None:
            verdicts.append(match.group(1))
    return code, header, verdicts


def _page(preview: Preview) -> str:
    status, kind, body = preview.handle("/")
    assert (status, kind) == (200, "text/html; charset=utf-8")
    return body.decode("utf-8")


def test_preview_rerenders_on_each_edit_and_banners_a_broken_recipe(
    recipe: Path, fake: FakeServer
) -> None:
    good = recipe.read_text(encoding="utf-8")
    edited = good.replace("first-motd", "second-motd")
    seen: list[tuple[int, str]] = []

    def ticks() -> Iterator[object]:
        for text in (edited, edited + "def broken(:\n", edited):
            assert fake.preview is not None
            seen.append((fake.preview.version, _page(fake.preview)))
            recipe.write_text(text, encoding="utf-8")
            yield text
        assert fake.preview is not None
        seen.append((fake.preview.version, _page(fake.preview)))

    code, header, lines = _preview(str(recipe), ticks=ticks())

    assert code == EXIT_OK
    assert header == f"previewing {recipe} at http://127.0.0.1:8765/ (Ctrl-C stops)"
    assert [version for version, _ in seen] == [1, 2, 3, 4]
    first, second, broken, fixed = (page for _, page in seen)
    assert "first-motd" in first and BANNER not in first
    assert "second-motd" in second and "first-motd" not in second and BANNER not in second
    assert BANNER in broken and "second-motd" in broken  # the last good page stays
    assert "failed to load: showing the last good rendering" in broken
    assert "syntax error" in broken and re.search(r"<br>at <code>recipe\.py:\d+</code>", broken)
    assert "Hint: Run " in broken
    assert BANNER not in fixed and "second-motd" in fixed
    assert lines[0] == "v1 lint 0 errors 0 warnings"
    assert lines[2].startswith("v3 error [E_VALIDATION] Recipe recipe.py failed to load")
    assert fake.stopped == ["shutdown", "close"]


class Stub:
    """An exploration stand-in: the lint section and a page naming *text*."""

    def __init__(self, text: str, errors: int = 0) -> None:
        finding = {"code": "x-bad", "severity": "error", "message": "bad <thing>"}
        finding |= {"hint": "fix it", "location": "recipe.py:3"}
        self.model: dict[str, Any] = {
            "lint": {"errors": errors, "warnings": 0, "findings": [finding] * errors}
        }
        self.text = text

    def html(self) -> str:
        return f"<!doctype html><html><body><p>{self.text}</p></body></html>"


def _loader(*steps: Stub | Exception) -> Any:
    queue = list(steps)

    def load() -> Exploration:
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        return cast(Exploration, step)

    return load


def test_preview_banners_lint_errors_over_the_fresh_page_and_skips_unchanged() -> None:
    failed = ValidationError("Recipe recipe.py failed to load.", hint="look")
    preview = Preview(_loader(failed, Stub("one"), Stub("one"), Stub("two", errors=1)))
    assert preview.version == 0 and "Rendering the recipe..." in _page(preview)

    assert preview.render() == "v1 error [E_VALIDATION] Recipe recipe.py failed to load."
    assert "No rendering yet." in _page(preview) and "showing no rendering yet" in _page(preview)
    assert preview.render() == "v2 lint 0 errors 0 warnings"
    assert preview.render() == "v2 lint 0 errors 0 warnings"  # same page: no reload
    assert preview.render() == "v3 lint 1 error 0 warnings"
    page = _page(preview)
    assert page.index(BANNER) < page.index("<p>two</p>") < page.index('var shown = "3"')
    assert "Lint found 1 error." in page and "bad &lt;thing&gt;" in page
    assert "<code>recipe.py:3</code>" in page and "Hint: fix it" in page
    assert preview.handle("/version") == (200, "text/plain; charset=utf-8", b"3\n")
    assert preview.handle("/version?t=1")[2] == b"3\n"
    assert preview.handle("/nope")[0] == 404


def test_serve_answers_page_and_version_over_a_real_socket() -> None:
    preview = Preview(_loader(Stub("served")))
    preview.render()
    server = serve(preview, "127.0.0.1", 0)
    try:
        url = url_of(server)
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", url)
        with urllib.request.urlopen(url) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert "<p>served</p>" in response.read().decode("utf-8")
        with urllib.request.urlopen(url + "version") as response:
            assert response.read() == b"1\n"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(url + "missing")
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_preview_cli_serves_on_an_ephemeral_port(recipe: Path) -> None:
    out = io.StringIO()
    fetched: list[bytes] = []

    def ticks() -> Iterator[object]:
        url = re.search(r"http://\S+", out.getvalue())
        assert url is not None
        for path in ("", "version"):
            with urllib.request.urlopen(url.group(0) + path) as response:
                fetched.append(response.read())
        yield "fetched"

    argv = ["preview", str(recipe), "--port", "0", "--interval", "0.001"]
    assert main(argv, stdout=out, ticks=ticks()) == EXIT_OK
    page, version = fetched
    assert b"first-motd" in page and version == b"1\n"


def test_preview_refuses_a_remote_host_unless_allowed(
    recipe: Path,
    fake: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_main("preview", str(recipe), "--host", "0.0.0.0")[0] == EXIT_SDK_ERROR
    assert "not a loopback address" in capsys.readouterr().err and fake.preview is None

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    code, header, _ = _preview(
        str(recipe), "--host", "0.0.0.0", "--allow-remote", "--open", ticks=iter(())
    )
    assert code == EXIT_OK and header.endswith("at http://0.0.0.0:8765/ (Ctrl-C stops)")
    assert "anyone who can reach it" in capsys.readouterr().err
    assert opened == ["http://0.0.0.0:8765/"]


def test_preview_ctrl_c_exits_zero(recipe: Path, fake: FakeServer) -> None:
    def ticks() -> Iterator[object]:
        raise KeyboardInterrupt
        yield

    out = io.StringIO()
    assert main(["preview", str(recipe)], stdout=out, ticks=ticks()) == EXIT_OK
    assert out.getvalue().endswith("stopped\n") and fake.stopped == ["shutdown", "close"]


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("127.0.0.1", True), ("127.1.2.3", True), ("::1", True), ("localhost", True)]
    + [("0.0.0.0", False), ("192.168.1.2", False), ("example.com", False)],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback
