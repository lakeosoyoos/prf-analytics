"""The data behind every view — the web app's PRF tab, minus Streamlit.

Each function mirrors a helper in aip_products/streamlit_app.py (_prf_page_html,
_interval_page_html, _vintage_compare, ...) and calls the same engine functions with the
same arguments. Nothing here computes a PRF number; it only picks inputs, caches, and hands
the engine's output to the window.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from collections import OrderedDict

from . import paths  # noqa: F401  (puts the engine on sys.path)
from src import prfallocpage, prfdeck, prfintervalpage, prfvintagepage
from src.prfherdpage import build_herd_size_payload, render_herd_size_page_html
from src.prfpage import build_prf_page_payload, render_prf_page_html

USES = ("Grazing", "Haying", "Haying-Irrigated")
# Label -> the string the engine is keyed on, as in streamlit_app._VT_COVERAGES.
COVERAGES = {"90%": "0.9", "85%": "0.85", "80%": "0.8", "75%": "0.75", "70%": "0.7"}
PF_MIN, PF_MAX, PF_DEFAULT = 60, 150, 100


# ------------------------------------------------------------------ plumbing
_local = threading.local()


def conn() -> sqlite3.Connection:
    """One read-only connection per server thread, opened the way the web app's
    data.open_ro opens it (Row factory — the engine reads columns by name). immutable=1: the
    DB ships inside the app in DELETE journal mode and never changes under a running
    process, so SQLite can skip locking entirely."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(paths.DB_PATH.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        c.row_factory = sqlite3.Row
        _local.conn = c
    return c


def memo(maxsize: int):
    """Cache by arguments, computing each key once even when two requests race for it —
    the county map takes seconds to build and the window asks for it at startup."""
    def wrap(fn):
        cache: OrderedDict = OrderedDict()
        locks: dict = {}
        guard = threading.Lock()

        def inner(*args):
            with guard:
                if args in cache:
                    cache.move_to_end(args)
                    return cache[args]
                lock = locks.setdefault(args, threading.Lock())
            with lock:
                with guard:
                    if args in cache:
                        return cache[args]
                value = fn(*args)
                with guard:
                    cache[args] = value
                    locks.pop(args, None)
                    while len(cache) > maxsize:
                        cache.popitem(last=False)
                return value

        inner.cache_clear = lambda: cache.clear()
        return inner
    return wrap


@memo(1)
def _assets() -> dict:
    return {
        "d3_js": (paths.ASSETS_DIR / "d3.v7.min.js").read_text(encoding="utf-8"),
        "topojson_js": (paths.ASSETS_DIR / "topojson-client.min.js").read_text(encoding="utf-8"),
        "atlas": json.loads((paths.ASSETS_DIR / "counties-10m.json").read_text(encoding="utf-8")),
    }


def _count(table: str) -> int:
    try:
        return conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0


def seed_mtimes() -> tuple:
    out = []
    for name in paths.SEEDS:
        p = paths.seed_path(name)
        try:
            out.append((str(p), p.stat().st_mtime))
        except OSError:
            out.append((str(p), 0.0))
    return tuple(out)


# ------------------------------------------------------------------ status
def meta() -> dict:
    try:
        sync = json.loads(paths.SYNC_JSON.read_text())
    except (OSError, ValueError):
        sync = {}
    return {
        "counts": {
            "cbv": _count("prf_county"), "opt": _count("prf_opt_best"),
            "herd": _count("fsa_grazing_county"), "intervals": _count("prf_interval_history"),
        },
        "vintage": vintage_meta(),
        "sync": {k: sync.get(k) for k in ("synced_at", "source_commit", "source_uncommitted",
                                          "source_db_modified")},
        "commission": {name: str(paths.seed_path(name)) for name in paths.SEEDS},
        "commission_is_user_copy": all((paths.SUPPORT_DIR / n).exists() for n in paths.SEEDS),
        "uses": list(USES), "coverages": list(COVERAGES),
        "pf": {"min": PF_MIN, "max": PF_MAX, "default": PF_DEFAULT},
    }


# ------------------------------------------------------------------ county map
def prf_page_html() -> str:
    return _prf_page_html(seed_mtimes())


@memo(2)
def _prf_page_html(seeds: tuple) -> str:
    """The five-metric county map. Keyed on the commission seeds' paths and mtimes, so an
    edit to either CSV shows up on the next load — the same cache-bust streamlit_app.py
    does with _prf_seed_mtime."""
    (comm, _), (tz, _) = seeds
    payload = build_prf_page_payload(conn(), commission_csv=comm, timezone_csv=tz)
    return render_prf_page_html(payload, **_assets())


# ------------------------------------------------------------------ herd size
@memo(1)
def herd_page_html() -> str:
    return render_herd_size_page_html(build_herd_size_payload(conn()), **_assets())


# ------------------------------------------------------------------ interval analyzer
@memo(1)
def interval_counties() -> list:
    return [list(t) for t in prfintervalpage.counties_with_grids(conn())]


@memo(64)
def interval_grids(county_fips: str) -> list:
    return prfintervalpage.grid_choices(conn(), county_fips)


