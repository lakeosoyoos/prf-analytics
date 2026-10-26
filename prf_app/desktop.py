"""Open PRF Analytics in its own Mac window.

    ~/.prf_analytics_venv/bin/python run_prf_analytics.py            # native window (WebKit)
    ~/.prf_analytics_venv/bin/python run_prf_analytics.py --browser  # in the default browser

The window is a WebKit view pointed at the loopback server in server.py. Saving a CSV and
editing the commission rates go through the small Api below, which JavaScript in the window
reaches as window.pywebview.api.*; in --browser mode the page falls back to a download.
"""
from __future__ import annotations

import argparse
import faulthandler
import logging
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths


def _setup_logging(verbose: bool) -> None:
    # A windowed .app may start with no stdout/stderr; anything that prints must not crash.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    paths.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(paths.LOG_PATH, maxBytes=1_000_000, backupCount=2)]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    # Anything that would otherwise vanish with the window lands in the log: uncaught Python
    # exceptions, and a native-level crash's Python stack (faulthandler).
    sys.excepthook = lambda t, v, tb: logging.critical("uncaught", exc_info=(t, v, tb))
    faulthandler.enable(open(paths.LOG_PATH, "a"))


class Api:
    """Methods callable from the window's JavaScript. Underscored attributes stay private."""

    def __init__(self) -> None:
        self._window = None

    def save_vintage_csv(self, use: str, cov: str, pf, state: str) -> str:
        """Native Save dialog for the old-vs-new table. Returns the saved path, or ''."""
        from . import views
        import webview

        name, body = views.vintage_csv(views.check_use(use), views.check_coverage(cov),
                                       views.check_pf(pf), state)
        picked = self._window.create_file_dialog(
            webview.FileDialog.SAVE, directory=str(Path.home() / "Downloads"),
            save_filename=name)
        if not picked:
            return ""
        target = Path(picked if isinstance(picked, str) else picked[0])
        target.write_bytes(body)
        logging.info("saved %s (%d bytes)", target, len(body))
        return str(target)

    def save_deck(self, grid, county: str, use: str, cov: str, pf, end, length,
                  fmt: str = "pptx") -> dict:
        """Native Save dialog for the client deck, as PowerPoint ("pptx") or PDF ("pdf"). Returns {"path": saved path or ""} or
        {"error": what to change} — a grid with no base value is a message, not a crash."""
        from . import server, views
        import webview

        q = {"grid": [str(grid)], "county": [county], "use": [use], "cov": [cov], "pf": [str(pf)],
             "end": ["" if end in (None, "") else str(end)], "len": [str(length)],
             "fmt": [fmt or "pptx"]}
        try:
            name, body = views.deck(*server.deck_args(q))
        except ValueError as exc:
            return {"error": str(exc)}
        picked = self._window.create_file_dialog(
            webview.FileDialog.SAVE, directory=str(Path.home() / "Downloads"),
            save_filename=name)
        if not picked:
            return {"path": ""}
        target = Path(picked if isinstance(picked, str) else picked[0])
        target.write_bytes(body)
        logging.info("saved %s (%d bytes)", target, len(body))
        return {"path": str(target)}

    def save_text_file(self, name: str, text: str) -> str:
        """Native Save dialog for a CSV the page built (the all-allocations table). Returns the
        saved path, or '' if cancelled. Only .csv names are accepted."""
        from . import views
        import webview

        name = views.check_csv_name(name)
        picked = self._window.create_file_dialog(
            webview.FileDialog.SAVE, directory=str(Path.home() / "Downloads"), save_filename=name)
        if not picked:
            return ""
        target = Path(picked if isinstance(picked, str) else picked[0])
        target.write_text(text, encoding="utf-8")
        logging.info("saved %s (%d bytes)", target, len(text))
        return str(target)

    def edit_commission(self) -> str:
        """Copy the bundled commission seeds out of the app (once) and show them in Finder.
        The map reads the copies from then on; editing them and pressing Reload reprices it."""
        paths.SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        for name in paths.SEEDS:
            user = paths.SUPPORT_DIR / name
            if not user.exists():
                shutil.copy2(paths.SEED_DIR / name, user)
        target = paths.SUPPORT_DIR / paths.SEEDS[0]
        subprocess.run(["open", "-R", str(target)], check=False)
        return str(target)


def _wait_ready(port: int, timeout: float = 10.0) -> None:
    import http.client
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("HEAD", "/api/meta", headers={"Host": f"127.0.0.1:{port}"})
            if c.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("the local server did not come up")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="PRF Analytics")
    ap.add_argument("--browser", action="store_true", help="open in the default browser")
    ap.add_argument("--verbose", action="store_true", help="log to the terminal too")
    ap.add_argument("--port", type=int, default=0, help="fixed loopback port (default: any free one)")
    ap.add_argument("--no-open", action="store_true", help="with --browser: serve without opening a tab")
    ap.add_argument("--self-test", action="store_true",
                    help="start, render every view once, exit 0/1 (used by the build)")
    ap.add_argument("--check-window", action="store_true",
                    help="open the real window, click through every tab, close, exit 0/1")
    # macOS may pass -psn_0_… to an app launched from Finder; ignore anything unknown.
    args, _ = ap.parse_known_args(argv)
    _setup_logging(args.verbose)

    from . import server
    if not paths.DB_PATH.exists():
        logging.error("database missing: %s", paths.DB_PATH)
    srv = server.start(args.port)
    port = srv.server_port
    url = f"http://127.0.0.1:{port}/"
    _wait_ready(port)
    logging.info("serving %s (db %s)", url, paths.DB_PATH)

    if args.self_test:
        from .selftest import run
        return run(port)

    server.warm()

    if not args.browser:
        try:
            import webview
        except ImportError:
            logging.warning("pywebview not installed — falling back to the browser")
        else:
            api = Api()
            api._window = webview.create_window(
                paths.APP_NAME, url, js_api=api, width=1440, height=960,
                min_size=(1024, 700), text_select=True, background_color="#f6f8fa")
            if args.check_window:
                from .selftest import WINDOW_RESULT, check_window
                webview.start(check_window, api._window, private_mode=True)
                return WINDOW_RESULT["code"]
            webview.start(private_mode=True)
            logging.info("window closed — exiting")
            return 0

    if not args.no_open:
        webbrowser.open(url)
    print(f"{paths.APP_NAME} is running at {url} — press Ctrl-C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
