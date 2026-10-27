"""PRF section: the interval analyzer — per-interval heat map + allocation recommendation.

The layout follows the Rain & Hail "Coverage Analyzer" a PRF producer already gets with a
quote (per-interval summary rows above a year x interval index matrix), but every number in
it is OUR analysis rather than a fixed illustration:

  * the three metrics are the same three the PRF map ranks counties by — win rate, return
    per $1 of protection, return per acre — computed per interval instead of per county;
  * the recommendation underneath re-runs the SAME optimizer src/prfsweep.py runs, over the
    same admissible policies (2-5 non-adjacent intervals, 5% steps, 10%..cap each, summing
    to 100), so over the sweep's own window (2006-2025) it reproduces what prf_opt_best
    already stored for that grid. The payload carries that stored answer so the page can show
    the two side by side — a visible self-check, not a claim;
  * two dropdowns choose the window: where it ENDS (any year that leaves room for 5) and how
    long it is (5, 10, 15 or 20 years back from there), and both the metrics and the
    recommendation are re-run over exactly those years — the thing a fixed illustration
    cannot do.

WHY THE WINDOW CAN MOVE. The packed history is 2000-2025 (src/prfintervals), deeper than the
sweep's 2006-2025 so a 20-year lookback can end as early as 2019. Only the window that equals
the sweep's own is compared against prf_opt_best; any other window gets a recommendation the
map beside it never computed, and the page says so instead of implying they should agree.
Premium rates are the current rate year's for every year in the window — the question is
"what would today's price have paid then", not what it cost then.

ONE GRID AT A TIME, on purpose: PRF is priced per grid, and the whole point of this section
is the detail the county-level map averages away. Streamlit picks the grid (state -> county
-> grid) and hands this module that one grid's history, so the page payload is a few hundred
numbers and the dropdowns redraw entirely in the browser with no server round-trip.
"""
from __future__ import annotations

import json
import sqlite3

from . import prfbulk
from .prfintervals import (
    COVERAGES, INDEX_CAP, USES, unpack_index, unpack_rates, unpack_use_mask,
)
from .prfopt import INTERVALS, MAX_PCT, MIN_PCT, STEP_PCT
from .prfpage import _cov_key, _f, _fips5, _parse_list

DEFAULT_COVERAGE = 0.9


def _interval_label(code: str) -> str:
    """'JAN-FEB' -> 'Jan-Feb', matching how RMA and the Rain & Hail report print it."""
    a, _, b = code.partition("-")
    return f"{a.capitalize()}-{b.capitalize()}"


def grid_choices(conn: sqlite3.Connection, county_fips: str) -> list[int]:
    """Grid ids touching one county, from the shipped prf_grid_county."""
    try:
        return [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT grid_id FROM prf_grid_county WHERE county_fips = ? "
            "ORDER BY grid_id", (_fips5(county_fips),))]
    except sqlite3.OperationalError:
        return []


def counties_with_grids(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(state, county_fips, county_name) for every county with grid coverage, sorted."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT state, county_fips, county_name FROM prf_grid_county "
            "WHERE state IS NOT NULL ORDER BY state, county_name").fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r[0], _fips5(r[1]), r[2] or r[1]) for r in rows]


def _cap_for(conn: sqlite3.Connection, county_fips: str) -> int:
    """The county's actuarial maximum percent per interval; MAX_PCT when none is published.

    MIN, matching src/prfsweep.grid_caps and scripts/build_app_db.shrink_prf_max_pct: where a
    county's cap varies, the conservative value is the one that keeps a recommendation
    buyable.
    """
    fips = _fips5(county_fips)
    try:
        row = conn.execute(
            "SELECT MIN(max_pct) FROM prf_max_pct WHERE state_code = ? AND county_code = ?",
            (fips[:2], fips[2:])).fetchone()
    except sqlite3.OperationalError:
        return MAX_PCT
    return int(row[0]) if row and row[0] is not None else MAX_PCT


def _janfeb_cap_for(conn: sqlite3.Connection, county_fips: str) -> int | None:
    """The county's separate JAN-FEB limit (RMA PM-26-053: 25 from RY2027), None if none applies."""
    fips = _fips5(county_fips)
    try:
        row = conn.execute(
            "SELECT MIN(janfeb_max_pct) FROM prf_max_pct WHERE state_code = ? AND county_code = ?",
            (fips[:2], fips[2:])).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row and row[0] is not None else None


def _cbv_for(conn: sqlite3.Connection, county_fips: str, use: str) -> float | None:
    """County Base Value ($/acre) for the newest year, conventional practice."""
    intended = "Haying" if use.startswith("Haying") else "Grazing"
    irrigation = "Irrigated" if use.endswith("-Irrigated") else "Non-Irrigated"
    try:
        row = conn.execute(
            "SELECT county_base_value FROM prf_county WHERE county_fips = ? "
            "AND intended_use = ? AND irrigation_practice = ? "
            "AND organic_practice = 'Conventional' "
            "ORDER BY year DESC LIMIT 1",
            (_fips5(county_fips), intended, irrigation)).fetchone()
    except sqlite3.OperationalError:
        return None
    return float(row[0]) if row and row[0] is not None else None


def _subsidy_schedule(conn: sqlite3.Connection) -> dict[str, float]:
    try:
        rows = conn.execute("SELECT coverage_level, subsidy_pct FROM prf_subsidy").fetchall()
    except sqlite3.OperationalError:
        rows = []
    return {_cov_key(r[0]): float(r[1]) for r in rows if r[1] is not None}


