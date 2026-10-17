"""``tundravm preview``: serve the explore page and reload it as the recipe changes.

:class:`Preview` holds the page: :func:`tundravm.explore.explore`'s rendering of
the recipe, re-rendered by :meth:`Preview.render` whenever
:class:`tundravm.watch.Watch` sees a source change. :attr:`Preview.version`
counts the distinct pages; the page carries a small script that polls
``/version`` every second and reloads when it moves. A recipe that fails to
load keeps the last good rendering under an error banner (message, hint,
``file:line``); lint errors show the same banner above the fresh rendering.
:func:`serve` binds a stdlib :class:`~http.server.ThreadingHTTPServer` for it.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .errors import TdxError
from .explore import Exploration
from .watch import plural

Response = tuple[int, str, bytes]
"""What :meth:`Preview.handle` answers: status, content type and body."""

_BODY = re.compile(r"<body[^>]*>", re.IGNORECASE)
_HTML = "text/html; charset=utf-8"
_TEXT = "text/plain; charset=utf-8"


@dataclass(frozen=True, slots=True)
class Problem:
    """One entry of the error banner, with its *code*, *hint* and *location* when known."""

    message: str
    code: str = ""
    hint: str | None = None
    location: str = ""

    @classmethod
    def of(cls, exc: TdxError) -> Problem:
        location = exc.context.get("location", "")
        return cls(str(exc.args[0]), exc.code, exc.hint, location)

    @classmethod
    def finding(cls, entry: Mapping[str, Any]) -> Problem:
        """A lint finding as the exploration model records it."""
        hint = entry.get("hint")
        location = str(entry.get("location") or "")
        return cls(str(entry["message"]), str(entry["code"]), hint, location)


@dataclass(slots=True)
class Preview:
    """The page the server shows: the last good rendering under the current banner.

    *load* gathers the exploration, raising :class:`TdxError` when the recipe does
    not load. :attr:`version` starts at 0 (a placeholder page) and moves by one
    each time :meth:`render` produces a different page.
    """

    load: Callable[[], Exploration]
    version: int = 0
    body: str = ""
    heading: str = ""
    problems: tuple[Problem, ...] = ()
    page: bytes = b""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.page = _page(self.body, self.heading, self.problems, self.version)

    def render(self) -> str:
        """Render the recipe again; return the line ``watch`` prints for it."""
        try:
            found = self.load()
        except TdxError as exc:
            kept = "the last good rendering" if self.body else "no rendering yet"
            heading = f"The recipe failed to load: showing {kept}."
            self.publish(self.body, heading, (Problem.of(exc),))
            return f"v{self.version} error [{exc.code}] {exc.args[0]}"
        lint: Mapping[str, Any] = found.model["lint"]
        errors = tuple(Problem.finding(f) for f in lint["findings"] if f["severity"] == "error")
        heading = f"Lint found {plural(len(errors), 'error')}." if errors else ""
        self.publish(found.html(), heading, errors)
        counts = f"{plural(lint['errors'], 'error')} {plural(lint['warnings'], 'warning')}"
        return f"v{self.version} lint {counts}"

    def publish(self, body: str, heading: str, problems: tuple[Problem, ...]) -> None:
        """Show *body* under the banner; the version moves only when the page changes."""
        if self.version and (body, heading, problems) == (self.body, self.heading, self.problems):
            return
        page = _page(body, heading, problems, self.version + 1)
        with self._lock:
            self.body, self.heading, self.problems = body, heading, problems
            self.page, self.version = page, self.version + 1

    def handle(self, path: str) -> Response:
        """``/`` (or ``/index.html``) is the page, ``/version`` its version; else 404."""
        route = path.split("?", 1)[0].split("#", 1)[0]
        with self._lock:
            version, page = self.version, self.page
        if route in ("/", "/index.html"):
            return 200, _HTML, page
        if route == "/version":
            return 200, _TEXT, f"{version}\n".encode()
        return 404, _TEXT, b"not found\n"


_SHELL = """\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>tundravm preview</title></head>
<body style="font: 14px/1.45 system-ui, sans-serif; margin: 1.5rem;">
<p>$note</p>
</body>
</html>
"""

_BANNER_STYLE = (
    "border: 1px solid #cf222e; background: #ffebe9; color: #1f2328; border-radius: 6px; "
    "padding: .6rem .9rem; margin: 0 0 1rem; font: 14px/1.45 system-ui, sans-serif;"
)

_RELOAD = """\
<script>
(function () {
  "use strict";
  var shown = "$version";
  setInterval(function () {
    fetch("/version", { cache: "no-store" })
      .then(function (r) { return r.text(); })
      .then(function (t) { if (t.trim() !== shown) { location.reload(); } })
      .catch(function () {});
  }, 1000);
})();
</script>
"""


def _page(body: str, heading: str, problems: tuple[Problem, ...], version: int) -> bytes:
    """*body* (else a placeholder) with the banner after ``<body>`` and the reload script."""
    if not body:
        note = "No rendering yet." if problems else "Rendering the recipe..."
        body = _SHELL.replace("$note", note)
    match = _BODY.search(body)
    at = match.end() if match else 0
    text = body[:at] + _banner(heading, problems) + body[at:]
    script = _RELOAD.replace("$version", str(version))
    end = text.lower().rfind("</body>")
    text = text[:end] + script + text[end:] if end >= 0 else text + script
    return text.encode("utf-8")


def _banner(heading: str, problems: tuple[Problem, ...]) -> str:
    if not heading:
        return ""
    items = "".join(f"<li>{_item(problem)}</li>" for problem in problems)
    return (
        f'\n<div id="preview-problems" role="alert" style="{_BANNER_STYLE}">'
        f"<strong>{html.escape(heading)}</strong><ul>{items}</ul></div>\n"
    )


def _item(problem: Problem) -> str:
    parts = [f"<code>{html.escape(problem.code)}</code> " if problem.code else ""]
    parts.append(html.escape(problem.message))
    if problem.location:
        parts.append(f"<br>at <code>{html.escape(problem.location)}</code>")
    if problem.hint:
        parts.append(f"<br>Hint: {html.escape(problem.hint)}")
    return "".join(parts)


def is_loopback(host: str) -> bool:
    """Whether *host* is ``localhost`` or a loopback address (``127.0.0.0/8``, ``::1``)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class _Server6(_Server):
    address_family = socket.AF_INET6


def serve(preview: Preview, host: str, port: int) -> ThreadingHTTPServer:
    """Bind *host*:*port* (0 picks a free port) and serve *preview* from a daemon thread.

    The caller stops it with ``shutdown()`` and ``server_close()``.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, kind, body = preview.handle(self.path)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Quiet: the page polls ``/version`` every second."""

    server = (_Server6 if ":" in host else _Server)((host, port), Handler)
    loop = threading.Thread(
        target=server.serve_forever, args=(0.1,), name="tundravm-preview", daemon=True
    )
    loop.start()
    return server


def url_of(server: ThreadingHTTPServer) -> str:
    """``http://HOST:PORT/`` for a bound *server* (IPv6 hosts bracketed)."""
    host, port = server.server_address[:2]
    name = host.decode() if isinstance(host, bytes) else str(host)
    return f"http://[{name}]:{port}/" if ":" in name else f"http://{name}:{port}/"


__all__ = ["Preview", "Problem", "Response", "is_loopback", "serve", "url_of"]