@memo(24)
def interval_page_html(grid_id: int, county_fips: str, use: str) -> str:
    """One grid's analyzer. Keyed on grid AND county: a grid can straddle county lines and
    the base value that turns a per-$1 return into a per-acre one is per county."""
    payload = prfintervalpage.build_interval_payload(
        conn(), grid_id, county_fips=county_fips, use=use)
    return prfintervalpage.render_interval_page_html(payload) if payload else ""


# ------------------------------------------------------------------ all allocations
@memo(24)
def alloc_page_html(grid_id: int, county_fips: str, use: str) -> str:
    """Every allocation for one grid, sorted. Same payload (and so the same numbers) as the
    analyzer; see src/prfallocpage."""
    payload = prfallocpage.build_alloc_payload(conn(), grid_id, county_fips=county_fips, use=use)
    if not payload:
        return ""
    payload["deck_host"] = "mac"     # this app can build the deck: show the button beside Save as CSV
    return prfallocpage.render_alloc_page_html(payload)


def check_csv_name(v: str) -> str:
    """A plain file name ending in .csv -- the page proposes it, the Save dialog has the last word."""
    v = (v or "").strip().replace("/", "_").replace("\\", "_")
    if not v.lower().endswith(".csv") or len(v) > 160 or v.startswith("."):
        raise ValueError("expected a .csv file name")
    return v


# ------------------------------------------------------------------ client deck
@memo(8)
def deck(grid_id: int, county_fips: str, use: str, coverage: str, pf: int, end_year,
         years_back: int, fmt: str, day: str) -> tuple[str, bytes]:
    """(file name, bytes): the agency's three-slide deck for one grid as a PowerPoint ("pptx")
    or a PDF ("pdf"), from the analysis the analyzer shows. `pf` is the productivity factor in percent, as everywhere in this app;
    `day` is part of the key so a deck built yesterday is not served as today's."""
    import datetime as dt
    return prfdeck.build_deck(
        conn(), grid_id, county_fips, use, coverage=float(coverage), productivity=pf / 100,
        end_year=end_year, years_back=years_back, today=dt.date.fromisoformat(day),
        fmt=fmt)


# ------------------------------------------------------------------ old vs new
@memo(1)
def vintage_meta() -> dict:
    return prfvintagepage.prev_meta(conn()) or {}


@memo(6)
def vintage_compare(use: str, coverage: str) -> dict:
    return prfvintagepage.compare(conn(), use=use, coverage=float(coverage))


@memo(6)
def vintage_page_html(use: str, coverage: str, pf: int) -> str:
    result = vintage_compare(use, coverage)
    return prfvintagepage.render_vintage_page_html(
        prfvintagepage.build_vintage_payload(result, pf), **_assets())


@memo(6)
def vintage_table(use: str, coverage: str, pf: int) -> dict:
    """Stats tiles + every grid x county as columns/rows (half the bytes of a list of dicts)."""
    result = vintage_compare(use, coverage)
    records = prfvintagepage.table_records(result, pf)
    columns = list(records[0]) if records else []
    return {
        "stats": prfvintagepage.stats(result, pf),
        "columns": columns,
        "rows": [[r[c] for c in columns] for r in records],
        "states": sorted({r["state"] for r in result["rows"] if r["state"]}),
    }


def vintage_csv(use: str, coverage: str, pf: int, state: str) -> tuple[str, bytes]:
    """(file name, CSV bytes) — the same file name the web app's download button uses."""
    t = vintage_table(use, coverage, pf)
    si = t["columns"].index("State") if "State" in t["columns"] else None
    rows = [r for r in t["rows"] if state in ("", "All states") or r[si] == state]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(t["columns"])
    w.writerows(["" if v is None else v for v in r] for r in rows)
    label = next(k for k, v in COVERAGES.items() if v == coverage)
    name = (f"prf_old_vs_new_{use}_{label[:-1]}pct"
            f"{'' if state in ('', 'All states') else '_' + state}.csv")
    return name, buf.getvalue().encode("utf-8")


# ------------------------------------------------------------------ input checks
def check_use(v: str) -> str:
    if v not in USES:
        raise ValueError(f"unknown intended use {v!r}")
    return v


def check_coverage(v: str) -> str:
    if v in COVERAGES:
        v = COVERAGES[v]
    if v not in COVERAGES.values():
        raise ValueError(f"unknown coverage level {v!r}")
    return v


def check_pf(v) -> int:
    pf = int(float(v))
    if not PF_MIN <= pf <= PF_MAX:
        raise ValueError(f"productivity factor must be {PF_MIN}–{PF_MAX}%")
    return pf


def check_year(v):
    """A four-digit year, or None for "the latest" when the argument is empty."""
    v = (v or "").strip()
    if not v:
        return None
    if not (v.isdigit() and len(v) == 4):
        raise ValueError(f"bad year {v!r}")
    return int(v)


def check_lookback(v) -> int:
    n = int(float(v))
    if n not in prfdeck.LOOKBACKS:
        raise ValueError("lookback must be " + ", ".join(map(str, prfdeck.LOOKBACKS)) + " years")
    return n


def check_format(v: str) -> str:
    v = (v or "pptx").strip().lower()
    if v not in prfdeck.FORMATS:
        raise ValueError("format must be " + " or ".join(prfdeck.FORMATS))
    return v


def check_fips(v: str) -> str:
    v = (v or "").strip()
    if not (v.isdigit() and len(v) <= 5):
        raise ValueError(f"bad county FIPS {v!r}")
    return v.zfill(5)