def _sweep_answer(conn: sqlite3.Connection, grid_id: int, use: str) -> dict:
    """prf_opt_best's stored recommendation per coverage, for the side-by-side check."""
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT coverage_level, year_min, year_max, max_pct, best_win_rate, "
            "best_win_combo, best_win_props, best_net, best_net_combo, best_net_props "
            "FROM prf_opt_best WHERE grid_id = ? AND intended_use = ?",
            (grid_id, use)).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        out[_cov_key(r[0])] = {
            "y0": r[1], "y1": r[2], "cap": r[3],
            "win": _f(r[4], 4),
            "win_combo": _parse_list(r[5]), "win_props": _parse_list(r[6]),
            "net": _f(r[7], 6),
            "net_combo": _parse_list(r[8]), "net_props": _parse_list(r[9]),
        }
    return out


def build_interval_payload(conn: sqlite3.Connection, grid_id: int, *,
                           county_fips: str | None = None,
                           use: str = "Grazing") -> dict:
    """Everything the analyzer page needs for ONE grid. Pure read; testable.

    Returns {} when the grid has no packed history — the section then renders an honest
    note instead of an empty chart.
    """
    try:
        row = conn.execute(
            "SELECT year_min, year_max, rate_year, index_blob, rate_blob, "
            "use_mask, index_source FROM prf_interval_history WHERE grid_id = ?",
            (grid_id,)).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}

    (year_min, year_max, rate_year, index_blob, rate_blob, use_mask, index_source) = row
    years = list(range(int(year_min), int(year_max) + 1))
    index = unpack_index(index_blob)
    if any(len(v) != len(years) for v in index.values()):
        raise ValueError(
            f"grid {grid_id}: index_blob holds {len(next(iter(index.values())))} years but the "
            f"row says {year_min}-{year_max} -- rebuild with scripts/build_prf_interval_history.py")
    offered = unpack_use_mask(int(use_mask))

    # WHICH COUNTY, EXPLICITLY. A 0.25-degree grid routinely straddles county lines — grid
    # 33345 touches five Washington counties — and the County Base Value is published per
    # COUNTY, not per grid: Douglas is $21.10/acre where Ferry is $30.30, a 44% spread on the
    # same rainfall. So the county is an input, not something to guess: every per-acre number
    # and the actuarial cap follow from it. The caller passes the county the acres are in; the
    # fallback is the lowest FIPS purely so the page is deterministic, and the payload carries
    # the grid's other counties so the page can say the figure is county-specific.
    siblings = [(_fips5(r[0]), r[1], r[2]) for r in conn.execute(
        "SELECT county_fips, county_name, state FROM prf_grid_county WHERE grid_id = ? "
        "ORDER BY county_fips", (grid_id,))]
    if county_fips is None and siblings:
        county_fips = siblings[0][0]
    else:
        county_fips = _fips5(county_fips) if county_fips else None

    county_name = state = None
    for fips, name, st in siblings:
        if fips == county_fips:
            county_name, state = name, st
            break

    return {
        "grid_id": int(grid_id),
        "years": years,
        "intervals": list(INTERVALS),
        "interval_labels": {iv: _interval_label(iv) for iv in INTERVALS},
        "index": index,
        "rates": unpack_rates(rate_blob),
        "offered": offered,
        "use": use,
        "uses": list(USES),
        "rate_year": int(rate_year),
        "subsidy": _subsidy_schedule(conn),
        "coverages": [f"{c:g}" for c in COVERAGES],
        "default_coverage": f"{DEFAULT_COVERAGE:g}",
        "cap": _cap_for(conn, county_fips) if county_fips else MAX_PCT,
        "janfeb_cap": _janfeb_cap_for(conn, county_fips) if county_fips else None,
        "cbv": _cbv_for(conn, county_fips, use) if county_fips else None,
        "county_fips": county_fips,
        "county_name": county_name,
        "state": state,
        "other_counties": [{"fips": f, "name": n, "state": s}
                           for f, n, s in siblings if f != county_fips],
        "sweep": _sweep_answer(conn, grid_id, use),
        "index_cap": INDEX_CAP,
        # Which Rainfall Index series these numbers are, read from the packed row itself so
        # it survives into the shipped DB (prf_grid_index does not). RMA moved from NOAA CPC
        # to NOAA NCEI for the 2027 crop year and re-based the whole history with it, so
        # "the index" is not one thing any more and a page that does not say which one it is
        # showing is not saying enough. See src/prfbulk.
        "index_source": index_source,
        # Derived, never string-matched in the page: a stamp can read
        # "PrfWebApi 2025 top-up+vi_ri_2026cy" and be CPC without the word appearing in it.
        "index_provider": prfbulk.provider_of_source(index_source or ""),
        "current_provider": prfbulk.provider(),
        "rules": {"min_pct": MIN_PCT, "step_pct": STEP_PCT, "max_intervals": 5,
                  "min_intervals": 2},
    }


