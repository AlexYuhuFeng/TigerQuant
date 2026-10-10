"""A read-only, loopback-only operator dashboard.

CR-08 asks for a dashboard and for isolation to be "shown distinctly". Both constraints point
the same way: the safest dashboard is one that **cannot** change anything and **cannot** be
reached from off the machine.

Four properties, each enforced rather than documented:

**Read-only.** Only ``GET`` and ``HEAD`` are answered; anything else is ``405``. There is no
route that writes, projects, repairs, halts or submits. The handler's only capability is to
render a journal it was handed at construction.

**Loopback-only.** The server refuses to bind any address other than ``127.0.0.1``, ``::1`` or
``localhost``. AGENTS.md restricts this project to the development workspace and paper services;
a dashboard one flag away from being exposed on a conference-network interface is a dashboard
that will eventually be exposed on one.

**No path traversal and no dynamic routing.** A fixed set of routes, each served from data
computed once at startup. A request path is never used to build a filesystem path, so there is
nothing to traverse.

**Isolation is stated, not implied.** Every response carries the mode in the body *and* in a
header, and the index lists exactly which journal was loaded. Two dashboards on two modes look
unmistakably different, because the failure this guards against is an operator reading paper
numbers while believing they are live ones.

The server is a development and inspection tool. It is not a deployment artifact: there is no
authentication, no TLS and no multi-user story, and it says so on its own index page.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from tigerquant.core.errors import ConfigurationError
from tigerquant.ops.lineage_report import LineageReport, render_csv, render_json, render_text

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ALLOWED_METHODS",
    "LOOPBACK_HOSTS",
    "DashboardApp",
    "DashboardServer",
    "build_app",
]

#: The only methods answered. Everything else is refused before any handler runs.
ALLOWED_METHODS = frozenset({"GET", "HEAD"})

#: The only addresses the server will bind.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: The complete route table. Deliberately a tuple: there is no dynamic routing, so a request
#: path can never influence anything but a dictionary lookup.
ROUTES: tuple[str, ...] = ("/", "/lineage.json", "/lineage.txt", "/lineage.csv", "/health")


@dataclass(frozen=True, slots=True)
class DashboardApp:
    """The rendered content, computed once.

    Content is built at construction rather than per request. That makes the dashboard
    genuinely read-only — there is no code path from an HTTP request to a journal read — and
    it makes the served bytes deterministic and testable.
    """

    mode: str
    journal: str
    report: LineageReport
    _bodies: dict[str, bytes] = field(default_factory=dict, repr=False)

    def body(self, path: str) -> bytes | None:
        return self._bodies.get(path)


def build_app(report: LineageReport, *, journal: str) -> DashboardApp:
    """Render every route once, at startup."""
    bodies = {
        "/": _index_html(report, journal).encode("utf-8"),
        "/lineage.json": render_json(report).encode("utf-8"),
        "/lineage.txt": render_text(report).encode("utf-8"),
        "/lineage.csv": render_csv(report).encode("utf-8"),
        "/health": json.dumps(
            {"ok": True, "mode": report.mode, "records": report.decisions, "clean": report.is_clean}
        ).encode("utf-8"),
    }
    return DashboardApp(mode=report.mode, journal=journal, report=report, _bodies=bodies)


def _index_html(report: LineageReport, journal: str) -> str:
    """A single page, no external assets, no JavaScript.

    No JavaScript is a deliberate choice: it removes a whole class of injection surface and
    means the page works from a file:// copy of the output. Every value below is escaped.
    """
    rows = "\n".join(
        "<tr>"
        f"<td>{_escape(record.decision_id)}</td>"
        f"<td>{_escape(record.action)}</td>"
        f"<td>{_escape(record.instrument or '-')}</td>"
        f"<td>{len(record.evidence_cited)}/{len(record.evidence_available)}</td>"
        f"<td>{_escape(record.risk_outcome or '-')}</td>"
        f"<td>{_escape(' -> '.join(record.order_states) or '-')}</td>"
        f"<td>{record.filled_quantity}</td>"
        f"<td>{record.traded_notional}</td>"
        f"<td>{'<b>YES</b>' if record.evidence_not_shown else 'no'}</td>"
        "</tr>"
        for record in report.records
    )
    facts = "\n".join(
        f"<li><code>{_escape(key)}</code>: {_escape(value)}</li>"
        for key, value in sorted(report.facts.items())
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>TigerQuant lineage - {_escape(report.mode)}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; }}
 .banner {{ background: #fff4d6; border-left: 6px solid #b8860b; padding: 0.75rem 1rem; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
 th, td {{ border: 1px solid #ddd; padding: 0.35rem 0.5rem; text-align: left; font-size: 0.9rem; }}
 th {{ background: #f2f2f2; }}
 code {{ background: #f6f6f6; padding: 0 0.2rem; }}
 .warn {{ color: #a00; font-weight: 600; }}
</style></head><body>
<div class="banner">
  <b>MODE: {_escape(report.mode)}</b> &mdash; every number on this page comes from the
  <code>{_escape(journal)}</code> journal and <b>nothing from any other mode is merged in</b>.
  This is a read-only development view: no authentication, no TLS, loopback only.
</div>
<h1>Decision lineage</h1>
<p>{_escape(report.summary())}</p>
<h2>Isolation and run facts</h2>
<ul>{facts}</ul>
<h2>Decisions</h2>
<table>
<tr><th>decision</th><th>action</th><th>instrument</th><th>evidence cited/shown</th>
<th>risk</th><th>order states</th><th>filled</th><th>notional</th><th>fabrication</th></tr>
{rows}
</table>
<h2>Exports</h2>
<ul>
<li><a href="/lineage.json">lineage.json</a> - the stable machine-readable form</li>
<li><a href="/lineage.txt">lineage.txt</a> - the human-readable form</li>
<li><a href="/lineage.csv">lineage.csv</a> - one row per decision, for a spreadsheet</li>
</ul>
</body></html>
"""


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def _escape(value: object) -> str:
    """HTML-escape every interpolated value, including ones we generated ourselves."""
    return "".join(_ESCAPES.get(character, character) for character in str(value))


