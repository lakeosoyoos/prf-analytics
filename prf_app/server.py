"""A loopback-only HTTP server that feeds the window.

Standard library only (no Flask): a handful of GET routes, each a thin call into views.py.
Bound to 127.0.0.1 on a port the OS picks, and it answers only requests whose Host header
names this port on 127.0.0.1 or localhost, so a web page in a browser cannot reach it by DNS rebinding.
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import paths, views
from src import prfdeck  # noqa: E402  (paths puts the engine on sys.path first)

log = logging.getLogger("prf_analytics")


def _q(query: dict, key: str, default: str = "") -> str:
    return (query.get(key) or [default])[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "PRFAnalytics/1"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- responses
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, separators=(",", ":")).encode("utf-8"),
                   "application/json")

    def _note(self, status: int, message: str) -> None:
        """An error as a small page, since most routes are loaded into an iframe."""
        body = (f"<!doctype html><meta charset=utf-8><body style=\"font:14px -apple-system,"
                f"sans-serif;color:#57606a;padding:24px\">{message}</body>")
        self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

    # ---------------------------------------------------------------- routing
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        port = self.server.server_port
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._note(403, "Forbidden")
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        route = ROUTES.get(url.path)
        if route is None:
            self._note(404, "Not found")
            return
        try:
            route(self, q)
        except ValueError as exc:
            self._note(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass  # the window navigated away mid-response
        except Exception:
            log.error("error serving %s\n%s", self.path, traceback.format_exc())
            self._note(500, "Something went wrong building this view. Details are in "
                            f"{paths.LOG_PATH}.")

    def log_message(self, fmt: str, *args) -> None:  # stderr may not exist in the .app
        log.debug("%s " + fmt, self.address_string(), *args)


# -------------------------------------------------------------------- routes
def r_shell(h: Handler, q) -> None:
    h._html(paths.SHELL_HTML.read_text(encoding="utf-8"))


def r_meta(h: Handler, q) -> None:
    h._json(views.meta())


def r_prf_page(h: Handler, q) -> None:
    h._html(views.prf_page_html())


def r_herd_page(h: Handler, q) -> None:
    h._html(views.herd_page_html())


def r_counties(h: Handler, q) -> None:
    h._json(views.interval_counties())


def r_grids(h: Handler, q) -> None:
    h._json(views.interval_grids(views.check_fips(_q(q, "county"))))


def r_interval_page(h: Handler, q) -> None:
    grid = int(_q(q, "grid") or 0)
    county = views.check_fips(_q(q, "county"))
    use = views.check_use(_q(q, "use", "Grazing"))
    html = views.interval_page_html(grid, county, use)
    if not html:
        h._note(200, f"Grid {grid} has no packed interval history in this database. Run "
                     "<code>scripts/build_prf_interval_history.py</code> in aip_products, "
                     "rebuild the app DB, then run <code>sync_engine.py</code> here.")
        return
    h._html(html)


def r_alloc_page(h: Handler, q) -> None:
    grid = int(_q(q, "grid") or 0)
    county = views.check_fips(_q(q, "county"))
    use = views.check_use(_q(q, "use", "Grazing"))
    html = views.alloc_page_html(grid, county, use)
    if not html:
        h._note(200, f"Grid {grid} has no packed interval history in this database.")
        return
    h._html(html)


def _vintage_args(q) -> tuple[str, str, int]:
    return (views.check_use(_q(q, "use", "Grazing")),
            views.check_coverage(_q(q, "cov", "0.9")),
            views.check_pf(_q(q, "pf", str(views.PF_DEFAULT))))


def r_vintage_page(h: Handler, q) -> None:
    h._html(views.vintage_page_html(*_vintage_args(q)))


def r_vintage_table(h: Handler, q) -> None:
    h._json(views.vintage_table(*_vintage_args(q)))


def r_vintage_csv(h: Handler, q) -> None:
    """Browser fallback only — in the window, Save goes through the native dialog."""
    name, body = views.vintage_csv(*_vintage_args(q), _q(q, "state", "All states"))
    h._send(200, body, "text/csv; charset=utf-8",
            {"Content-Disposition": f'attachment; filename="{name}"'})


def deck_args(q) -> tuple:
    """Query -> views.deck arguments, every one checked. A bad value is a 400, not a 500."""
    import datetime as dt
    return (int(_q(q, "grid") or 0), views.check_fips(_q(q, "county")),
            views.check_use(_q(q, "use", "Grazing")), views.check_coverage(_q(q, "cov", "0.9")),
            views.check_pf(_q(q, "pf", str(views.PF_DEFAULT))), views.check_year(_q(q, "end")),
            views.check_lookback(_q(q, "len", "20")),
            views.check_format(_q(q, "fmt")), dt.date.today().isoformat())


def r_deck(h: Handler, q) -> None:
    """Browser fallback only — in the window, Save goes through the native dialog."""
    args = deck_args(q)
    name, body = views.deck(*args)
    h._send(200, body, prfdeck.FORMATS[args[-2]],
            {"Content-Disposition": f'attachment; filename="{name}"'})


ROUTES = {
    "/": r_shell,
    "/api/meta": r_meta,
    "/api/counties": r_counties,
    "/api/grids": r_grids,
    "/api/vintage": r_vintage_table,
    "/api/vintage.csv": r_vintage_csv,
    "/api/deck.pptx": r_deck,
    "/api/deck": r_deck,
    "/page/prf": r_prf_page,
    "/page/herd": r_herd_page,
    "/page/interval": r_interval_page,
    "/page/allocations": r_alloc_page,
    "/page/vintage": r_vintage_page,
}


class Server(ThreadingHTTPServer):
    daemon_threads = True


def start(port: int = 0) -> Server:
    """Start serving on a loopback port (0 = any free one) in a background thread."""
    srv = Server(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, name="prf-http", daemon=True).start()
    return srv


def warm() -> None:
    """Build the slow views ahead of the first click, most-likely-first."""
    def _run():
        for fn in (views.prf_page_html, views.interval_counties,
                   lambda: views.vintage_table("Grazing", "0.9", views.PF_DEFAULT),
                   lambda: views.vintage_page_html("Grazing", "0.9", views.PF_DEFAULT),
                   views.herd_page_html):
            try:
                fn()
            except Exception:
                log.error("warm-up failed\n%s", traceback.format_exc())
    threading.Thread(target=_run, name="prf-warm", daemon=True).start()
