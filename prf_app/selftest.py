"""`--self-test`: fetch every route once from the running server and check the answers.

The build runs this against the FROZEN app, so a module PyInstaller failed to bundle, a data
file left out, or a path that only resolves in development fails the build rather than
reaching someone's Dock.
"""
from __future__ import annotations

import http.client
import json
import logging
import time


def _get(port: int, path: str) -> tuple[int, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    return r.status, r.read()


def run(port: int) -> int:
    failures = []

    def check(path: str, min_bytes: int, must: bytes = b"") -> bytes:
        t = time.time()
        status, body = _get(port, path)
        ok = status == 200 and len(body) >= min_bytes and must in body
        line = f"{'ok ' if ok else 'FAIL'} {path}  {status}  {len(body):,} B  {time.time() - t:.2f}s"
        print(line)
        logging.info("self-test %s", line)
        if not ok:
            failures.append(path)
        return body

    meta = json.loads(check("/api/meta", 100) or b"{}")
    counts = meta.get("counts", {})
    for k in ("cbv", "opt", "herd", "intervals"):
        if not counts.get(k):
            print(f"FAIL table behind '{k}' is empty")
            failures.append(k)
    check("/", 1000, b"PRF Analytics")
    check("/page/prf", 1_000_000, b"<svg")
    check("/page/herd", 500_000)
    counties = json.loads(check("/api/counties", 1000) or b"[]")
    fips = next((f for s, f, n in counties if s == "WA"), counties[0][1] if counties else "")
    grids = json.loads(check(f"/api/grids?county={fips}", 2) or b"[]")
    if grids:
        check(f"/page/interval?grid={grids[0]}&county={fips}&use=Grazing", 5000, b"<script")
        check(f"/page/allocations?grid={grids[0]}&county={fips}&use=Grazing", 5000, b"saveCsv")
    # A deck for the first grid that can have one (some counties publish no base value).
    built = False
    for _, f, _ in counties[:60]:
        gs = json.loads(_get(port, f"/api/grids?county={f}")[1] or b"[]")
        if not gs:
            continue
        base = (f"/api/deck?grid={gs[0]}&county={f}&use=Grazing&cov=0.9"
                "&pf=100&end=2025&len=20")
        status, body = _get(port, base + "&fmt=pptx")
        if status == 200:
            pstatus, pdf = _get(port, base + "&fmt=pdf")
            built = (body[:2] == b"PK" and len(body) > 400_000 and b"ppt/slides/slide2.xml" in body
                     and pstatus == 200 and pdf[:5] == b"%PDF-" and len(pdf) > 300_000)
            line = (f"ok  /api/deck (grid {gs[0]}, county {f})  pptx {len(body):,} B, pdf {len(pdf):,} B"
                    if built else f"FAIL /api/deck pptx {status} {len(body):,} B, pdf {pstatus} {len(pdf):,} B")
            print(line)
            break
    if not built:
        print("FAIL /api/deck: no grid in the first 60 counties produced a deck")
        failures.append("deck")
    table = json.loads(check("/api/vintage?use=Grazing&cov=0.9&pf=100", 100_000) or b"{}")
    if not table.get("rows"):
        failures.append("vintage rows")
    check("/page/vintage?use=Grazing&cov=0.9&pf=100", 100_000)
    check("/api/vintage.csv?use=Grazing&cov=0.9&pf=100&state=WA", 1000, b"State,County")
    print("SELF-TEST " + ("PASSED" if not failures else f"FAILED: {failures}"))
    return 1 if failures else 0


# ---------------------------------------------------------------------------- the window
WINDOW_RESULT = {"code": 1}


def check_window(window) -> None:
    """`--check-window`: drive the REAL window — WebKit, the JS bridge, every tab — then close it.

    Runs on pywebview's worker thread once the window exists. Every check reads the live page
    through evaluate_js; the outcome goes to stdout and the log, and main() exits with it.
    """
    js = window.evaluate_js
    failures = []

    def wait(expr: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if js(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def check(name: str, ok: bool, detail="") -> None:
        line = f"{'ok ' if ok else 'FAIL'} window: {name} {detail}".rstrip()
        print(line)
        logging.info("check-window %s", line)
        if not ok:
            failures.append(name)

    try:
        window.events.loaded.wait(30)
        check("shell loaded", wait("!!document.getElementById('t-map')", 20))
        check("native bridge (Save CSV, Save deck, commission)", wait(
            "!!(window.pywebview && window.pywebview.api && window.pywebview.api.save_vintage_csv"
            " && window.pywebview.api.save_deck && window.pywebview.api.save_text_file"
            " && window.pywebview.api.edit_commission)", 20))
        loaded = ("(function(p){var f=document.querySelector('#'+p+' iframe');"
                  "var v=f&&f.parentNode.querySelector('.loading');"
                  "return !!(f && f.dataset.src && v && v.hidden);})")
        for tab, panel, secs in (("t-map", "p-map", 60), ("t-iv", "p-iv", 30), ("t-al", "p-al", 60),
                                 ("t-vt", "p-vt", 60), ("t-herd", "p-herd", 30)):
            js(f"document.getElementById('{tab}').click()")
            check(f"tab {tab[2:]} rendered", wait(f"{loaded}('{panel}')", secs))
        check("county map drew counties", wait(
            "document.getElementById('f-map').contentDocument.querySelectorAll('path').length > 3000", 20))
        check("interval recommendation shown", wait(
            "/Recommended allocation/.test(document.getElementById('f-iv').contentDocument.body.innerText)", 20))
        state = js("(function(){var w=document.getElementById('f-iv').contentWindow;"
                   "return w.PRF_STATE?JSON.stringify(w.PRF_STATE()):null})()")
        check("analyzer state reaches the Save deck button", bool(state) and
              '"coverage":"0.9"' in state and '"lookback":20' in state, f"({state})")
        check("Save deck button present", bool(js(
            "!!document.getElementById('iv-deck-save') && !document.getElementById('iv-deck-save').disabled")))
        al_rows = js("document.getElementById('f-al').contentDocument.querySelectorAll('#all tbody tr').length")
        al_count = js("document.getElementById('f-al').contentDocument.getElementById('count').textContent")
        check("all-allocations table filled", (al_rows or 0) >= 100, f"({al_rows} rows; {al_count})")
        rows = js("document.querySelectorAll('#vt-table tbody tr').length")
        check("old-vs-new table rows", bool(rows), f"({rows} on page 1)")
        tiles = js("document.querySelectorAll('#vt-tiles .tile').length")
        check("old-vs-new stat tiles", tiles == 5, f"({tiles})")
    except Exception as exc:  # the check itself broke — that is a failure too
        check("window check ran", False, repr(exc))
    print("WINDOW CHECK " + ("PASSED" if not failures else f"FAILED: {failures}"))
    WINDOW_RESULT["code"] = 1 if failures else 0
    window.destroy()