def _js_embed_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def render_interval_page_html(payload: dict) -> str:
    return _TEMPLATE.replace("__PAYLOAD__", _js_embed_json(payload))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRF — interval analyzer</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb; --page: #f9f9f7;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --ring: rgba(11,11,11,0.10);
    --none: #ececea; --accent: #2a78d6;
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--ink); font-size: 13.5px; padding: 0 0 14px;
  }
  header { padding: 12px 16px 8px; }
  header h1 { font-size: 15.5px; font-weight: 650; }
  header .sub { color: var(--ink-2); font-size: 12px; margin-top: 3px; }
  .bar {
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
    padding: 9px 16px; border-top: 1px solid var(--grid); border-bottom: 1px solid var(--grid);
    background: var(--surface);
  }
  .bar label { color: var(--ink-2); font-size: 12px; display: flex; align-items: center; gap: 6px; }
  .seg { display: inline-flex; border: 1px solid var(--baseline); border-radius: 6px; overflow: hidden; }
  .seg button {
    font: inherit; font-size: 12.5px; padding: 4px 10px; border: none;
    background: var(--surface); color: var(--ink-2); cursor: pointer;
  }
  .seg button + button { border-left: 1px solid var(--baseline); }
  .seg button.on { background: #238b45; color: #fff; font-weight: 600; }
  #metricSeg button.on { background: var(--ink); font-weight: 650; }
  input[type=number] {
    font: inherit; font-size: 12.5px; width: 74px; padding: 3px 5px;
    border: 1px solid var(--baseline); border-radius: 6px; background: var(--surface);
  }
  select {
    font: inherit; font-size: 12.5px; padding: 3px 5px; color: var(--ink);
    border: 1px solid var(--baseline); border-radius: 6px; background: var(--surface);
  }
  .stepper { display: inline-flex; align-items: stretch; }
  .stepper input { width: 64px; border-radius: 0; text-align: center; }
  .stepper button {
    font: inherit; font-size: 10px; padding: 0 8px; cursor: pointer; color: var(--ink-2);
    border: 1px solid var(--baseline); background: var(--surface);
  }
  .stepper button:first-child { border-radius: 6px 0 0 6px; border-right: none; }
  .stepper button:last-child { border-radius: 0 6px 6px 0; border-left: none; }
  .stepper button:hover:not(:disabled) { background: var(--grid); color: var(--ink); }
  .stepper button:disabled { opacity: 0.35; cursor: default; }
  .lookout { font-variant-numeric: tabular-nums; color: var(--ink); font-weight: 600; }
  section { padding: 12px 16px 0; }
  h2 { font-size: 12px; font-weight: 650; color: var(--ink-2); text-transform: uppercase;
       letter-spacing: 0.04em; margin-bottom: 6px; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { padding: 3px 4px; text-align: center; font-size: 11.5px; white-space: nowrap; }
  th.rowhead, td.rowhead {
    text-align: left; color: var(--ink-2); font-weight: 500; padding-right: 8px;
    position: sticky; left: 0; background: var(--page); z-index: 1;
  }
  thead th { color: var(--ink-2); font-weight: 600; border-bottom: 1px solid var(--baseline); }
  thead th.off { color: var(--muted); text-decoration: line-through; }
  .strip td { height: 40px; font-size: 12.5px; font-weight: 650; border: 1px solid var(--surface); }
  .matrix td { border: 1px solid var(--surface); }
  .scroller { overflow-x: auto; }
  .note { color: var(--ink-2); font-size: 11.5px; margin-top: 5px; }
  .rec {
    margin: 12px 16px 0; background: var(--surface); border: 1px solid var(--ring);
    border-radius: 9px; padding: 11px 13px;
  }
  .rec h3 { font-size: 13px; font-weight: 650; margin-bottom: 6px; }
  .rec .alloc { display: flex; gap: 7px; flex-wrap: wrap; margin: 7px 0 8px; }
  .rec .chip {
    background: #238b45; color: #fff; border-radius: 6px; padding: 4px 9px;
    font-size: 12.5px; font-weight: 600; font-variant-numeric: tabular-nums;
  }
  .rec .stats { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2); }
  .rec .stats b { color: var(--ink); font-variant-numeric: tabular-nums; }
  .rec .check { margin-top: 8px; font-size: 11.5px; color: var(--ink-2);
                border-top: 1px solid var(--grid); padding-top: 7px; }
  .rec .check.agree { color: #1c6b38; }
  .legend { display: flex; align-items: center; gap: 0; margin-top: 7px; }
  .legend .cell { width: 48px; height: 9px; }
  /* 48px, not 34: a per-$1 figure prints as "$0.038" and at 34px the labels ran together
     into one unreadable string. The swatch width follows the label, not the other way. */
  .legend .labels { display: flex; font-size: 10px; color: var(--muted); margin-top: 2px; }
  .legend .labels span { width: 48px; }
  .warn { color: #8a5a00; font-size: 11.5px; margin-top: 6px; }
</style>
</head>
<body>
<header>
  <h1 id="title">PRF interval analyzer</h1>
  <div class="sub" id="sub"></div>
</header>

<div class="bar">
  <label>Metric <span class="seg" id="metricSeg"></span></label>
  <label title="Which allocation to show, by its rank on the chosen metric (1 = the best)">Rank
    <span class="stepper"><button type="button" id="rankUp" title="Up the list (toward #1)">&#9650;</button><input type="number" id="rankIn" min="1" step="1" value="1"><button type="button" id="rankDown" title="Down the list">&#9660;</button></span>
    <span class="lookout" id="rankOut"></span></label>
  <label>Coverage <span class="seg" id="covSeg"></span></label>
  <label>Window ends <select id="endYear"></select></label>
  <label>Lookback <select id="lookLen"></select>
    <span class="lookout" id="lookOut"></span></label>
  <label id="pfWrap">Productivity <input type="number" id="pf" min="0.6" max="1.5" step="0.05" value="1.00"></label>
</div>

<section>
  <h2 id="stripTitle">By interval</h2>
  <div class="scroller">
    <table id="strip"></table>
  </div>
  <div class="legend" id="legend"></div>
  <div class="note" id="stripNote"></div>
</section>

<div class="rec" id="rec"></div>

<section>
  <h2>Year by interval</h2>
  <div class="scroller">
    <table id="matrix"></table>
  </div>
  <div class="note" id="matrixNote"></div>
</section>

<script>
var DATA = __PAYLOAD__;

(function () {
  "use strict";

  // The app's sequential green, same ramp the county maps use.
  var RAMP = ["#e5f5e0", "#c7e9c0", "#a1d99b", "#74c476", "#41ab5d", "#238b45", "#005a32"];
  // A LOSS IS NOT A FAINT GAIN. Return metrics go negative, and painting a negative with the
  // palest green would read as "nearly break-even" when it is money gone. Negatives get their
  // own muted warm tone, outside the ramp entirely, so the eye never confuses the two.
  var LOSS = "#f0d8c8", LOSS_DEEP = "#dba57f";
  var NONE = "#ececea";

  var IV = DATA.intervals || [], YEARS = DATA.years || [];
  var LBL = DATA.interval_labels || {};
  var RULES = DATA.rules || {min_pct: 10, step_pct: 5, min_intervals: 2, max_intervals: 5};

  var METRICS = {
    win:  {label: "Win rate", unit: "%", title: "share of years the interval alone paid more than it cost"},
    net:  {label: "Return per $1", unit: "/$1", title: "average net return per $1 of protection"},
    acre: {label: "Return per acre", unit: "/ac", title: "average net return per acre"}
  };
  var metric = "net";
  var coverage = DATA.default_coverage || "0.9";
  // The window is "LOOKBACK years ending in ENDYEAR". The reader picks both; WANTED remembers
  // the length they asked for so moving the end year back and forth does not ratchet it down.
  var LENGTHS = [5, 10, 15, 20];
  var endYear = YEARS[YEARS.length - 1];
  var wanted = LENGTHS[LENGTHS.length - 1];
  var lookback = wanted;

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function el(id) { return document.getElementById(id); }

  // ---------------- controls
  function segment(host, items, labelFn, get, set) {
    var node = el(host);
    node.innerHTML = "";
    items.forEach(function (v) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = labelFn(v);
      b.classList.toggle("on", v === get());
      b.addEventListener("click", function () {
        if (get() === v) return;
        set(v);
        node.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
        draw();
      });
      node.appendChild(b);
    });
  }
  segment("metricSeg", ["win", "net", "acre"], function (k) { return METRICS[k].label; },
          function () { return metric; }, function (v) { metric = v; });
  segment("covSeg", DATA.coverages || ["0.9"], function (c) { return Math.round(parseFloat(c) * 100) + "%"; },
          function () { return coverage; }, function (v) { coverage = v; });

  // Window geometry, all in indices into YEARS: the window is [startIdx(), endIdx()] inclusive.
  function endIdx() { return YEARS.indexOf(endYear); }
  function startIdx() { return endIdx() - lookback + 1; }
  // Lengths that fit before this end year. Never empty, so a short history still has a window.
  function lengthsFor(ei) {
    var fit = LENGTHS.filter(function (n) { return n <= ei + 1; });
    return fit.length ? fit : [ei + 1];
  }
  function fitLookback() {
    var fit = lengthsFor(endIdx()), pick = fit[0];
    fit.forEach(function (n) { if (n <= wanted) pick = n; });
    lookback = pick;
    lenSel.innerHTML = fit.map(function (n) {
      return "<option value='" + n + "'>" + n + " years</option>";
    }).join("");
    lenSel.value = String(lookback);
  }
  var endSel = el("endYear"), lenSel = el("lookLen");
  // Newest first. The earliest end year is the first one with room for the shortest lookback.
  var firstEnd = Math.min(LENGTHS[0], YEARS.length) - 1;
  endSel.innerHTML = YEARS.slice(firstEnd).reverse().map(function (y) {
    return "<option value='" + y + "'>" + y + "</option>";
  }).join("");
  endSel.value = String(endYear);
  fitLookback();
  endSel.addEventListener("change", function () {
    endYear = +endSel.value; fitLookback(); draw();
  });
  lenSel.addEventListener("change", function () {
    wanted = +lenSel.value; lookback = wanted; draw();
  });
  el("pf").addEventListener("input", draw);
  el("pf").addEventListener("change", draw);

  function pf() {
    var v = parseFloat(el("pf").value);
    return isFinite(v) ? Math.min(1.5, Math.max(0.6, v)) : 1.0;
  }
  function subsidy() {
    var s = (DATA.subsidy || {})[coverage];
    return (s === undefined || s === null) ? null : s;
  }
  // Protection per acre = CBV x coverage x productivity — the same product the PRF map's
  // per-acre metric uses, so the two pages cannot drift apart.
  function protection() {
    if (DATA.cbv === null || DATA.cbv === undefined) return null;
    return DATA.cbv * parseFloat(coverage) * pf();
  }
  function windowYears() {
    return YEARS.slice(startIdx(), endIdx() + 1);
  }
  function offeredSet() {
    var list = (DATA.offered || {})[DATA.use] || [];
    var s = {};
    list.forEach(function (iv) { s[iv] = 1; });
    return s;
  }

  // ---------------- the analysis (mirrors src/prfopt.interval_year_nets)
  // net per $1 of protection = max(0, (coverage - index) / coverage) - rate x (1 - subsidy)
  function rateFor(iv) {
    var r = (DATA.rates || {})[iv];
    return r ? r[coverage] : null;
  }
  function netSeries(iv) {
    var rate = rateFor(iv), sub = subsidy();
    if (rate === null || rate === undefined || sub === null) return null;
    var c = parseFloat(coverage);
    var netPrem = rate * (1 - sub);
    var idx = (DATA.index || {})[iv] || [];
    var out = [];
    for (var k = startIdx(); k <= endIdx(); k++) {
      var v = idx[k];
      if (v === null || v === undefined) { out.push(null); continue; }
      out.push(Math.max(0, (c - v) / c) - netPrem);
    }
    return out;
  }
  function summarise(iv) {
    var s = netSeries(iv);
    if (!s) return null;
    var vals = s.filter(function (v) { return v !== null; });
    if (!vals.length) return null;
    var wins = 0, total = 0;
    vals.forEach(function (v) { if (v > 0) wins++; total += v; });
    var mean = total / vals.length;
    var prot = protection();
    return {
      n: vals.length,
      win: wins / vals.length,
      net: mean,
      acre: prot === null ? null : mean * prot,
      paid: s.filter(function (v) { return v !== null && v + rateFor(iv) * (1 - subsidy()) > 0; }).length,
      series: s
    };
  }
  function metricValue(sum) {
    if (!sum) return null;
    return metric === "win" ? sum.win : (metric === "net" ? sum.net : sum.acre);
  }

  // ---------------- colour
  function ramp(v, lo, hi) {
    if (v === null || v === undefined) return NONE;
    if (v < 0) {
      // depth of loss, on its own two-step warm scale
      var worst = Math.min(lo, 0);
      var t = worst < 0 ? Math.min(1, v / worst) : 0;
      return t > 0.5 ? LOSS_DEEP : LOSS;
    }
    if (hi <= 0) return RAMP[0];
    var span = hi - Math.max(lo, 0);
    var f = span > 0 ? (v - Math.max(lo, 0)) / span : 1;
    var i = Math.max(0, Math.min(RAMP.length - 1, Math.round(f * (RAMP.length - 1))));
    return RAMP[i];
  }
  function inkOn(bg) {
    return (bg === "#238b45" || bg === "#005a32" || bg === "#41ab5d") ? "#fff" : "var(--ink)";
  }

  // ---------------- formatting
  function fmtMetric(v) {
    if (v === null || v === undefined) return "&mdash;";
    if (metric === "win") return (v * 100).toFixed(0) + "%";
    if (metric === "net") return (v >= 0 ? "" : "−") + "$" + Math.abs(v).toFixed(3);
    return (v >= 0 ? "" : "−") + "$" + Math.abs(v).toFixed(2);
  }
  // Values at the storage cap are "this much or wetter" — far above any coverage level, so
  // the payout is zero either way, but the number itself should not pretend to be exact.
  function fmtIdx(v) {
    if (v === null || v === undefined) return "&mdash;";
    if (DATA.index_cap && v >= DATA.index_cap) return "≥" + DATA.index_cap.toFixed(2);
    return v.toFixed(2);
  }

  // ---------------- policy space (mirrors src/prfopt.enumerate_* exactly)
  function combos(usable) {
    var out = [], n = IV.length;
    function rec(start, picked) {
      if (picked.length >= RULES.min_intervals) out.push(picked.slice());
      if (picked.length === RULES.max_intervals) return;
      for (var i = start; i < n; i++) {
        if (picked.length && i - picked[picked.length - 1] < 2) continue;  // no two adjacent
        if (!usable[IV[i]]) continue;
        picked.push(i); rec(i + 1, picked); picked.pop();
      }
    }
    rec(0, []);
    return out;
  }
  function compositions(parts, cap) {
    // percentages in STEP_PCT units summing to 100, each in [MIN_PCT, cap]
    var step = RULES.step_pct, lo = RULES.min_pct / step, hi = cap / step, total = 100 / step;
    var out = [];
    function rec(left, k, acc) {
      if (k === 1) {
        if (left >= lo && left <= hi) { acc.push(left); out.push(acc.slice()); acc.pop(); }
        return;
      }
      for (var v = lo; v <= Math.min(hi, left - lo * (k - 1)); v++) {
        acc.push(v); rec(left - v, k - 1, acc); acc.pop();
      }
    }
    rec(total, parts, []);
    return out.map(function (c) { return c.map(function (v) { return v * step; }); });
  }
  var COMPO_CACHE = {};
  function compositionsFor(parts, cap) {
    var key = parts + ":" + cap;
    if (!COMPO_CACHE[key]) COMPO_CACHE[key] = compositions(parts, cap);
    return COMPO_CACHE[key];
  }
  // RMA PM-26-053: from crop year 2027 JAN-FEB (index 0) carries at most DATA.janfeb_cap percent
  // of value, below the county cap. Mirrors prfopt.enumerate_policies(janfeb_max_pct=...).
  function compositionsForCombo(idxs, cap) {
    var all = compositionsFor(idxs.length, cap), jc = DATA.janfeb_cap;
    if (!jc || idxs[0] !== 0) return all;
    return all.filter(function (p) { return p[0] <= jc; });
  }

  function recommend(series) {
    var scored = scoreAll(series), rows = ranked(scored);
    if (!rows.length) return null;
    var rank = Math.min(wantedRank(), rows.length), r = rows[rank - 1];
    return {avg: r.avg, win: r.win, idxs: r.idxs, props: r.props, rank: rank,
            of: rows.length, n_policies: scored.n};
  }

  // Every allocation is scored once per coverage and window, then sorted once per metric, so
  // stepping through ranks (or changing productivity) costs nothing.
  var SCORED = {key: null, rows: [], n: 0}, RANKED = {key: null, rows: []};

  function scoreAll(series) {
    var key = coverage + "|" + startIdx() + "|" + endIdx() + "|" + DATA.use;
    if (SCORED.key === key) return SCORED;
    var usable = {};
    IV.forEach(function (iv) { if (series[iv]) usable[iv] = 1; });
    var cap = DATA.cap || 60;
    var rows = [], n = 0;
    combos(usable).forEach(function (idxs) {
      var ws = idxs.map(function (i) { return series[IV[i]]; });
      compositionsForCombo(idxs, cap).forEach(function (props) {
        n++;
        var total = 0, wins = 0, yrs = ws[0].length, counted = 0;
        for (var y = 0; y < yrs; y++) {
          var net = 0, ok = true;
          for (var j = 0; j < ws.length; j++) {
            var v = ws[j][y];
            if (v === null) { ok = false; break; }
            net += (props[j] / 100) * v;
          }
          if (!ok) continue;
          counted++; total += net;
          if (net > 0) wins++;
        }
        if (!counted) return;
        rows.push({avg: total / counted, win: wins / counted, idxs: idxs, props: props,
                   seq: rows.length});
      });
    });
    SCORED = {key: key, rows: rows, n: n};
    RANKED = {key: null, rows: []};
    return SCORED;
  }

  // Return per acre is return per $1 times a constant (the protection), so it ranks
  // identically — only the printed units differ.
  // TIE-BREAK ON THE OTHER METRIC, matching src/prfopt.rank_by_win_rate (sorts win rate, then
  // average net) and rank_by_avg_net_return (average net, then win rate), then enumeration
  // order — the All Allocations table's order exactly, so rank N here is row N there.
  function ranked(scored) {
    var byWin = metric === "win", key = byWin ? "win" : "avg";
    if (RANKED.key === key) return RANKED.rows;
    var rows = scored.rows.slice().sort(function (a, b) {
      var sa = byWin ? a.win : a.avg, sb = byWin ? b.win : b.avg;
      if (Math.abs(sa - sb) > 1e-12) return sb - sa;
      var ta = byWin ? a.avg : a.win, tb = byWin ? b.avg : b.win;
      if (Math.abs(ta - tb) > 1e-12) return tb - ta;
      return a.seq - b.seq;
    });
    RANKED = {key: key, rows: rows};
    return rows;
  }

  // Rank N on the chosen metric: 1 is the recommendation; any other number shows that row of
  // the All Allocations table instead, and everything below follows it.
  el("rankIn").addEventListener("input", draw);
  el("rankIn").addEventListener("change", draw);
  // Up moves toward #1 (a better rank), down moves down the list.
  function stepRank(d) {
    var r = wantedRank() + d, top = +el("rankIn").max || r;
    el("rankIn").value = Math.max(1, Math.min(top, r));
    draw();
  }
  el("rankUp").addEventListener("click", function () { stepRank(-1); });
  el("rankDown").addEventListener("click", function () { stepRank(1); });
  function wantedRank() {
    var v = parseInt(el("rankIn").value, 10);
    return isFinite(v) && v >= 1 ? v : 1;
  }

  // ---------------- render
  function draw() {
    var yrs = windowYears();
    el("lookOut").textContent = lookback + " yr (" + yrs[0] + "–" + yrs[yrs.length - 1] + ")";
    el("pfWrap").style.display = metric === "acre" ? "" : "none";

    var offered = offeredSet();
    var sums = {}, series = {}, vals = [];
    IV.forEach(function (iv) {
      if (!offered[iv]) return;
      var s = summarise(iv);
      if (!s) return;
      sums[iv] = s; series[iv] = s.series;
      var v = metricValue(s);
      if (v !== null && v !== undefined) vals.push(v);
    });
    var lo = vals.length ? Math.min.apply(null, vals) : 0;
    var hi = vals.length ? Math.max.apply(null, vals) : 1;

    // -- the interval strip: one cell per interval, coloured by the chosen metric
    var head = "<thead><tr><th class='rowhead'></th>";
    IV.forEach(function (iv) {
      head += "<th class='" + (offered[iv] ? "" : "off") + "'>" + esc(LBL[iv] || iv) + "</th>";
    });
    head += "</tr></thead>";

    var body = "<tbody><tr class='strip'><td class='rowhead'>" +
               esc(METRICS[metric].label) + "</td>";
    IV.forEach(function (iv) {
      var s = sums[iv], v = metricValue(s);
      var bg = s ? ramp(v, lo, hi) : NONE;
      body += "<td style='background:" + bg + ";color:" + inkOn(bg) + "'>" +
              (s ? fmtMetric(v) : "&mdash;") + "</td>";
    });
    body += "</tr>";

    function plainRow(label, fn) {
      var h = "<tr><td class='rowhead'>" + label + "</td>";
      IV.forEach(function (iv) { h += "<td>" + fn(iv) + "</td>"; });
      return h + "</tr>";
    }
    body += plainRow("Avg index", function (iv) {
      var idx = (DATA.index || {})[iv] || [], tot = 0, n = 0;
      for (var k = startIdx(); k <= endIdx(); k++) {
        var v = idx[k];
        if (v === null || v === undefined) continue;
        tot += v; n++;
      }
      return n ? (tot / n).toFixed(2) : "&mdash;";
    });
    body += plainRow("Years paid", function (iv) {
      var s = sums[iv];
      if (!s) return "&mdash;";
      var c = parseFloat(coverage), idx = (DATA.index || {})[iv] || [], paid = 0, seen = 0;
      for (var k = startIdx(); k <= endIdx(); k++) {
        var v = idx[k];
        if (v === null || v === undefined) continue;
        seen++; if (v < c) paid++;
      }
      return paid + " / " + seen;
    });
    body += plainRow("Premium rate", function (iv) {
      var r = rateFor(iv);
      return r === null || r === undefined ? "&mdash;" : r.toFixed(4);
    });
    body += "</tbody>";
    el("strip").innerHTML = head + body;

    // legend
    var cells = RAMP.map(function (c) { return "<div class='cell' style='background:" + c + "'></div>"; }).join("");
    var labels = "<span>" + fmtMetric(Math.max(lo, 0)) + "</span>" +
                 RAMP.slice(1).map(function (_, i) {
                   var t = (i + 1) / (RAMP.length - 1);
                   return "<span>" + fmtMetric(Math.max(lo, 0) + t * (hi - Math.max(lo, 0))) + "</span>";
                 }).join("");
    el("legend").innerHTML =
      "<div>" + "<div class='legend' style='margin:0'>" + cells +
      (lo < 0 ? "<div class='cell' style='background:" + LOSS + ";margin-left:10px'></div>" +
                "<div class='cell' style='background:" + LOSS_DEEP + "'></div>" : "") +
      "</div><div class='labels'>" + labels +
      (lo < 0 ? "<span style='margin-left:10px;width:96px'>loss</span>" : "") + "</div></div>";

    var offCount = IV.filter(function (iv) { return !offered[iv]; }).length;
    el("stripNote").innerHTML =
      "Each interval scored ALONE over " + lookback + " years, at " +
      Math.round(parseFloat(coverage) * 100) + "% coverage" +
      (metric === "acre" ? " on $" + (protection() === null ? "?" : protection().toFixed(2)) +
        " of protection per acre (CBV $" + (DATA.cbv === null ? "?" : DATA.cbv.toFixed(2)) +
        " &times; coverage &times; productivity " + pf().toFixed(2) + ")" : "") +
      ". " + (offCount ? offCount + " interval(s) struck through are not offered for " +
              esc(DATA.use) + " on this grid." : "");

    // -- recommendation
    var best = recommend(series);
    el("rankOut").textContent = best ? "of " + best.of.toLocaleString() : "";
    el("rankIn").max = best ? best.of : 1;
    el("rankUp").disabled = !best || best.rank <= 1;
    el("rankDown").disabled = !best || best.rank >= best.of;
    drawRecommendation(best, yrs);

    // -- year x interval matrix, coloured by that year's net contribution
    drawMatrix(series, offered, best);
  }

  function drawRecommendation(best, yrs) {
    var node = el("rec");
    if (!best) {
      node.innerHTML = "<h3>Recommendation</h3><div class='note'>Not enough rated, " +
        "offered intervals on this grid to build an admissible allocation.</div>";
      return;
    }
    var prot = protection();
    var chips = best.idxs.map(function (ix, j) {
      return "<span class='chip'>" + esc(LBL[IV[ix]] || IV[ix]) + " &middot; " + best.props[j] + "%</span>";
    }).join("");
    var over = " over " + yrs.length + " years (" + yrs[0] + "–" + yrs[yrs.length - 1] + ")";
    var h = best.rank === 1
      ? "<h3>Recommended allocation &mdash; best " + esc(METRICS[metric].label.toLowerCase()) + over + "</h3>"
      : "<h3>Rank #" + best.rank.toLocaleString() + " of " + best.of.toLocaleString() +
        " by " + esc(METRICS[metric].label.toLowerCase()) + over + "</h3>";
    h += "<div class='alloc'>" + chips + "</div>";
    h += "<div class='stats'>" +
         "<span>Win rate <b>" + (best.win * 100).toFixed(1) + "%</b></span>" +
         "<span>Return per $1 <b>" + (best.avg >= 0 ? "" : "−") + "$" +
           Math.abs(best.avg).toFixed(4) + "</b></span>" +
         (prot === null ? "" : "<span>Return per acre <b>" + (best.avg >= 0 ? "" : "−") +
           "$" + Math.abs(best.avg * prot).toFixed(2) + "</b></span>") +
         "<span>Searched <b>" + best.n_policies.toLocaleString() + "</b> allocations" +
           " (cap " + (DATA.cap || 60) + "%)</span>" +
         "</div>";

    // Side-by-side with what the sweep already stored — the visible self-check. The sweep
    // scored ONE window (sw.y0..sw.y1); any other window has no stored answer to compare with.
    var sw = (DATA.sweep || {})[coverage];
    var first = yrs[0], last = yrs[yrs.length - 1];
    if (best.rank !== 1) {
      // only rank 1 has a stored answer to check against
    } else if (sw && +sw.y0 === first && +sw.y1 === last) {
      var combo = metric === "win" ? sw.win_combo : sw.net_combo;
      var props = metric === "win" ? sw.win_props : sw.net_props;
      var mine = best.idxs.map(function (ix, j) { return IV[ix] + ":" + best.props[j]; }).join(" ");
      var theirs = (combo || []).map(function (iv, j) { return iv + ":" + (props || [])[j]; }).join(" ");
      var agree = mine === theirs;
      h += "<div class='check" + (agree ? " agree" : "") + "'>" +
           (agree
             ? "✓ Matches the stored sweep (prf_opt_best) for this grid over " + first + "–" +
               last + ", which is the same optimizer over the same years."
             : "Stored sweep says <b>" + esc(theirs || "—") + "</b>. A difference here is " +
               "expected only when the sweep's cap or rated intervals differ from the current " +
               "selection — both are shown above.") +
           "</div>";
    } else if (sw) {
      var n = +sw.y1 - +sw.y0 + 1;
      h += "<div class='check'>The stored sweep (prf_opt_best) scores " + sw.y0 + "–" + sw.y1 +
           ". This window is different, so there is nothing stored to compare with; set " +
           "<b>Window ends</b> to " + sw.y1 + " and <b>Lookback</b> to " + n +
           " years to compare against it directly.</div>";
    }
    node.innerHTML = h;
  }

  function drawMatrix(series, offered, best) {
    var yrs = windowYears();
    var picked = {};
    if (best) best.idxs.forEach(function (ix, j) { picked[IV[ix]] = best.props[j]; });

    var flat = [];
    IV.forEach(function (iv) {
      if (!series[iv]) return;
      series[iv].forEach(function (v) { if (v !== null) flat.push(v); });
    });
    var lo = flat.length ? Math.min.apply(null, flat) : 0;
    var hi = flat.length ? Math.max.apply(null, flat) : 1;

    var h = "<thead><tr><th class='rowhead'>Year</th>";
    IV.forEach(function (iv) {
      h += "<th class='" + (offered[iv] ? "" : "off") + "'>" + esc(LBL[iv] || iv) +
           (picked[iv] ? " <span style='color:#238b45'>" + picked[iv] + "%</span>" : "") + "</th>";
    });
    h += "<th>Elected</th></tr></thead><tbody class='matrix'>";

    for (var k = yrs.length - 1; k >= 0; k--) {
      h += "<tr><td class='rowhead'>" + yrs[k] + "</td>";
      var elected = 0, electedOk = !!best;
      IV.forEach(function (iv) {
        var s = series[iv];
        var v = s ? s[k] : null;
        var bg = v === null || v === undefined ? NONE : ramp(v, lo, hi);
        var idx = (DATA.index || {})[iv];
        var iv_val = idx ? idx[startIdx() + k] : null;
        h += "<td style='background:" + bg + ";color:" + inkOn(bg) + "' title='index " +
             fmtIdx(iv_val) + "'>" + (v === null || v === undefined ? "&mdash;" : fmtIdx(iv_val)) +
             "</td>";
        if (picked[iv]) {
          if (v === null || v === undefined) electedOk = false;
          else elected += (picked[iv] / 100) * v;
        }
      });
      var ebg = !electedOk ? NONE : ramp(elected, lo, hi);
      h += "<td style='background:" + ebg + ";color:" + inkOn(ebg) + ";font-weight:650'>" +
           (!electedOk ? "&mdash;" : (elected >= 0 ? "" : "−") + "$" + Math.abs(elected).toFixed(3)) +
           "</td>";
      h += "</tr>";
    }
    h += "</tbody>";
    el("matrix").innerHTML = h;
    el("matrixNote").innerHTML =
      "Cells show the RAINFALL INDEX for that interval and year; the colour is what that " +
      "year's net return per $1 of protection would have been at " +
      Math.round(parseFloat(coverage) * 100) + "% coverage — green where the indemnity " +
      "beat the premium, warm where it did not. <b>Elected</b> applies the " +
      "allocation above (the chosen rank) to each year. Index values are stored to 0.005 of normal wherever a payout is possible (below " +
      "0.90) and in coarser steps above it, so <b>Avg index</b> can differ from RMA&rsquo;s by " +
      "about a hundredth, and where two allocations are nearly tied on <b>Win rate</b> the " +
      "pick can differ from an exact calculation by about a year in twenty (return per $1 " +
      "almost never does).";
  }

  // ---------------- header
  var where = DATA.county_name
    ? esc(DATA.county_name) + " County, " + esc(DATA.state || "")
    : "grid " + DATA.grid_id;
  el("title").innerHTML = "Grid " + DATA.grid_id + " — " + where;
  var sub =
    esc(DATA.use) + " &middot; County Base Value " +
    (DATA.cbv === null || DATA.cbv === undefined ? "not published" : "$" + DATA.cbv.toFixed(2) + "/acre") +
    " &middot; CY" + DATA.rate_year + " rates" +
    " &middot; index " + YEARS[0] + "–" + YEARS[YEARS.length - 1] +
    (DATA.index_source ? " (" + esc(DATA.index_source) + ")" : "") +
    " &middot; actuarial cap " + (DATA.cap || 60) + "% per interval" +
    (DATA.janfeb_cap ? " (Jan&ndash;Feb limited to " + DATA.janfeb_cap + "%)" : "");
  // Name the series, and say so loudly when it is the superseded one. RMA pays 2027
  // indemnities on the NCEI index; a recommendation built on the retired CPC series is
  // advice about numbers that no longer settle anything.
  if (DATA.index_provider && DATA.current_provider &&
      DATA.index_provider !== DATA.current_provider) {
    sub += "<br><span style='color:#8a5a00'>⚠ These are NOAA <b>" +
           esc(DATA.index_provider.toUpperCase()) + "</b> index values, the series RMA " +
           "retired after the 2026 crop year. For 2027 and beyond RMA settles on NOAA <b>" +
           esc(DATA.current_provider.toUpperCase()) + "</b>, on which about half of all index " +
           "cells differ by more than 0.10 of normal — reload the 2027CY vintage before " +
           "relying on this for a 2027 election.</span>";
  }
  // A grid that straddles counties has ONE rainfall history and SEVERAL base values. The
  // per-acre column and the cap belong to the county named above, so say which, and name the
  // others rather than letting the reader assume the dollars travel with the grid.
  var others = DATA.other_counties || [];
  if (others.length) {
    sub += "<br><span style='color:var(--muted)'>This grid also covers " +
           others.map(function (c) { return esc(c.name) + " (" + esc(c.state) + ")"; }).join(", ") +
           " — the rainfall is the same there, the base value and cap are not, " +
           "so pick the county the acres are actually in.</span>";
  }
  el("sub").innerHTML = sub;

  // What the reader has set, for the host window's "Save deck" button (the Mac app reads this
  // from the same-origin iframe, so the deck is built for exactly what is on screen).
  window.PRF_STATE = function () {
    return {coverage: coverage, productivity: pf(), endYear: endYear, lookback: lookback,
            metric: metric};
  };

  draw();
})();
</script>
</body>
</html>
"""