class DashboardServer:
    """A loopback-only HTTP server over a prebuilt :class:`DashboardApp`."""

    __slots__ = ("_app", "_httpd", "_thread")

    def __init__(self, app: DashboardApp, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in LOOPBACK_HOSTS:
            msg = (
                f"refusing to bind {host!r}. This dashboard has no authentication and no TLS, "
                "so it may only listen on loopback. AGENTS.md restricts this project to the "
                "development workspace and paper services; a flag that could expose it on a "
                "real interface is a flag that will eventually be used."
            )
            raise ConfigurationError(msg)
        self._app = app
        handler = _handler_for(app)
        # Port 0 asks the OS for a free port, which is what a test wants. A caller who pins a
        # port gets that port or a clear failure.
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> None:
        """Serve in a background thread, so a caller can drive it and then stop it."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> DashboardServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _handler_for(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    """Build the request handler for one app.

    A closure rather than a class attribute, so the served content is fixed at construction
    and the handler has no way to reach anything else.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "TigerQuantDashboard/1.0"
        sys_version = ""

        def do_GET(self) -> None:
            self._respond(include_body=True)

        def do_HEAD(self) -> None:
            self._respond(include_body=False)

        def _respond(self, *, include_body: bool) -> None:
            path = urlparse(self.path).path
            body = app.body(path)
            if body is None:
                # Nothing here reflects the request path, so a traversal attempt simply misses.
                payload = json.dumps({"error": "not found", "routes": list(ROUTES)}).encode("utf-8")
                self._send(404, "application/json", payload, include_body=include_body)
                return
            content_type = _content_type(path)
            self._send(200, content_type, body, include_body=include_body)

        def _send(self, status: int, content_type: str, body: bytes, *, include_body: bool) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Isolation is stated in a header as well as in the body, so a scripted consumer
            # cannot collect two modes' numbers without noticing which is which.
            self.send_header("X-TigerQuant-Mode", app.mode)
            self.send_header("X-TigerQuant-Journal", app.journal)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _drain_body(self) -> None:
            """Consume any request body before answering.

            Responding while the client is still sending leaves unread bytes in the socket and
            the connection is then reset, so the client sees ConnectionAbortedError instead of
            the status we sent. Found by the test that posts a body — which is why that test
            posts one rather than sending a bare POST.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 0:
                self.rfile.read(length)

        def _refuse(self, method: str) -> None:
            self._drain_body()
            payload = json.dumps(
                {
                    "error": "method not allowed",
                    "method": method,
                    "allowed": sorted(ALLOWED_METHODS),
                    "note": (
                        "this dashboard is read-only by construction: there is no route that "
                        "writes, projects, repairs, halts or submits."
                    ),
                }
            ).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", ", ".join(sorted(ALLOWED_METHODS)))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-TigerQuant-Mode", app.mode)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self._refuse("POST")

        def do_PUT(self) -> None:
            self._refuse("PUT")

        def do_DELETE(self) -> None:
            self._refuse("DELETE")

        def do_PATCH(self) -> None:
            self._refuse("PATCH")

        def log_message(self, fmt: str, *args: Any) -> None:
            # Silenced: the default writes to stderr and a lineage page contains no secrets,
            # but an operator watching a terminal does not need one line per asset request.
            return

    return Handler


def _content_type(path: str) -> str:
    if path.endswith(".json") or path == "/health":
        return "application/json; charset=utf-8"
    if path.endswith(".csv"):
        return "text/csv; charset=utf-8"
    if path.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "text/html; charset=utf-8"


def route_table() -> Sequence[str]:
    """The routes this dashboard serves, for a test or an operator."""
    return ROUTES
